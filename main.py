from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional, Tuple
import os
import tempfile
import uuid
import shutil
from datetime import timedelta, datetime
import requests
import traceback

from minio import Minio
from minio.error import S3Error

import ezdxf
from ezdxf import bbox
from ezdxf.math import Matrix44


app = FastAPI(title="PCB Panelization API", version="0.8.0-minio-dify-async-job")

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
# 注意：MVP 版本，服務重啟後 job 記錄會消失
# =========================================================

JOB_STORE: Dict[str, Dict[str, Any]] = {}


# =========================================================
# 共用工具
# =========================================================

def normalize_yes_no(value: str) -> str:
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
        .replace(":", "_")
        .replace("*", "_")
        .replace("?", "_")
        .replace('"', "_")
        .replace("<", "_")
        .replace(">", "_")
        .replace("|", "_")
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


def make_public_api_download_url(object_key: str) -> str:
    return f"/api/pcb/download?object_key={object_key}"


def select_display_candidates(candidates: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    """
    報告最多只顯示前 limit 個候選方案。

    排序邏輯：
    1. 優先顯示建議方案
    2. 再顯示謹慎使用方案
    3. 最後顯示不建議方案
    4. 同類型中 pcs/panel 越高越優先
    5. 風險分數越低越優先
    """
    status_priority = {
        "recommended": 0,
        "use_with_caution": 1,
        "not_recommended": 2
    }

    sorted_candidates = sorted(
        candidates,
        key=lambda c: (
            status_priority.get(c.get("status_code", ""), 9),
            -int(c.get("pcs_per_panel", 0)),
            int(c.get("risk_score", 9999)),
            float(c.get("panel_length_mm", 9999)) * float(c.get("panel_width_mm", 9999))
        )
    )

    return sorted_candidates[:limit]


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
        "version": "0.8.0-minio-dify-async-job",
        "message": "Use /upload for DXF upload page, or /docs for API testing.",
        "important_note": "This version includes real DXF panelization by reading source DXF entities with ezdxf.",
        "display_rule": "Report only shows top 3 display_candidates. all_candidates are still kept for debugging."
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
# 候選方案計算
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
        if is_irregular_shape:
            gap_x = 2.0
            gap_y = 2.0
        else:
            gap_x = 0.0
            gap_y = 0.0

        panel_length = (
            single_board_length * x_count
            + gap_x * max(x_count - 1, 0)
            + rail_width * 2
        )

        panel_width = (
            single_board_width * y_count
            + gap_y * max(y_count - 1, 0)
            + rail_width * 2
        )

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

        aspect_ratio = max(panel_length, panel_width) / max(min(panel_length, panel_width), 0.001)

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

        status_zh = status_to_zh(status_code)

        candidates.append({
            "panel_type": f"{x_count}x{y_count}",
            "x_count": x_count,
            "y_count": y_count,
            "columns": x_count,
            "rows": y_count,
            "gap_x_mm": gap_x,
            "gap_y_mm": gap_y,
            "panel_length_mm": round(panel_length, 2),
            "panel_width_mm": round(panel_width, 2),
            "panel_size": f"{panel_length:.1f} x {panel_width:.1f} mm",
            "pcs_per_panel": pcs_per_panel,
            "aspect_ratio": round(aspect_ratio, 2),
            "split_method": split_method,
            "risk_score": risk_score,
            "risk_level_zh": risk_level_zh(risk_score),
            "status_code": status_code,
            "status": status_zh,
            "status_zh": status_zh,
            "reasons": reasons,
            "reason_text": reasons_to_text(reasons)
        })

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

    display_candidates = select_display_candidates(candidates, limit=3)

    return {
        "best_candidate": best_candidate,
        "display_candidates": display_candidates,
        "candidates": candidates,
        "all_candidates": candidates,
        "recommended_candidates": recommended,
        "caution_candidates": caution,
        "not_recommended_candidates": not_recommended
    }


def build_comparison_table_markdown(candidates: List[Dict[str, Any]], limit: int = 3) -> str:
    display_candidates = select_display_candidates(candidates, limit=limit)

    lines = []
    lines.append("| 方案 | Panel 尺寸 | pcs/panel | 分板方式 | 風險分數 | 風險等級 | 狀態 | 建議原因 |")
    lines.append("|---|---:|---:|---|---:|---|---|---|")

    for c in display_candidates:
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


def build_caution_and_not_summary(candidates: List[Dict[str, Any]], limit: int = 3) -> str:
    display_candidates = select_display_candidates(candidates, limit=limit)

    items = [
        c for c in display_candidates
        if c["status_code"] in ["use_with_caution", "not_recommended"]
    ]

    if not items:
        return "本次顯示的前三個候選方案皆為建議方案，未出現謹慎使用或不建議方案。"

    lines = []
    for c in items:
        lines.append(f"- 方案 {c['panel_type']}：{c['status_zh']}，原因：{c['reason_text']}")

    return "\n".join(lines)


def build_ai_report_markdown(
    product_name: str,
    object_key: str,
    result: Dict[str, Any],
    panel_dxf: Optional[Dict[str, Any]] = None
) -> str:
    best = result["best_candidate"]
    comparison_table = build_comparison_table_markdown(result["all_candidates"], limit=3)
    caution_text = build_caution_and_not_summary(result["all_candidates"], limit=3)

    dxf_info = ""
    if panel_dxf:
        dxf_info = f"""
- 輸出 DXF 檔名：{panel_dxf.get("output_filename", "")}
- 輸出 object_key：{panel_dxf.get("output_object_key", "")}
- 下載連結：{panel_dxf.get("download_url", "")}
""".strip()

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

## 二、前三個候選方案比較表

{comparison_table}

## 三、推薦方案說明

本次系統推薦方案為 {best["panel_type"]}，Panel 尺寸為 {best["panel_size"]}，每 Panel 可生產 {best["pcs_per_panel"]} pcs，建議分板方式為 {best["split_method"]}。此方案風險分數為 {best["risk_score"]}，風險等級為「{best["risk_level_zh"]}」，狀態為「{best["status_zh"]}」。主要判斷原因：{best["reason_text"]}

## 四、前三個候選方案中，謹慎使用與不建議方案原因

{caution_text}

## 五、DXF 輸出資訊

{dxf_info if dxf_info else "DXF 已由系統產生，請於下載連結取得。"}

## 六、製程風險提醒

- 若有 BGA/QFN，需確認元件距離 V-cut 或 Router 邊界的安全距離。
- 若有 DIP，需確認波峰焊方向、錫流方向與治具需求。
- 若有重零件，需評估過爐板彎、支撐方式與分板應力。
- 若為異形板，通常不建議直接使用 V-cut，建議優先評估 Router 或 Tab。
- 若 Panel 尺寸超過 SMT 或 ICT 限制，該方案應列為不建議。

## 七、ME / CAM 最終確認清單

- 單板尺寸是否正確。
- Panel 尺寸是否符合 SMT 最大進板限制。
- Panel 尺寸是否符合 ICT 治具限制。
- 分板方式是否符合產品結構與元件配置。
- Fiducial、Tooling Hole、工藝邊寬度是否符合公司規範。
- 是否需要補強支撐、治具或調整過爐方向。

## 八、輸出限制說明

本階段輸出的 DXF 為 AI 建議版，正式投產前仍需 ME / CAM 工程師確認 V-cut、Router、Fiducial、Tooling Hole 與分板應力。
"""
    return report.strip()


# =========================================================
# DXF 幾何工具
# =========================================================

def get_modelspace_bbox(doc) -> Optional[Tuple[float, float, float, float]]:
    try:
        msp = doc.modelspace()
        ext = bbox.extents(msp, fast=True)

        if not ext.has_data:
            return None

        min_x = float(ext.extmin.x)
        min_y = float(ext.extmin.y)
        max_x = float(ext.extmax.x)
        max_y = float(ext.extmax.y)

        if max_x <= min_x or max_y <= min_y:
            return None

        return min_x, min_y, max_x, max_y

    except Exception:
        return None


def ensure_layer(doc, name: str, color: int = 7):
    try:
        if name not in doc.layers:
            doc.layers.add(name=name, color=color)
    except Exception:
        pass


def transform_entity_safe(entity, matrix: Matrix44) -> bool:
    try:
        entity.transform(matrix)
        return True
    except Exception:
        return False


def add_lwpolyline_rect(msp, x: float, y: float, w: float, h: float, layer: str):
    msp.add_lwpolyline(
        [
            (x, y),
            (x + w, y),
            (x + w, y + h),
            (x, y + h),
            (x, y),
        ],
        dxfattribs={"layer": layer, "closed": True}
    )


def add_line(msp, x1: float, y1: float, x2: float, y2: float, layer: str):
    msp.add_line(
        (x1, y1),
        (x2, y2),
        dxfattribs={"layer": layer}
    )


def add_circle(msp, x: float, y: float, r: float, layer: str):
    msp.add_circle(
        center=(x, y),
        radius=r,
        dxfattribs={"layer": layer}
    )


def add_text(msp, text: str, x: float, y: float, height: float, layer: str):
    try:
        msp.add_text(
            text,
            dxfattribs={
                "layer": layer,
                "height": height,
                "insert": (x, y)
            }
        )
    except Exception:
        pass


def create_real_panel_dxf_from_source(
    source_path: str,
    output_path: str,
    product_name: str,
    single_board_length: float,
    single_board_width: float,
    rail_width: float,
    candidate: Dict[str, Any]
) -> Dict[str, Any]:

    doc = ezdxf.readfile(source_path)
    msp = doc.modelspace()

    ensure_layer(doc, "PANEL_OUTLINE", 3)
    ensure_layer(doc, "PANEL_VCUT", 1)
    ensure_layer(doc, "PANEL_ROUTE", 2)
    ensure_layer(doc, "PANEL_TOOLING", 5)
    ensure_layer(doc, "PANEL_FIDUCIAL", 6)
    ensure_layer(doc, "PANEL_TEXT", 7)
    ensure_layer(doc, "PANEL_DIMENSION", 4)

    detected_bbox = get_modelspace_bbox(doc)

    if detected_bbox:
        min_x, min_y, max_x, max_y = detected_bbox
        detected_board_w = max_x - min_x
        detected_board_h = max_y - min_y
    else:
        min_x = 0.0
        min_y = 0.0
        detected_board_w = single_board_length
        detected_board_h = single_board_width

    board_w = detected_board_w if detected_board_w > 0 else single_board_length
    board_h = detected_board_h if detected_board_h > 0 else single_board_width

    columns = int(candidate.get("x_count", candidate.get("columns", 1)))
    rows = int(candidate.get("y_count", candidate.get("rows", 1)))

    split_method = str(candidate.get("split_method", "V-cut"))

    if "Router" in split_method or "Tab" in split_method:
        gap_x = float(candidate.get("gap_x_mm", 2.0))
        gap_y = float(candidate.get("gap_y_mm", 2.0))
    else:
        gap_x = float(candidate.get("gap_x_mm", 0.0))
        gap_y = float(candidate.get("gap_y_mm", 0.0))

    rail_left = rail_width
    rail_right = rail_width
    rail_bottom = rail_width
    rail_top = rail_width

    pitch_x = board_w + gap_x
    pitch_y = board_h + gap_y

    panel_w = rail_left + columns * board_w + max(columns - 1, 0) * gap_x + rail_right
    panel_h = rail_bottom + rows * board_h + max(rows - 1, 0) * gap_y + rail_top

    source_entities = list(msp)

    base_dx = rail_left - min_x
    base_dy = rail_bottom - min_y
    base_matrix = Matrix44.translate(base_dx, base_dy, 0)

    for entity in source_entities:
        transform_entity_safe(entity, base_matrix)

    for row in range(rows):
        for col in range(columns):
            if row == 0 and col == 0:
                continue

            dx = col * pitch_x
            dy = row * pitch_y
            copy_matrix = Matrix44.translate(dx, dy, 0)

            for entity in source_entities:
                try:
                    copied = entity.copy()
                    transform_entity_safe(copied, copy_matrix)
                    msp.add_entity(copied)
                except Exception:
                    continue

    add_lwpolyline_rect(
        msp,
        0,
        0,
        panel_w,
        panel_h,
        "PANEL_OUTLINE"
    )

    for row in range(rows):
        for col in range(columns):
            x = rail_left + col * pitch_x
            y = rail_bottom + row * pitch_y
            add_lwpolyline_rect(
                msp,
                x,
                y,
                board_w,
                board_h,
                "PANEL_OUTLINE"
            )

    if "V-cut" in split_method or "V-CUT" in split_method or split_method == "V-cut":
        for col in range(1, columns):
            x = rail_left + col * board_w + (col - 0.5) * gap_x
            add_line(
                msp,
                x,
                rail_bottom,
                x,
                panel_h - rail_top,
                "PANEL_VCUT"
            )

        for row in range(1, rows):
            y = rail_bottom + row * board_h + (row - 0.5) * gap_y
            add_line(
                msp,
                rail_left,
                y,
                panel_w - rail_right,
                y,
                "PANEL_VCUT"
            )
    else:
        for col in range(1, columns):
            x1 = rail_left + col * board_w + (col - 1) * gap_x
            add_lwpolyline_rect(
                msp,
                x1,
                rail_bottom,
                max(gap_x, 0.3),
                panel_h - rail_top - rail_bottom,
                "PANEL_ROUTE"
            )

        for row in range(1, rows):
            y1 = rail_bottom + row * board_h + (row - 1) * gap_y
            add_lwpolyline_rect(
                msp,
                rail_left,
                y1,
                panel_w - rail_left - rail_right,
                max(gap_y, 0.3),
                "PANEL_ROUTE"
            )

    tooling_r = 1.6
    tooling_margin = max(rail_width / 2.0, 2.5)

    add_circle(msp, tooling_margin, tooling_margin, tooling_r, "PANEL_TOOLING")
    add_circle(msp, panel_w - tooling_margin, tooling_margin, tooling_r, "PANEL_TOOLING")
    add_circle(msp, tooling_margin, panel_h - tooling_margin, tooling_r, "PANEL_TOOLING")
    add_circle(msp, panel_w - tooling_margin, panel_h - tooling_margin, tooling_r, "PANEL_TOOLING")

    fid_r = 0.75
    fid_margin = max(rail_width, 3.0)

    add_circle(msp, fid_margin, panel_h - fid_margin, fid_r, "PANEL_FIDUCIAL")
    add_circle(msp, panel_w - fid_margin, panel_h - fid_margin, fid_r, "PANEL_FIDUCIAL")
    add_circle(msp, fid_margin, fid_margin, fid_r, "PANEL_FIDUCIAL")

    text_y = panel_h + 8

    add_text(
        msp,
        f"Product: {product_name}",
        0,
        text_y,
        2.5,
        "PANEL_TEXT"
    )

    add_text(
        msp,
        f"One piece: {board_w:.2f} x {board_h:.2f} mm",
        0,
        text_y - 4,
        2.5,
        "PANEL_TEXT"
    )

    add_text(
        msp,
        f"Panel size: {panel_w:.2f} x {panel_h:.2f} mm",
        0,
        text_y - 8,
        2.5,
        "PANEL_TEXT"
    )

    add_text(
        msp,
        f"{columns * rows} pcs/panel, Layout: {columns} x {rows}, Split: {split_method}",
        0,
        text_y - 12,
        2.5,
        "PANEL_TEXT"
    )

    add_text(
        msp,
        "AI suggested DXF. ME/CAM confirmation required before production.",
        0,
        text_y - 16,
        2.5,
        "PANEL_TEXT"
    )

    dim_offset = 6.0

    add_line(
        msp,
        0,
        -dim_offset,
        panel_w,
        -dim_offset,
        "PANEL_DIMENSION"
    )

    add_line(
        msp,
        -dim_offset,
        0,
        -dim_offset,
        panel_h,
        "PANEL_DIMENSION"
    )

    add_text(
        msp,
        f"{panel_w:.2f}",
        panel_w / 2.0,
        -dim_offset - 3,
        2.0,
        "PANEL_DIMENSION"
    )

    add_text(
        msp,
        f"{panel_h:.2f}",
        -dim_offset - 8,
        panel_h / 2.0,
        2.0,
        "PANEL_DIMENSION"
    )

    doc.saveas(output_path)

    return {
        "detected_board_width_mm": round(board_w, 3),
        "detected_board_height_mm": round(board_h, 3),
        "panel_width_mm": round(panel_w, 3),
        "panel_height_mm": round(panel_h, 3),
        "columns": columns,
        "rows": rows,
        "pcs_per_panel": columns * rows,
        "gap_x_mm": gap_x,
        "gap_y_mm": gap_y,
        "rail_left_mm": rail_left,
        "rail_right_mm": rail_right,
        "rail_top_mm": rail_top,
        "rail_bottom_mm": rail_bottom,
        "split_method": split_method
    }


def create_simple_panel_dxf_fallback(
    output_path: str,
    product_name: str,
    single_board_length: float,
    single_board_width: float,
    rail_width: float,
    candidate: Dict[str, Any]
) -> Dict[str, Any]:

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    ensure_layer(doc, "PANEL_OUTLINE", 3)
    ensure_layer(doc, "PANEL_VCUT", 1)
    ensure_layer(doc, "PANEL_TOOLING", 5)
    ensure_layer(doc, "PANEL_FIDUCIAL", 6)
    ensure_layer(doc, "PANEL_TEXT", 7)

    columns = int(candidate.get("x_count", 1))
    rows = int(candidate.get("y_count", 1))

    gap_x = float(candidate.get("gap_x_mm", 0.0))
    gap_y = float(candidate.get("gap_y_mm", 0.0))

    panel_w = rail_width * 2 + single_board_length * columns + gap_x * max(columns - 1, 0)
    panel_h = rail_width * 2 + single_board_width * rows + gap_y * max(rows - 1, 0)

    pitch_x = single_board_length + gap_x
    pitch_y = single_board_width + gap_y

    add_lwpolyline_rect(msp, 0, 0, panel_w, panel_h, "PANEL_OUTLINE")

    for row in range(rows):
        for col in range(columns):
            x = rail_width + col * pitch_x
            y = rail_width + row * pitch_y
            add_lwpolyline_rect(msp, x, y, single_board_length, single_board_width, "PANEL_OUTLINE")

    for col in range(1, columns):
        x = rail_width + col * single_board_length + (col - 0.5) * gap_x
        add_line(msp, x, rail_width, x, panel_h - rail_width, "PANEL_VCUT")

    for row in range(1, rows):
        y = rail_width + row * single_board_width + (row - 0.5) * gap_y
        add_line(msp, rail_width, y, panel_w - rail_width, y, "PANEL_VCUT")

    tooling_r = 1.6
    margin = max(rail_width / 2.0, 2.5)

    add_circle(msp, margin, margin, tooling_r, "PANEL_TOOLING")
    add_circle(msp, panel_w - margin, margin, tooling_r, "PANEL_TOOLING")
    add_circle(msp, margin, panel_h - margin, tooling_r, "PANEL_TOOLING")
    add_circle(msp, panel_w - margin, panel_h - margin, tooling_r, "PANEL_TOOLING")

    fid_r = 0.75
    add_circle(msp, rail_width, rail_width, fid_r, "PANEL_FIDUCIAL")
    add_circle(msp, panel_w - rail_width, rail_width, fid_r, "PANEL_FIDUCIAL")
    add_circle(msp, rail_width, panel_h - rail_width, fid_r, "PANEL_FIDUCIAL")

    add_text(msp, f"Product: {product_name}", 0, panel_h + 8, 2.5, "PANEL_TEXT")
    add_text(msp, f"Panel: {columns} x {rows}, {columns * rows} pcs", 0, panel_h + 4, 2.5, "PANEL_TEXT")
    add_text(msp, "Fallback simple panel DXF. Source geometry was not copied.", 0, panel_h, 2.5, "PANEL_TEXT")

    doc.saveas(output_path)

    return {
        "detected_board_width_mm": single_board_length,
        "detected_board_height_mm": single_board_width,
        "panel_width_mm": round(panel_w, 3),
        "panel_height_mm": round(panel_h, 3),
        "columns": columns,
        "rows": rows,
        "pcs_per_panel": columns * rows,
        "gap_x_mm": gap_x,
        "gap_y_mm": gap_y,
        "split_method": candidate.get("split_method", "V-cut"),
        "fallback": True
    }


# =========================================================
# 本地保險產出：即使 Dify 失敗也能產出真實連版 DXF
# =========================================================

def generate_local_panelization_outputs(inputs: Dict[str, str]) -> Dict[str, Any]:
    object_key = str(inputs.get("object_key", ""))
    product_name = str(inputs.get("product_name", "UNKNOWN"))

    single_board_length = to_float(inputs.get("single_board_length", 120), 120)
    single_board_width = to_float(inputs.get("single_board_width", 80), 80)
    rail_width = to_float(inputs.get("rail_width", 5), 5)
    smt_max_length = to_float(inputs.get("smt_max_length", 330), 330)
    smt_max_width = to_float(inputs.get("smt_max_width", 250), 250)
    ict_max_length = to_float(inputs.get("ict_max_length", 350), 350)
    ict_max_width = to_float(inputs.get("ict_max_width", 300), 300)

    has_bga_qfn = yes_no_to_bool(inputs.get("has_bga_qfn", "No"))
    has_dip = yes_no_to_bool(inputs.get("has_dip", "No"))
    has_heavy_component = yes_no_to_bool(inputs.get("has_heavy_component", "No"))
    is_irregular_shape = yes_no_to_bool(inputs.get("is_irregular_shape", "No"))

    local_input = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_source.dxf")
    download_file_from_minio(object_key, local_input)

    detected_length = single_board_length
    detected_width = single_board_width

    try:
        source_doc = ezdxf.readfile(local_input)
        detected_bbox = get_modelspace_bbox(source_doc)
        if detected_bbox:
            min_x, min_y, max_x, max_y = detected_bbox
            detected_length = max_x - min_x
            detected_width = max_y - min_y

            if detected_length <= 0:
                detected_length = single_board_length

            if detected_width <= 0:
                detected_width = single_board_width
    except Exception:
        detected_length = single_board_length
        detected_width = single_board_width

    result = calculate_candidates(
        detected_length,
        detected_width,
        rail_width,
        smt_max_length,
        smt_max_width,
        ict_max_length,
        ict_max_width,
        has_bga_qfn,
        has_dip,
        has_heavy_component,
        is_irregular_shape
    )

    candidate = result["best_candidate"]

    safe_name = safe_filename_name(product_name)
    output_name = f"panelized_{safe_name}_{candidate['panel_type']}_real.dxf"
    output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{output_name}")

    geometry_info = {}

    try:
        geometry_info = create_real_panel_dxf_from_source(
            source_path=local_input,
            output_path=output_path,
            product_name=product_name,
            single_board_length=detected_length,
            single_board_width=detected_width,
            rail_width=rail_width,
            candidate=candidate
        )
        geometry_mode = "real_source_dxf_copied"

    except Exception as e:
        geometry_info = create_simple_panel_dxf_fallback(
            output_path=output_path,
            product_name=product_name,
            single_board_length=detected_length,
            single_board_width=detected_width,
            rail_width=rail_width,
            candidate=candidate
        )
        geometry_info["real_dxf_error"] = str(e)
        geometry_mode = "simple_fallback"

    try:
        os.remove(local_input)
    except Exception:
        pass

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

    minio_presigned_url = get_presigned_download_url(output_object_key, hours=24)
    api_download_url = make_public_api_download_url(output_object_key)

    panel_dxf = {
        "status": "success",
        "product_name": product_name,
        "input_object_key": object_key,
        "output_object_key": output_object_key,
        "output_filename": output_name,
        "download_url": api_download_url,
        "api_download_url": api_download_url,
        "minio_presigned_url": minio_presigned_url,
        "expires_hours": 24,
        "geometry_mode": geometry_mode,
        "geometry_info": geometry_info,
        "best_candidate": candidate,
        "display_candidates": result["display_candidates"],
        "all_candidates": result["all_candidates"],
        "candidates": result["candidates"],
        "message": "Panelized DXF generated by real DXF panelization engine."
    }

    report_text = build_ai_report_markdown(
        product_name=product_name,
        object_key=object_key,
        result=result,
        panel_dxf=panel_dxf
    )

    return {
        "report_text": report_text,
        "panel_dxf": panel_dxf,
        "panel_dxf_info": panel_dxf,
        "output_object_key": output_object_key,
        "output_filename": output_name,
        "download_url": api_download_url,
        "api_download_url": api_download_url,
        "minio_presigned_url": minio_presigned_url,
        "best_candidate": candidate,
        "display_candidates": result["display_candidates"],
        "all_candidates": result["all_candidates"],
        "candidates": result["candidates"],
        "geometry_info": geometry_info,
        "geometry_mode": geometry_mode
    }


def merge_local_outputs_into_dify_result(
    dify_result: Dict[str, Any],
    local_outputs: Dict[str, Any]
) -> Dict[str, Any]:

    if not isinstance(dify_result, dict):
        dify_result = {}

    if "data" not in dify_result or not isinstance(dify_result.get("data"), dict):
        dify_result["data"] = {}

    if "outputs" not in dify_result["data"] or not isinstance(dify_result["data"].get("outputs"), dict):
        dify_result["data"]["outputs"] = {}

    outputs = dify_result["data"]["outputs"]

    if not outputs.get("report_text"):
        outputs["report_text"] = local_outputs["report_text"]

    if not outputs.get("panel_dxf"):
        outputs["panel_dxf"] = local_outputs["panel_dxf"]

    if not outputs.get("panel_dxf_info"):
        outputs["panel_dxf_info"] = local_outputs["panel_dxf_info"]

    outputs["fallback_output_object_key"] = local_outputs["output_object_key"]
    outputs["fallback_output_filename"] = local_outputs["output_filename"]
    outputs["fallback_download_url"] = local_outputs["download_url"]
    outputs["geometry_mode"] = local_outputs["geometry_mode"]
    outputs["geometry_info"] = local_outputs["geometry_info"]
    outputs["display_candidates"] = local_outputs["display_candidates"]

    dify_result["data"]["outputs"] = outputs

    return dify_result


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
# API：候選方案
# 這支給 Dify Workflow 的 HTTP Request 節點使用
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

    detected_length = single_board_length
    detected_width = single_board_width

    try:
        source_doc = ezdxf.readfile(local_input)
        detected_bbox = get_modelspace_bbox(source_doc)
        if detected_bbox:
            min_x, min_y, max_x, max_y = detected_bbox
            detected_length = max_x - min_x
            detected_width = max_y - min_y
    except Exception:
        pass

    try:
        os.remove(local_input)
    except Exception:
        pass

    result = calculate_candidates(
        detected_length,
        detected_width,
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

    report_text = build_ai_report_markdown(
        product_name=product_name,
        object_key=object_key,
        result=result
    )

    return {
        "product_name": product_name,
        "object_key": object_key,
        "stage": "phase_2_real_dxf_panelization",
        "note": "已讀取 DXF bounding box，並產生所有候選連版方案；報告僅顯示前三個 display_candidates。",
        "detected_single_board": {
            "length_mm": round(detected_length, 3),
            "width_mm": round(detected_width, 3)
        },
        "input_single_board": {
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
        "display_candidates": result["display_candidates"],
        "all_candidates": result["all_candidates"],
        "candidates": result["candidates"],
        "recommended_candidates": result["recommended_candidates"],
        "caution_candidates": result["caution_candidates"],
        "not_recommended_candidates": result["not_recommended_candidates"],
        "comparison_table_markdown": build_comparison_table_markdown(result["all_candidates"], limit=3),
        "caution_and_not_recommended_summary": build_caution_and_not_summary(result["all_candidates"], limit=3),
        "not_recommended_summary": build_caution_and_not_summary(result["all_candidates"], limit=3),
        "report_text": report_text,
        "report_markdown": report_text
    }


# =========================================================
# API：產生真實連版 DXF
# 這支給 Dify Workflow 的 HTTP Request 節點使用
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
    inputs = {
        "object_key": object_key,
        "product_name": product_name,
        "single_board_length": str(single_board_length),
        "single_board_width": str(single_board_width),
        "rail_width": str(rail_width),
        "smt_max_length": str(smt_max_length),
        "smt_max_width": str(smt_max_width),
        "ict_max_length": str(ict_max_length),
        "ict_max_width": str(ict_max_width),
        "has_bga_qfn": has_bga_qfn,
        "has_dip": has_dip,
        "has_heavy_component": has_heavy_component,
        "is_irregular_shape": is_irregular_shape
    }

    outputs = generate_local_panelization_outputs(inputs)
    return outputs["panel_dxf"]


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
# 重點：本地會先產出真實連版 DXF，確保可下載
# =========================================================

def run_dify_job_background(job_id: str, inputs: Dict[str, str]):
    JOB_STORE[job_id]["status"] = "running"
    JOB_STORE[job_id]["message"] = "產生真實連版 DXF 並執行 Dify Workflow..."
    JOB_STORE[job_id]["started_at"] = datetime.utcnow().isoformat()

    local_outputs = None

    try:
        local_outputs = generate_local_panelization_outputs(inputs)

        if not DIFY_API_BASE or not DIFY_API_KEY:
            fallback_result = {
                "task_id": None,
                "workflow_run_id": None,
                "data": {
                    "status": "fallback_success",
                    "outputs": {
                        "report_text": local_outputs["report_text"],
                        "panel_dxf": local_outputs["panel_dxf"],
                        "panel_dxf_info": local_outputs["panel_dxf_info"],
                        "fallback_output_object_key": local_outputs["output_object_key"],
                        "fallback_output_filename": local_outputs["output_filename"],
                        "fallback_download_url": local_outputs["download_url"],
                        "geometry_mode": local_outputs["geometry_mode"],
                        "geometry_info": local_outputs["geometry_info"],
                        "display_candidates": local_outputs["display_candidates"]
                    },
                    "warning": "Dify API environment variables are not configured, but local real DXF generation succeeded."
                }
            }

            JOB_STORE[job_id]["status"] = "success"
            JOB_STORE[job_id]["message"] = "本地真實連版 DXF 已成功產生"
            JOB_STORE[job_id]["result"] = fallback_result
            JOB_STORE[job_id]["error"] = None
            JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()
            return

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
            fallback_result = {
                "task_id": None,
                "workflow_run_id": None,
                "data": {
                    "status": "fallback_success",
                    "outputs": {
                        "report_text": local_outputs["report_text"],
                        "panel_dxf": local_outputs["panel_dxf"],
                        "panel_dxf_info": local_outputs["panel_dxf_info"],
                        "fallback_output_object_key": local_outputs["output_object_key"],
                        "fallback_output_filename": local_outputs["output_filename"],
                        "fallback_download_url": local_outputs["download_url"],
                        "geometry_mode": local_outputs["geometry_mode"],
                        "geometry_info": local_outputs["geometry_info"],
                        "display_candidates": local_outputs["display_candidates"]
                    },
                    "warning": "Dify workflow API failed, but local real DXF generation succeeded.",
                    "dify_error": response.text
                }
            }

            JOB_STORE[job_id]["status"] = "success"
            JOB_STORE[job_id]["message"] = "Dify 失敗，但本地真實連版 DXF 已成功產生"
            JOB_STORE[job_id]["result"] = fallback_result
            JOB_STORE[job_id]["error"] = None
            JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()
            return

        content_type = response.headers.get("content-type", "")

        if "application/json" not in content_type:
            fallback_result = {
                "task_id": None,
                "workflow_run_id": None,
                "data": {
                    "status": "fallback_success",
                    "outputs": {
                        "report_text": local_outputs["report_text"],
                        "panel_dxf": local_outputs["panel_dxf"],
                        "panel_dxf_info": local_outputs["panel_dxf_info"],
                        "fallback_output_object_key": local_outputs["output_object_key"],
                        "fallback_output_filename": local_outputs["output_filename"],
                        "fallback_download_url": local_outputs["download_url"],
                        "geometry_mode": local_outputs["geometry_mode"],
                        "geometry_info": local_outputs["geometry_info"],
                        "display_candidates": local_outputs["display_candidates"]
                    },
                    "warning": "Dify returned non-JSON response, but local real DXF generation succeeded.",
                    "dify_response_preview": response.text[:1500]
                }
            }

            JOB_STORE[job_id]["status"] = "success"
            JOB_STORE[job_id]["message"] = "Dify 回傳非 JSON，但本地真實連版 DXF 已成功產生"
            JOB_STORE[job_id]["result"] = fallback_result
            JOB_STORE[job_id]["error"] = None
            JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()
            return

        result_json = response.json()

        dify_data = result_json.get("data", {})
        dify_status = dify_data.get("status", "")

        result_json = merge_local_outputs_into_dify_result(result_json, local_outputs)

        if dify_status == "failed":
            result_json["data"]["status"] = "fallback_success"
            result_json["data"]["warning"] = "Dify workflow failed, but local real DXF generation succeeded."
            result_json["data"]["original_dify_status"] = "failed"

        JOB_STORE[job_id]["status"] = "success"
        JOB_STORE[job_id]["message"] = "真實連版 DXF 已完成，Dify 結果已合併"
        JOB_STORE[job_id]["result"] = result_json
        JOB_STORE[job_id]["error"] = None
        JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()

    except Exception as e:
        JOB_STORE[job_id]["status"] = "failed"
        JOB_STORE[job_id]["message"] = "真實連版 DXF 產生失敗"
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
# API：查詢背景任務狀態
# =========================================================

@app.get("/api/pcb/job-status/{job_id}")
def get_job_status(job_id: str):
    job = JOB_STORE.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")

    return job


# =========================================================
# 舊版同步 API：現在也會產出真實連版 DXF
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

    local_outputs = generate_local_panelization_outputs(inputs)

    return {
        "status": "success",
        "message": "Synchronous API used real DXF panelization output.",
        "data": {
            "outputs": {
                "report_text": local_outputs["report_text"],
                "panel_dxf": local_outputs["panel_dxf"],
                "panel_dxf_info": local_outputs["panel_dxf_info"],
                "fallback_output_object_key": local_outputs["output_object_key"],
                "fallback_output_filename": local_outputs["output_filename"],
                "fallback_download_url": local_outputs["download_url"],
                "geometry_mode": local_outputs["geometry_mode"],
                "geometry_info": local_outputs["geometry_info"],
                "display_candidates": local_outputs["display_candidates"]
            }
        }
    }
