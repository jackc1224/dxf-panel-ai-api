from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any
import os
import tempfile
import uuid
import shutil
from datetime import timedelta, datetime
import requests
import traceback

from minio import Minio
from minio.error import S3Error


app = FastAPI(title="PCB Panelization API", version="0.9.0-all-candidates-status-zh")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# MinIO 環境變數設定
# =========================================================

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "pcb-dxf")
MINIO_SECURE = os.getenv("MINIO_SECURE", "true").lower() == "true"


# =========================================================
# Dify 環境變數設定
# =========================================================

DIFY_API_BASE = os.getenv("DIFY_API_BASE", "")
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "")


# =========================================================
# 背景任務暫存
# 注意：這是 MVP 版本，服務重啟後 job 記錄會消失
# =========================================================

JOB_STORE: Dict[str, Dict[str, Any]] = {}


# =========================================================
# 共用工具
# =========================================================

def normalize_yes_no(value: str) -> str:
    """
    Dify Select 欄位只接受 Yes / No。
    前端可能傳 true / false、是 / 否，這裡統一轉換。
    """
    v = str(value).strip().lower()

    if v in ["yes", "true", "1", "是", "y"]:
        return "Yes"

    if v in ["no", "false", "0", "否", "n"]:
        return "No"

    return "No"


def yes_no_to_bool(value: Any) -> bool:
    v = str(value).strip().lower()

    if v in ["yes", "true", "1", "是", "y"]:
        return True

    return False


def safe_filename_name(filename: str) -> str:
    return (
        filename
        .replace("/", "_")
        .replace("\\", "_")
        .replace(" ", "_")
    )


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def status_to_zh(status_code: str) -> str:
    mapping = {
        "recommended": "建議",
        "use_with_caution": "謹慎使用",
        "not_recommended": "不建議"
    }
    return mapping.get(status_code, status_code)


def risk_level_zh(risk_score: int) -> str:
    if risk_score >= 100:
        return "高風險"
    if risk_score >= 40:
        return "中風險"
    return "低風險"


def reasons_to_text(reasons: List[str]) -> str:
    if not reasons:
        return "符合目前尺寸與製程限制，無明顯重大風險。"
    return "；".join(reasons)


# =========================================================
# MinIO 共用函式
# =========================================================

def get_minio_client() -> Minio:
    if not MINIO_ENDPOINT:
        raise HTTPException(status_code=500, detail="MINIO_ENDPOINT is not configured")

    if not MINIO_ACCESS_KEY:
        raise HTTPException(status_code=500, detail="MINIO_ACCESS_KEY is not configured")

    if not MINIO_SECRET_KEY:
        raise HTTPException(status_code=500, detail="MINIO_SECRET_KEY is not configured")

    endpoint = MINIO_ENDPOINT.replace("https://", "").replace("http://", "")

    return Minio(
        endpoint=endpoint,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def ensure_bucket_exists():
    client = get_minio_client()

    try:
        found = client.bucket_exists(MINIO_BUCKET)

        if not found:
            client.make_bucket(MINIO_BUCKET)

    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"MinIO bucket error: {str(e)}")


def upload_file_to_minio(
    local_path: str,
    object_name: str,
    content_type: str = "application/dxf"
):
    ensure_bucket_exists()
    client = get_minio_client()

    try:
        client.fput_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            file_path=local_path,
            content_type=content_type,
        )

    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"MinIO upload error: {str(e)}")


def download_file_from_minio(object_name: str, local_path: str):
    ensure_bucket_exists()
    client = get_minio_client()

    try:
        client.fget_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            file_path=local_path,
        )

    except S3Error as e:
        raise HTTPException(
            status_code=404,
            detail=f"MinIO object not found or download failed: {str(e)}"
        )


def get_presigned_download_url(object_name: str, hours: int = 24) -> str:
    ensure_bucket_exists()
    client = get_minio_client()

    try:
        return client.presigned_get_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            expires=timedelta(hours=hours),
        )

    except S3Error as e:
        raise HTTPException(
            status_code=500,
            detail=f"MinIO presigned url error: {str(e)}"
        )


# =========================================================
# 基礎 API
# =========================================================

@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "PCB Panelization API",
        "version": "0.9.0-all-candidates-status-zh",
        "message": "Use /upload for DXF upload page, or /docs for API testing."
    }


@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    html_path = os.path.join(os.getcwd(), "upload.html")

    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="upload.html not found")

    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/health/minio")
def health_minio():
    try:
        client = get_minio_client()
        bucket_exists = client.bucket_exists(MINIO_BUCKET)

        if not bucket_exists:
            client.make_bucket(MINIO_BUCKET)

        return {
            "status": "ok",
            "message": "MinIO connection success",
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT,
            "secure": MINIO_SECURE,
            "bucket_created_or_exists": True
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "MinIO connection failed",
            "endpoint": MINIO_ENDPOINT,
            "secure": MINIO_SECURE,
            "bucket": MINIO_BUCKET,
            "error_type": type(e).__name__,
            "error_detail": str(e)
        }


@app.get("/api/health/dify")
def health_dify():
    return {
        "status": "ok" if DIFY_API_BASE and DIFY_API_KEY else "error",
        "DIFY_API_BASE_configured": bool(DIFY_API_BASE),
        "DIFY_API_KEY_configured": bool(DIFY_API_KEY),
        "DIFY_API_BASE": DIFY_API_BASE,
        "expected_workflow_run_url": f"{DIFY_API_BASE.rstrip('/')}/workflows/run" if DIFY_API_BASE else ""
    }


# =========================================================
# 共用：產生連版候選方案
# =========================================================

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
    is_irregular_shape: bool
) -> Dict[str, Any]:

    panel_patterns = [
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
        (3, 1),
        (1, 3),
        (3, 2),
        (2, 3),
        (4, 1),
        (1, 4),
    ]

    candidates: List[Dict[str, Any]] = []

    for x_count, y_count in panel_patterns:
        panel_length = single_board_length * x_count + rail_width * 2
        panel_width = single_board_width * y_count + rail_width * 2
        pcs_per_panel = x_count * y_count

        reasons = []
        risk_score = 0

        if panel_length > smt_max_length:
            reasons.append(
                f"Panel 長度 {panel_length:.1f} mm 超過 SMT 最大長度 {smt_max_length:.1f} mm"
            )
            risk_score += 100

        if panel_width > smt_max_width:
            reasons.append(
                f"Panel 寬度 {panel_width:.1f} mm 超過 SMT 最大寬度 {smt_max_width:.1f} mm"
            )
            risk_score += 100

        if ict_max_length > 0 and ict_max_width > 0:
            if panel_length > ict_max_length or panel_width > ict_max_width:
                reasons.append(
                    f"Panel 尺寸 {panel_length:.1f} x {panel_width:.1f} mm 可能超過 ICT 治具限制 {ict_max_length:.1f} x {ict_max_width:.1f} mm"
                )
                risk_score += 30

        aspect_ratio = max(panel_length, panel_width) / min(panel_length, panel_width)

        if aspect_ratio > 3:
            reasons.append(
                f"Panel 長寬比 {aspect_ratio:.2f} 過大，可能有板彎或輸送不穩風險"
            )
            risk_score += 25

        if is_irregular_shape:
            split_method = "Router / Tab"
            reasons.append("異形板不建議直接使用 V-cut，建議 Router 或 Tab")
            risk_score += 15
        else:
            split_method = "V-cut"

        if has_bga_qfn:
            reasons.append("有 BGA/QFN，需確認距 V-cut 或 Router 邊界安全距離")
            risk_score += 15

        if has_dip:
            reasons.append("有 DIP，需確認波峰焊方向、錫流方向與治具需求")
            risk_score += 10

        if has_heavy_component:
            reasons.append("有重零件，需評估過爐板彎與分板應力")
            risk_score += 10

        if risk_score >= 100:
            status_code = "not_recommended"
        elif risk_score >= 40:
            status_code = "use_with_caution"
        else:
            status_code = "recommended"

        candidate = {
            "panel_type": f"{x_count}x{y_count}",
            "x_count": x_count,
            "y_count": y_count,
            "panel_length_mm": round(panel_length, 2),
            "panel_width_mm": round(panel_width, 2),
            "panel_size": f"{panel_length:.1f} x {panel_width:.1f} mm",
            "pcs_per_panel": pcs_per_panel,
            "aspect_ratio": round(aspect_ratio, 2),
            "split_method": split_method,
            "risk_score": risk_score,
            "risk_level_zh": risk_level_zh(risk_score),
            "status_code": status_code,
            "status_zh": status_to_zh(status_code),
            "recommended_conclusion": status_to_zh(status_code),
            "reasons": reasons,
            "reason_text": reasons_to_text(reasons)
        }

        candidates.append(candidate)

    recommended = sorted(
        [c for c in candidates if c["status_code"] == "recommended"],
        key=lambda x: (
            -x["pcs_per_panel"],
            x["risk_score"],
            x["panel_length_mm"] * x["panel_width_mm"]
        )
    )

    caution = sorted(
        [c for c in candidates if c["status_code"] == "use_with_caution"],
        key=lambda x: (
            -x["pcs_per_panel"],
            x["risk_score"],
            x["panel_length_mm"] * x["panel_width_mm"]
        )
    )

    not_recommended = sorted(
        [c for c in candidates if c["status_code"] == "not_recommended"],
        key=lambda x: (
            x["risk_score"],
            -x["pcs_per_panel"]
        )
    )

    if recommended:
        best_candidate = recommended[0]
    elif caution:
        best_candidate = caution[0]
    else:
        best_candidate = not_recommended[0]

    comparison_table_markdown = build_comparison_table_markdown(candidates)
    not_recommended_summary = build_not_recommended_summary(candidates)
    recommendation_summary = build_recommendation_summary(best_candidate)

    return {
        "best_candidate": best_candidate,
        "all_candidates": candidates,
        "candidates": candidates,
        "recommended_candidates": recommended,
        "caution_candidates": caution,
        "not_recommended_candidates": not_recommended,
        "comparison_table_markdown": comparison_table_markdown,
        "not_recommended_summary": not_recommended_summary,
        "recommendation_summary": recommendation_summary
    }


def build_comparison_table_markdown(candidates: List[Dict[str, Any]]) -> str:
    lines = []
    lines.append("| 方案 | Panel 尺寸 | pcs/panel | 分板方式 | 風險分數 | 風險等級 | 狀態 | 建議原因 |")
    lines.append("|---|---:|---:|---|---:|---|---|---|")

    for c in candidates:
        lines.append(
            f"| {c['panel_type']} "
            f"| {c['panel_size']} "
            f"| {c['pcs_per_panel']} "
            f"| {c['split_method']} "
            f"| {c['risk_score']} "
            f"| {c['risk_level_zh']} "
            f"| {c['status_zh']} "
            f"| {c['reason_text']} |"
        )

    return "\n".join(lines)


def build_not_recommended_summary(candidates: List[Dict[str, Any]]) -> str:
    lines = []

    caution_or_not = [
        c for c in candidates
        if c["status_code"] in ["use_with_caution", "not_recommended"]
    ]

    if not caution_or_not:
        return "本次所有候選方案皆為建議方案，未出現謹慎使用或不建議方案。"

    for c in caution_or_not:
        lines.append(
            f"- 方案 {c['panel_type']}：{c['status_zh']}，原因：{c['reason_text']}"
        )

    return "\n".join(lines)


def build_recommendation_summary(best_candidate: Dict[str, Any]) -> str:
    return (
        f"本次系統推薦方案為 {best_candidate['panel_type']}，"
        f"Panel 尺寸為 {best_candidate['panel_size']}，"
        f"每 Panel 可生產 {best_candidate['pcs_per_panel']} pcs，"
        f"建議分板方式為 {best_candidate['split_method']}。"
        f"此方案風險分數為 {best_candidate['risk_score']}，"
        f"狀態為「{best_candidate['status_zh']}」。"
        f"主要判斷原因：{best_candidate['reason_text']}"
    )


def build_ai_report_markdown(
    product_name: str,
    object_key: str,
    result: Dict[str, Any]
) -> str:
    best = result["best_candidate"]

    report = f"""
# PCB 連版規劃 AI 建議報告

## 一、AI 建議結論

- 產品名稱：{product_name}
- 原始 DXF object_key：{object_key}
- 建議連版方式：{best["panel_type"]}
- 建議 Panel 尺寸：{best["panel_size"]}
- 每 Panel 數量：{best["pcs_per_panel"]} pcs/panel
- 建議分板方式：{best["split_method"]}
- 風險分數：{best["risk_score"]}
- 風險等級：{best["risk_level_zh"]}
- 狀態：{best["status_zh"]}
- 是否可進入下一階段：{"可進入下一階段，但仍需 ME / CAM 工程師確認" if best["status_code"] == "recommended" else "需先由 ME / CAM 工程師審查後再決定"}

## 二、方案比較表

{result["comparison_table_markdown"]}

## 三、推薦方案說明

{result["recommendation_summary"]}

## 四、謹慎使用與不建議方案原因

{result["not_recommended_summary"]}

## 五、製程風險提醒

- 若有 BGA/QFN，需確認元件距離 V-cut 或 Router 邊界的安全距離。
- 若有 DIP，需確認波峰焊方向、錫流方向與治具需求。
- 若有重零件，需評估過爐板彎、支撐方式與分板應力。
- 若為異形板，通常不建議直接使用 V-cut，建議優先評估 Router 或 Tab。
- 若 Panel 尺寸超過 SMT 或 ICT 限制，該方案應列為不建議。

## 六、ME / CAM 最終確認清單

- 單板尺寸是否正確。
- Panel 尺寸是否符合 SMT 最大進板限制。
- Panel 尺寸是否符合 ICT 治具限制。
- 分板方式是否符合產品結構與元件配置。
- Fiducial、Tooling Hole、工藝邊寬度是否符合公司規範。
- 是否需要補強支撐、治具或調整過爐方向。

## 七、輸出限制說明

本階段輸出的 DXF 為 AI 建議版，正式投產前仍需 ME / CAM 工程師確認 V-cut、Router、Fiducial、Tooling Hole 與分板應力。
"""
    return report.strip()


# =========================================================
# 共用：DXF 產生工具
# =========================================================

def dxf_header() -> str:
    return """0
SECTION
2
HEADER
9
$ACADVER
1
AC1009
0
ENDSEC
0
SECTION
2
TABLES
0
TABLE
2
LAYER
70
5
0
LAYER
2
BOARD
70
0
62
7
6
CONTINUOUS
0
LAYER
2
PANEL
70
0
62
3
6
CONTINUOUS
0
LAYER
2
VCUT
70
0
62
1
6
CONTINUOUS
0
LAYER
2
TOOLING
70
0
62
5
6
CONTINUOUS
0
LAYER
2
FIDUCIAL
70
0
62
2
6
CONTINUOUS
0
ENDTAB
0
ENDSEC
0
SECTION
2
ENTITIES
"""


def dxf_footer() -> str:
    return """0
ENDSEC
0
EOF
"""


def dxf_line(x1: float, y1: float, x2: float, y2: float, layer: str) -> str:
    return f"""0
LINE
8
{layer}
10
{x1:.3f}
20
{y1:.3f}
30
0.000
11
{x2:.3f}
21
{y2:.3f}
31
0.000
"""


def dxf_circle(x: float, y: float, r: float, layer: str) -> str:
    return f"""0
CIRCLE
8
{layer}
10
{x:.3f}
20
{y:.3f}
30
0.000
40
{r:.3f}
"""


def dxf_rect(x: float, y: float, w: float, h: float, layer: str) -> str:
    return (
        dxf_line(x, y, x + w, y, layer)
        + dxf_line(x + w, y, x + w, y + h, layer)
        + dxf_line(x + w, y + h, x, y + h, layer)
        + dxf_line(x, y + h, x, y, layer)
    )


def create_panel_dxf(
    output_path: str,
    product_name: str,
    single_board_length: float,
    single_board_width: float,
    rail_width: float,
    candidate: Dict[str, Any]
):
    x_count = candidate["x_count"]
    y_count = candidate["y_count"]
    panel_length = candidate["panel_length_mm"]
    panel_width = candidate["panel_width_mm"]

    content = dxf_header()

    content += dxf_rect(0, 0, panel_length, panel_width, "PANEL")

    for i in range(x_count):
        for j in range(y_count):
            x = rail_width + i * single_board_length
            y = rail_width + j * single_board_width
            content += dxf_rect(x, y, single_board_length, single_board_width, "BOARD")

    for i in range(1, x_count):
        x = rail_width + i * single_board_length
        content += dxf_line(x, rail_width, x, panel_width - rail_width, "VCUT")

    for j in range(1, y_count):
        y = rail_width + j * single_board_width
        content += dxf_line(rail_width, y, panel_length - rail_width, y, "VCUT")

    hole_r = 1.6
    offset = max(rail_width / 2, 2.5)

    content += dxf_circle(offset, offset, hole_r, "TOOLING")
    content += dxf_circle(panel_length - offset, offset, hole_r, "TOOLING")
    content += dxf_circle(offset, panel_width - offset, hole_r, "TOOLING")
    content += dxf_circle(panel_length - offset, panel_width - offset, hole_r, "TOOLING")

    fid_r = 0.75

    content += dxf_circle(rail_width, rail_width, fid_r, "FIDUCIAL")
    content += dxf_circle(panel_length - rail_width, rail_width, fid_r, "FIDUCIAL")
    content += dxf_circle(rail_width, panel_width - rail_width, fid_r, "FIDUCIAL")

    content += dxf_footer()

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)


# =========================================================
# API：上傳 DXF 到 MinIO
# =========================================================

@app.post("/api/pcb/upload-dxf-to-minio")
async def upload_dxf_to_minio(
    dxf_file: UploadFile = File(...)
):
    if not dxf_file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    if not dxf_file.filename.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail="Only .dxf file is allowed")

    file_id = str(uuid.uuid4())
    safe_filename = safe_filename_name(dxf_file.filename)

    object_key = f"uploads/{file_id}/{safe_filename}"
    tmp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{safe_filename}")

    with open(tmp_path, "wb") as buffer:
        shutil.copyfileobj(dxf_file.file, buffer)

    file_size = os.path.getsize(tmp_path)

    upload_file_to_minio(
        local_path=tmp_path,
        object_name=object_key,
        content_type="application/dxf",
    )

    try:
        os.remove(tmp_path)
    except Exception:
        pass

    return {
        "status": "success",
        "file_id": file_id,
        "object_key": object_key,
        "filename": safe_filename,
        "file_size_bytes": file_size,
        "message": "DXF uploaded to MinIO successfully. Use object_key in Dify workflow."
    }


# =========================================================
# API：用 MinIO object_key 產生候選方案
# =========================================================

@app.post("/api/pcb/generate-panel-candidates-from-minio")
async def generate_panel_candidates_from_minio(
    object_key: str = Form(...),
    product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...),
    single_board_width: float = Form(...),
    rail_width: float = Form(5.0),
    smt_max_length: float = Form(330.0),
    smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0),
    ict_max_width: float = Form(300.0),
    has_bga_qfn: str = Form("No"),
    has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"),
    is_irregular_shape: str = Form("No")
):
    local_input = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_input.dxf")
    download_file_from_minio(object_key, local_input)

    try:
        os.remove(local_input)
    except Exception:
        pass

    result = calculate_candidates(
        single_board_length,
        single_board_width,
        rail_width,
        smt_max_length,
        smt_max_width,
        ict_max_length,
        ict_max_width,
        yes_no_to_bool(has_bga_qfn),
        yes_no_to_bool(has_dip),
        yes_no_to_bool(has_heavy_component),
        yes_no_to_bool(is_irregular_shape)
    )

    report_markdown = build_ai_report_markdown(
        product_name=product_name,
        object_key=object_key,
        result=result
    )

    return {
        "product_name": product_name,
        "object_key": object_key,
        "stage": "phase_1_dxf_mvp_minio",
        "note": "DXF 已儲存在 MinIO，Dify 僅傳 object_key，避免 Dify 檔案大小限制。",
        "single_board": {
            "length_mm": single_board_length,
            "width_mm": single_board_width
        },
        "machine_limits": {
            "smt_max_length": smt_max_length,
            "smt_max_width": smt_max_width,
            "ict_max_length": ict_max_length,
            "ict_max_width": ict_max_width
        },
        "best_candidate": result["best_candidate"],
        "all_candidates": result["all_candidates"],
        "candidates": result["candidates"],
        "recommended_candidates": result["recommended_candidates"],
        "caution_candidates": result["caution_candidates"],
        "not_recommended_candidates": result["not_recommended_candidates"],
        "comparison_table_markdown": result["comparison_table_markdown"],
        "recommendation_summary": result["recommendation_summary"],
        "not_recommended_summary": result["not_recommended_summary"],
        "report_markdown": report_markdown
    }


# =========================================================
# API：用 MinIO object_key 產生連版 DXF，並上傳回 MinIO
# =========================================================

@app.post("/api/pcb/generate-panel-dxf-from-minio")
async def generate_panel_dxf_from_minio(
    object_key: str = Form(...),
    product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...),
    single_board_width: float = Form(...),
    rail_width: float = Form(5.0),
    smt_max_length: float = Form(330.0),
    smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0),
    ict_max_width: float = Form(300.0),
    has_bga_qfn: str = Form("No"),
    has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"),
    is_irregular_shape: str = Form("No")
):
    local_input = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_input.dxf")
    download_file_from_minio(object_key, local_input)

    try:
        os.remove(local_input)
    except Exception:
        pass

    result = calculate_candidates(
        single_board_length,
        single_board_width,
        rail_width,
        smt_max_length,
        smt_max_width,
        ict_max_length,
        ict_max_width,
        yes_no_to_bool(has_bga_qfn),
        yes_no_to_bool(has_dip),
        yes_no_to_bool(has_heavy_component),
        yes_no_to_bool(is_irregular_shape)
    )

    candidate = result["best_candidate"]

    safe_name = safe_filename_name(product_name)

    output_name = f"panelized_{safe_name}_{candidate['panel_type']}.dxf"
    output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{output_name}")

    create_panel_dxf(
        output_path=output_path,
        product_name=product_name,
        single_board_length=single_board_length,
        single_board_width=single_board_width,
        rail_width=rail_width,
        candidate=candidate
    )

    output_object_key = f"outputs/{uuid.uuid4()}/{output_name}"

    upload_file_to_minio(
        local_path=output_path,
        object_name=output_object_key,
        content_type="application/dxf",
    )

    try:
        os.remove(output_path)
    except Exception:
        pass

    download_url = get_presigned_download_url(output_object_key, hours=24)

    return {
        "status": "success",
        "product_name": product_name,
        "input_object_key": object_key,
        "output_object_key": output_object_key,
        "output_filename": output_name,
        "download_url": download_url,
        "expires_hours": 24,
        "best_candidate": candidate,
        "all_candidates": result["all_candidates"],
        "comparison_table_markdown": result["comparison_table_markdown"],
        "recommendation_summary": result["recommendation_summary"],
        "not_recommended_summary": result["not_recommended_summary"],
        "message": "Panelized DXF generated and uploaded to MinIO."
    }


# =========================================================
# API：下載 MinIO 內的 DXF
# =========================================================

@app.get("/api/pcb/download")
def download_from_minio(
    object_key: str
):
    local_path = os.path.join(
        tempfile.gettempdir(),
        f"{uuid.uuid4()}_{os.path.basename(object_key)}"
    )

    download_file_from_minio(object_key, local_path)

    return FileResponse(
        local_path,
        media_type="application/dxf",
        filename=os.path.basename(object_key)
    )


# =========================================================
# 背景任務：呼叫 Dify Workflow
# =========================================================

def run_dify_job_background(job_id: str, inputs: Dict[str, str]):
    JOB_STORE[job_id]["status"] = "running"
    JOB_STORE[job_id]["message"] = "Dify Workflow 執行中..."
    JOB_STORE[job_id]["started_at"] = datetime.utcnow().isoformat()

    try:
        if not DIFY_API_BASE:
            raise Exception("DIFY_API_BASE is not configured")

        if not DIFY_API_KEY:
            raise Exception("DIFY_API_KEY is not configured")

        url = f"{DIFY_API_BASE.rstrip('/')}/workflows/run"

        payload = {
            "inputs": {
                "product_name": str(inputs.get("product_name", "")),
                "object_key": str(inputs.get("object_key", "")),
                "single_board_length": str(inputs.get("single_board_length", "")),
                "single_board_width": str(inputs.get("single_board_width", "")),
                "rail_width": str(inputs.get("rail_width", "5")),
                "smt_max_length": str(inputs.get("smt_max_length", "330")),
                "smt_max_width": str(inputs.get("smt_max_width", "250")),
                "ict_max_length": str(inputs.get("ict_max_length", "350")),
                "ict_max_width": str(inputs.get("ict_max_width", "300")),
                "has_bga_qfn": normalize_yes_no(inputs.get("has_bga_qfn", "No")),
                "has_dip": normalize_yes_no(inputs.get("has_dip", "No")),
                "has_heavy_component": normalize_yes_no(inputs.get("has_heavy_component", "No")),
                "is_irregular_shape": normalize_yes_no(inputs.get("is_irregular_shape", "No"))
            },
            "response_mode": "blocking",
            "user": "pcb-upload-page"
        }

        headers = {
            "Authorization": f"Bearer {DIFY_API_KEY}",
            "Content-Type": "application/json"
        }

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=600
        )

        if response.status_code >= 400:
            JOB_STORE[job_id]["status"] = "failed"
            JOB_STORE[job_id]["message"] = "Dify Workflow API failed"
            JOB_STORE[job_id]["error"] = {
                "dify_status_code": response.status_code,
                "dify_url": url,
                "sent_payload": payload,
                "dify_response": response.text
            }
            JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()
            return

        content_type = response.headers.get("content-type", "")

        if "application/json" not in content_type:
            JOB_STORE[job_id]["status"] = "failed"
            JOB_STORE[job_id]["message"] = "Dify API returned non-JSON response"
            JOB_STORE[job_id]["error"] = {
                "dify_status_code": response.status_code,
                "content_type": content_type,
                "dify_url": url,
                "sent_payload": payload,
                "dify_response_preview": response.text[:1500]
            }
            JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()
            return

        result_json = response.json()

        JOB_STORE[job_id]["status"] = "success"
        JOB_STORE[job_id]["message"] = "Dify Workflow 執行完成"
        JOB_STORE[job_id]["result"] = result_json
        JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()

    except Exception as e:
        JOB_STORE[job_id]["status"] = "failed"
        JOB_STORE[job_id]["message"] = "Dify Workflow 執行失敗"
        JOB_STORE[job_id]["error"] = {
            "error_type": type(e).__name__,
            "error_detail": str(e),
            "traceback": traceback.format_exc()
        }
        JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()


# =========================================================
# API：開始 Dify 背景任務
# =========================================================

@app.post("/api/pcb/start-dify-job")
async def start_dify_job(
    background_tasks: BackgroundTasks,
    object_key: str = Form(...),
    product_name: str = Form(...),
    single_board_length: str = Form(...),
    single_board_width: str = Form(...),
    rail_width: str = Form("5"),
    smt_max_length: str = Form("330"),
    smt_max_width: str = Form("250"),
    ict_max_length: str = Form("350"),
    ict_max_width: str = Form("300"),
    has_bga_qfn: str = Form("No"),
    has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"),
    is_irregular_shape: str = Form("No")
):
    job_id = str(uuid.uuid4())

    inputs = {
        "object_key": object_key,
        "product_name": product_name,
        "single_board_length": single_board_length,
        "single_board_width": single_board_width,
        "rail_width": rail_width,
        "smt_max_length": smt_max_length,
        "smt_max_width": smt_max_width,
        "ict_max_length": ict_max_length,
        "ict_max_width": ict_max_width,
        "has_bga_qfn": has_bga_qfn,
        "has_dip": has_dip,
        "has_heavy_component": has_heavy_component,
        "is_irregular_shape": is_irregular_shape
    }

    JOB_STORE[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "message": "任務已建立，等待背景執行",
        "inputs": inputs,
        "created_at": datetime.utcnow().isoformat(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None
    }

    background_tasks.add_task(run_dify_job_background, job_id, inputs)

    return {
        "status": "accepted",
        "job_id": job_id,
        "message": "Dify Workflow background job started."
    }


# =========================================================
# API：查詢 Dify 背景任務狀態
# =========================================================

@app.get("/api/pcb/job-status/{job_id}")
def get_job_status(job_id: str):
    job = JOB_STORE.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")

    return job


# =========================================================
# 舊版同步 API：保留但不建議前端使用
# =========================================================

@app.post("/api/pcb/run-dify-panelization")
async def run_dify_panelization(
    object_key: str = Form(...),
    product_name: str = Form(...),
    single_board_length: str = Form(...),
    single_board_width: str = Form(...),
    rail_width: str = Form("5"),
    smt_max_length: str = Form("330"),
    smt_max_width: str = Form("250"),
    ict_max_length: str = Form("350"),
    ict_max_width: str = Form("300"),
    has_bga_qfn: str = Form("No"),
    has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"),
    is_irregular_shape: str = Form("No")
):
    if not DIFY_API_BASE:
        raise HTTPException(status_code=500, detail="DIFY_API_BASE is not configured")

    if not DIFY_API_KEY:
        raise HTTPException(status_code=500, detail="DIFY_API_KEY is not configured")

    url = f"{DIFY_API_BASE.rstrip('/')}/workflows/run"

    payload = {
        "inputs": {
            "product_name": str(product_name),
            "object_key": str(object_key),
            "single_board_length": str(single_board_length),
            "single_board_width": str(single_board_width),
            "rail_width": str(rail_width),
            "smt_max_length": str(smt_max_length),
            "smt_max_width": str(smt_max_width),
            "ict_max_length": str(ict_max_length),
            "ict_max_width": str(ict_max_width),
            "has_bga_qfn": normalize_yes_no(has_bga_qfn),
            "has_dip": normalize_yes_no(has_dip),
            "has_heavy_component": normalize_yes_no(has_heavy_component),
            "is_irregular_shape": normalize_yes_no(is_irregular_shape)
        },
        "response_mode": "blocking",
        "user": "pcb-upload-page"
    }

    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=600
        )

        if response.status_code >= 400:
            raise HTTPException(
                status_code=response.status_code,
                detail={
                    "message": "Dify workflow API failed",
                    "dify_status_code": response.status_code,
                    "dify_url": url,
                    "sent_payload": payload,
                    "dify_response": response.text
                }
            )

        content_type = response.headers.get("content-type", "")

        if "application/json" not in content_type:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Dify API returned non-JSON response",
                    "dify_status_code": response.status_code,
                    "content_type": content_type,
                    "dify_url": url,
                    "sent_payload": payload,
                    "dify_response_preview": response.text[:1000]
                }
            )

        return response.json()

    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Dify workflow timeout")

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Dify request error: {str(e)}")
