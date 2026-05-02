from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
import os
import uvicorn

app = FastAPI(
    title="PCB Panelization AI Toolkit API",
    version="0.1.0",
    description="Phase 1 MVP: DXF upload + PCB panelization candidate generator for Dify workflow."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ["true", "1", "yes", "y", "是", "有"]


def calculate_candidates(
    single_board_length: float,
    single_board_width: float,
    rail_width: float,
    smt_max_length: float,
    smt_max_width: float,
    ict_max_length: float,
    ict_max_width: float,
    has_bga_qfn: bool,
    has_dip: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
):
    panel_patterns = [
        (1, 1), (1, 2), (2, 1), (2, 2),
        (3, 1), (1, 3), (3, 2), (2, 3),
        (4, 1), (1, 4), (4, 2), (2, 4)
    ]

    candidates: List[Dict[str, Any]] = []

    for x_count, y_count in panel_patterns:
        panel_length = single_board_length * x_count + rail_width * 2
        panel_width = single_board_width * y_count + rail_width * 2
        pcs_per_panel = x_count * y_count
        panel_area = panel_length * panel_width
        board_area_total = single_board_length * single_board_width * pcs_per_panel
        utilization = board_area_total / panel_area if panel_area > 0 else 0

        reasons = []
        risk_score = 0

        if panel_length > smt_max_length:
            reasons.append(f"Panel 長度 {panel_length:.1f} mm 超過 SMT 最大長度 {smt_max_length:.1f} mm")
            risk_score += 100
        if panel_width > smt_max_width:
            reasons.append(f"Panel 寬度 {panel_width:.1f} mm 超過 SMT 最大寬度 {smt_max_width:.1f} mm")
            risk_score += 100
        if panel_length > ict_max_length or panel_width > ict_max_width:
            reasons.append("Panel 尺寸可能超過 ICT 治具限制")
            risk_score += 30

        aspect_ratio = max(panel_length, panel_width) / min(panel_length, panel_width)
        if aspect_ratio > 3:
            reasons.append(f"Panel 長寬比 {aspect_ratio:.2f} 過大，可能有板彎或輸送不穩風險")
            risk_score += 25
        elif aspect_ratio > 2.3:
            reasons.append(f"Panel 長寬比 {aspect_ratio:.2f} 偏大，建議 ME 確認輸送穩定性")
            risk_score += 10

        if is_irregular_shape:
            split_method = "Router / Tab"
            reasons.append("異形板不建議直接使用 V-cut，建議 Router 或 Tab")
            risk_score += 15
        else:
            split_method = "V-cut"

        if has_bga_qfn:
            reasons.append("有 BGA/QFN，需確認距 V-cut 或 Router 邊界安全距離與分板應力")
            risk_score += 15
        if has_dip:
            reasons.append("有 DIP，需確認波峰焊方向、錫流方向與治具需求")
            risk_score += 10
        if has_heavy_component:
            reasons.append("有重零件，需評估過爐板彎、支撐點與分板應力")
            risk_score += 10

        # Penalize very small panels in production context
        if pcs_per_panel == 1:
            reasons.append("單片 Panel 生產效率較低，除非產品尺寸較大或有特殊限制，通常不作為優先方案")
            risk_score += 15

        if risk_score >= 100:
            status = "not_recommended"
        elif risk_score >= 40:
            status = "use_with_caution"
        else:
            status = "recommended"

        candidates.append({
            "panel_type": f"{x_count}x{y_count}",
            "x_count": x_count,
            "y_count": y_count,
            "panel_length_mm": round(panel_length, 2),
            "panel_width_mm": round(panel_width, 2),
            "pcs_per_panel": pcs_per_panel,
            "aspect_ratio": round(aspect_ratio, 2),
            "area_utilization_percent": round(utilization * 100, 2),
            "split_method": split_method,
            "risk_score": risk_score,
            "status": status,
            "reasons": reasons
        })

    recommended = sorted(
        [c for c in candidates if c["status"] == "recommended"],
        key=lambda x: (-x["pcs_per_panel"], x["risk_score"], -x["area_utilization_percent"])
    )
    caution = sorted(
        [c for c in candidates if c["status"] == "use_with_caution"],
        key=lambda x: (x["risk_score"], -x["pcs_per_panel"])
    )

    best_candidate = recommended[0] if recommended else (caution[0] if caution else None)
    return best_candidate, candidates


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "PCB Panelization AI Toolkit API",
        "version": "0.1.0",
        "docs": "/docs",
        "main_endpoint": "/api/pcb/generate-panel-candidates"
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/api/pcb/generate-panel-candidates")
async def generate_panel_candidates(
    dxf_file: UploadFile = File(...),
    product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...),
    single_board_width: float = Form(...),
    rail_width: float = Form(5.0),
    smt_max_length: float = Form(330.0),
    smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0),
    ict_max_width: float = Form(300.0),
    has_bga_qfn: Optional[str] = Form("false"),
    has_dip: Optional[str] = Form("false"),
    has_heavy_component: Optional[str] = Form("false"),
    is_irregular_shape: Optional[str] = Form("false"),
):
    file_name = dxf_file.filename or "uploaded.dxf"
    file_bytes = await dxf_file.read()
    file_size = len(file_bytes)

    bga = to_bool(has_bga_qfn)
    dip = to_bool(has_dip)
    heavy = to_bool(has_heavy_component)
    irregular = to_bool(is_irregular_shape)

    best_candidate, candidates = calculate_candidates(
        single_board_length=single_board_length,
        single_board_width=single_board_width,
        rail_width=rail_width,
        smt_max_length=smt_max_length,
        smt_max_width=smt_max_width,
        ict_max_length=ict_max_length,
        ict_max_width=ict_max_width,
        has_bga_qfn=bga,
        has_dip=dip,
        has_heavy_component=heavy,
        is_irregular_shape=irregular,
    )

    return {
        "product_name": product_name,
        "input_file": {
            "filename": file_name,
            "size_bytes": file_size,
            "note": "Phase 1 MVP 目前接收 DXF 檔，但尚未解析 DXF 幾何；板長寬以使用者輸入為準。"
        },
        "stage": "phase_1_mvp",
        "important_limitations": [
            "第一階段僅產生連版建議與方案比較。",
            "第一階段不產生正式可投板 Gerber。",
            "第一階段 DXF 檔僅作為上傳流程驗證；幾何解析會在第二階段加入。",
            "正式投產前仍需 ME / CAM 工程師確認。"
        ],
        "single_board": {
            "length_mm": single_board_length,
            "width_mm": single_board_width,
            "rail_width_mm": rail_width
        },
        "machine_limits": {
            "smt_max_length_mm": smt_max_length,
            "smt_max_width_mm": smt_max_width,
            "ict_max_length_mm": ict_max_length,
            "ict_max_width_mm": ict_max_width
        },
        "process_flags": {
            "has_bga_qfn": bga,
            "has_dip": dip,
            "has_heavy_component": heavy,
            "is_irregular_shape": irregular
        },
        "best_candidate": best_candidate,
        "candidates": candidates
    }


@app.post("/api/pcb/generate-report-text")
async def generate_report_text(
    dxf_file: UploadFile = File(...),
    product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...),
    single_board_width: float = Form(...),
    rail_width: float = Form(5.0),
    smt_max_length: float = Form(330.0),
    smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0),
    ict_max_width: float = Form(300.0),
    has_bga_qfn: Optional[str] = Form("false"),
    has_dip: Optional[str] = Form("false"),
    has_heavy_component: Optional[str] = Form("false"),
    is_irregular_shape: Optional[str] = Form("false"),
):
    # Simple text report endpoint for users who want to test without Dify LLM first.
    result = await generate_panel_candidates(
        dxf_file=dxf_file,
        product_name=product_name,
        single_board_length=single_board_length,
        single_board_width=single_board_width,
        rail_width=rail_width,
        smt_max_length=smt_max_length,
        smt_max_width=smt_max_width,
        ict_max_length=ict_max_length,
        ict_max_width=ict_max_width,
        has_bga_qfn=has_bga_qfn,
        has_dip=has_dip,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
    )
    best = result["best_candidate"]
    if not best:
        conclusion = "目前沒有找到可推薦方案，請降低連版數、調整工藝邊或確認設備限制。"
    else:
        conclusion = (
            f"建議採用 {best['panel_type']} 連版，Panel 尺寸約 "
            f"{best['panel_length_mm']} x {best['panel_width_mm']} mm，"
            f"每 Panel {best['pcs_per_panel']} pcs，建議分板方式：{best['split_method']}。"
        )

    return {
        "report_text": f"""
# PCB 連版規劃 AI 建議報告

## 一、AI 建議結論
{conclusion}

## 二、重要限制
- 第一階段僅產生連版建議與方案比較。
- 第一階段不產生正式可投板 Gerber。
- DXF 幾何自動解析會在第二階段加入。
- 正式投產前需由 ME / CAM 工程師確認。

## 三、ME 最終確認清單
□ Panel 尺寸是否符合 SMT 進板限制  
□ Panel 尺寸是否符合 ICT 治具限制  
□ V-cut / Router 安全距離是否足夠  
□ Fiducial / Tooling hole 是否符合公司規範  
□ 過爐方向與錫流方向是否合理  
□ BGA/QFN、重零件是否有分板應力風險  
""".strip(),
        "raw_result": result
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
