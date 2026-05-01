import os
import json
import uuid
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Tuple

import ezdxf
import boto3
from botocore.client import Config
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse


app = FastAPI(
    title="PCB DXF Panel AI API",
    description="Analyze PCB DXF and generate panelized DXF from MinIO.",
    version="1.0.0",
)


# =========================
# MinIO / S3 settings
# =========================

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "https://minio1-api.zeabur.app")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "pcb-dxf")


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def download_from_minio(object_key: str, local_path: str) -> None:
    s3 = get_s3_client()
    s3.download_file(MINIO_BUCKET, object_key, local_path)


def upload_to_minio(local_path: str, object_key: str) -> None:
    s3 = get_s3_client()
    s3.upload_file(local_path, MINIO_BUCKET, object_key)


def make_presigned_url(object_key: str, expires_in: int = 3600) -> str:
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": MINIO_BUCKET,
            "Key": object_key,
        },
        ExpiresIn=expires_in,
    )


# =========================
# DXF analysis helpers
# =========================

def collect_layers(doc) -> List[str]:
    return [layer.dxf.name for layer in doc.layers]


def entity_bbox_points(entity) -> List[Tuple[float, float]]:
    points = []

    try:
        etype = entity.dxftype()

        if etype == "LINE":
            points.append((float(entity.dxf.start.x), float(entity.dxf.start.y)))
            points.append((float(entity.dxf.end.x), float(entity.dxf.end.y)))

        elif etype == "LWPOLYLINE":
            for p in entity.get_points():
                points.append((float(p[0]), float(p[1])))

        elif etype == "POLYLINE":
            for v in entity.vertices:
                points.append((float(v.dxf.location.x), float(v.dxf.location.y)))

        elif etype == "CIRCLE":
            cx = float(entity.dxf.center.x)
            cy = float(entity.dxf.center.y)
            r = float(entity.dxf.radius)
            points.extend([(cx - r, cy - r), (cx + r, cy + r)])

        elif etype == "ARC":
            cx = float(entity.dxf.center.x)
            cy = float(entity.dxf.center.y)
            r = float(entity.dxf.radius)
            points.extend([(cx - r, cy - r), (cx + r, cy + r)])

    except Exception:
        pass

    return points


def find_outline_entities(doc):
    msp = doc.modelspace()
    layers = collect_layers(doc)

    preferred_layers = [
        "BOARD_OUTLINE",
        "PCB_OUTLINE",
        "OUTLINE",
        "BOARD",
        "PROFILE",
        "0",
    ]

    existing_preferred = [layer for layer in preferred_layers if layer in layers]

    if existing_preferred:
        entities = [
            e for e in msp
            if getattr(e.dxf, "layer", "") in existing_preferred
        ]
        if entities:
            return entities, existing_preferred

    return list(msp), ["ALL_MODELSPACE"]


def calculate_bbox(entities):
    xs = []
    ys = []

    for e in entities:
        for x, y in entity_bbox_points(e):
            xs.append(x)
            ys.append(y)

    if not xs or not ys:
        return None

    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)

    return {
        "min_x": round(min_x, 3),
        "max_x": round(max_x, 3),
        "min_y": round(min_y, 3),
        "max_y": round(max_y, 3),
        "width_mm": round(max_x - min_x, 3),
        "height_mm": round(max_y - min_y, 3),
    }


def analyze_dxf_file(dxf_path: str) -> Dict[str, Any]:
    doc = ezdxf.readfile(dxf_path)
    layers = collect_layers(doc)
    outline_entities, outline_layers = find_outline_entities(doc)
    bbox = calculate_bbox(outline_entities)

    if bbox is None:
        return {
            "parse_status": "failed",
            "error": "無法從 DXF 取得有效外框座標。請確認 DXF 內有 BOARD_OUTLINE / OUTLINE / PCB_OUTLINE 圖層。",
            "layers": layers,
        }

    layer_upper = [x.upper() for x in layers]

    has_keepout = any("KEEP" in x or "KEEPOUT" in x for x in layer_upper)
    has_edge_connector = any("EDGE" in x or "CONNECTOR" in x or "GOLD" in x for x in layer_upper)
    has_fiducial = any("FID" in x or "FIDUCIAL" in x or "MARK" in x for x in layer_upper)
    has_tooling = any("TOOL" in x or "HOLE" in x for x in layer_upper)

    risk_items = []

    if not has_fiducial:
        risk_items.append("未偵測到 Fiducial 圖層，建議在工藝邊新增 Fiducial。")

    if not has_tooling:
        risk_items.append("未偵測到 Tooling Hole 圖層，建議在工藝邊新增定位孔。")

    if has_edge_connector:
        risk_items.append("偵測到疑似 Edge Connector / 金手指相關圖層，V-cut 可行性需人工確認。")

    if not has_keepout:
        risk_items.append("未偵測到 Keep-out 圖層，Tab、Mouse Bite、V-cut 位置需人工確認避開零件與禁佈區。")

    return {
        "parse_status": "success",
        "file_name": Path(dxf_path).name,
        "unit": "mm",
        "board": {
            "outline_detected": True,
            "outline_layers_used": outline_layers,
            "min_x": bbox["min_x"],
            "max_x": bbox["max_x"],
            "min_y": bbox["min_y"],
            "max_y": bbox["max_y"],
            "width_mm": bbox["width_mm"],
            "height_mm": bbox["height_mm"],
            "shape": "rectangular_or_bbox_based",
        },
        "layers": layers,
        "features": {
            "has_keepout": has_keepout,
            "has_edge_connector": has_edge_connector,
            "has_existing_fiducial": has_fiducial,
            "has_existing_tooling_hole": has_tooling,
        },
        "available_methods": {
            "v_cut_possible": not has_edge_connector,
            "routing_possible": True,
            "routing_tab_possible": True,
        },
        "risk_items": risk_items,
    }


# =========================
# DXF generation helpers
# =========================

def ensure_layer(doc, name: str):
    if name not in doc.layers:
        doc.layers.add(name)


def add_rectangle(msp, x1, y1, x2, y2, layer):
    points = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    msp.add_lwpolyline(points, dxfattribs={"layer": layer})


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def generate_panel_dxf_file(input_path: str, strategy: Dict[str, Any], output_path: str) -> Dict[str, Any]:
    analysis = analyze_dxf_file(input_path)

    if analysis.get("parse_status") != "success":
        raise ValueError(f"DXF 分析失敗：{analysis.get('error')}")

    board = analysis["board"]
    board_w = safe_float(board["width_mm"])
    board_h = safe_float(board["height_mm"])

    layout = strategy.get("layout", {})

    rows = safe_int(layout.get("rows", 2), 2)
    columns = safe_int(layout.get("columns", 3), 3)
    gap_x = safe_float(layout.get("gap_x_mm", 3), 3)
    gap_y = safe_float(layout.get("gap_y_mm", 3), 3)

    rail_left = safe_float(layout.get("rail_left_mm", 5), 5)
    rail_right = safe_float(layout.get("rail_right_mm", 5), 5)
    rail_top = safe_float(layout.get("rail_top_mm", 5), 5)
    rail_bottom = safe_float(layout.get("rail_bottom_mm", 5), 5)

    panel_w = rail_left + columns * board_w + (columns - 1) * gap_x + rail_right
    panel_h = rail_bottom + rows * board_h + (rows - 1) * gap_y + rail_top

    recommended_method = strategy.get("recommended_method", "V_CUT")
    cutting = strategy.get("cutting_design", {})

    doc = ezdxf.new("R2010")
    doc.units = 4
    msp = doc.modelspace()

    for layer in [
        "PANEL_FRAME",
        "PCB_UNIT",
        "RAIL",
        "V_CUT_LINE",
        "ROUTING_PATH",
        "FIDUCIAL_NEW",
        "TOOLING_HOLE_NEW",
        "NOTE",
    ]:
        ensure_layer(doc, layer)

    add_rectangle(msp, 0, 0, panel_w, panel_h, "PANEL_FRAME")

    unit_positions = []

    for r in range(rows):
        for c in range(columns):
            x1 = rail_left + c * (board_w + gap_x)
            y1 = rail_bottom + r * (board_h + gap_y)
            x2 = x1 + board_w
            y2 = y1 + board_h
            unit_positions.append((x1, y1, x2, y2))
            add_rectangle(msp, x1, y1, x2, y2, "PCB_UNIT")

    if cutting.get("v_cut_lines", True) or recommended_method == "V_CUT":
        for c in range(1, columns):
            x = rail_left + c * board_w + (c - 0.5) * gap_x
            msp.add_line(
                (x, rail_bottom),
                (x, panel_h - rail_top),
                dxfattribs={"layer": "V_CUT_LINE"},
            )

        for r in range(1, rows):
            y = rail_bottom + r * board_h + (r - 0.5) * gap_y
            msp.add_line(
                (rail_left, y),
                (panel_w - rail_right, y),
                dxfattribs={"layer": "V_CUT_LINE"},
            )

    if cutting.get("routing_path", False) or recommended_method == "ROUTING_TAB":
        for x1, y1, x2, y2 in unit_positions:
            add_rectangle(msp, x1, y1, x2, y2, "ROUTING_PATH")

    fiducial_points = [
        (max(3, rail_left / 2), max(3, rail_bottom / 2)),
        (panel_w - max(3, rail_right / 2), max(3, rail_bottom / 2)),
        (max(3, rail_left / 2), panel_h - max(3, rail_top / 2)),
    ]

    for x, y in fiducial_points:
        msp.add_circle(
            (x, y),
            radius=1.0,
            dxfattribs={"layer": "FIDUCIAL_NEW"},
        )

    tooling_points = [
        (panel_w * 0.25, panel_h - max(3, rail_top / 2)),
        (panel_w * 0.75, panel_h - max(3, rail_top / 2)),
    ]

    for x, y in tooling_points:
        msp.add_circle(
            (x, y),
            radius=1.5,
            dxfattribs={"layer": "TOOLING_HOLE_NEW"},
        )

    note = (
        f"Panel: {rows}x{columns}, "
        f"Method: {recommended_method}, "
        f"Size: {round(panel_w, 3)} x {round(panel_h, 3)} mm"
    )

    msp.add_text(
        note,
        dxfattribs={"layer": "NOTE", "height": 3},
    ).set_placement((5, panel_h + 5))

    doc.saveas(output_path)

    return {
        "panel_width_mm": round(panel_w, 3),
        "panel_height_mm": round(panel_h, 3),
        "rows": rows,
        "columns": columns,
        "recommended_method": recommended_method,
    }


# =========================
# API endpoints
# =========================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "PCB DXF Panel AI API",
    }


@app.get("/")
async def root():
    return {
        "message": "PCB DXF Panel AI API is running.",
        "docs": "/docs",
        "endpoints": [
            "/analyze-dxf-minio",
            "/generate-panel-dxf-minio",
        ],
    }


@app.post("/analyze-dxf-minio")
async def analyze_dxf_minio(object_key: str = Form(...)):
    work_dir = tempfile.mkdtemp(prefix="dxf_minio_analyze_")

    try:
        local_dxf = os.path.join(work_dir, Path(object_key).name)
        download_from_minio(object_key, local_dxf)

        result = analyze_dxf_file(local_dxf)
        result["source_object_key"] = object_key

        return result

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "parse_status": "failed",
                "error": str(e),
                "source_object_key": object_key,
                "minio_endpoint": MINIO_ENDPOINT,
                "minio_bucket": MINIO_BUCKET,
            },
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.post("/generate-panel-dxf-minio")
async def generate_panel_dxf_minio(
    object_key: str = Form(...),
    strategy_json: str = Form(...),
):
    work_dir = tempfile.mkdtemp(prefix="dxf_minio_generate_")

    try:
        input_path = os.path.join(work_dir, Path(object_key).name)
        download_from_minio(object_key, input_path)

        strategy = json.loads(strategy_json)

        output_name = f"panel_{uuid.uuid4().hex[:8]}.dxf"
        output_path = os.path.join(work_dir, output_name)
        output_key = f"output/{output_name}"

        generate_result = generate_panel_dxf_file(input_path, strategy, output_path)
        upload_to_minio(output_path, output_key)

        download_url = make_presigned_url(output_key, expires_in=3600)

        return {
            "status": "success",
            "input_object_key": object_key,
            "output_object_key": output_key,
            "download_url": download_url,
            "panel": {
                "width_mm": generate_result["panel_width_mm"],
                "height_mm": generate_result["panel_height_mm"],
                "rows": generate_result["rows"],
                "columns": generate_result["columns"],
                "recommended_method": generate_result["recommended_method"],
            },
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "failed",
                "error": str(e),
                "input_object_key": object_key,
                "minio_endpoint": MINIO_ENDPOINT,
                "minio_bucket": MINIO_BUCKET,
            },
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
