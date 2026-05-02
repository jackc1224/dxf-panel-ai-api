from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Tuple
import os
import tempfile
import uuid

app = FastAPI(title="PCB Panelization API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
            reasons.append(f"Panel 長度 {panel_length:.1f} mm 超過 SMT 最大長度 {smt_max_length:.1f} mm")
            risk_score += 100

        if panel_width > smt_max_width:
            reasons.append(f"Panel 寬度 {panel_width:.1f} mm 超過 SMT 最大寬度 {smt_max_width:.1f} mm")
            risk_score += 100

        if ict_max_length > 0 and ict_max_width > 0:
            if panel_length > ict_max_length or panel_width > ict_max_width:
                reasons.append("Panel 尺寸可能超過 ICT 治具限制")
                risk_score += 30

        aspect_ratio = max(panel_length, panel_width) / min(panel_length, panel_width)

        if aspect_ratio > 3:
            reasons.append(f"Panel 長寬比 {aspect_ratio:.2f} 過大，可能有板彎或輸送不穩風險")
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
            "split_method": split_method,
            "risk_score": risk_score,
            "status": status,
            "reasons": reasons
        })

    recommended = sorted(
        [c for c in candidates if c["status"] == "recommended"],
        key=lambda x: (-x["pcs_per_panel"], x["risk_score"], x["panel_length_mm"] * x["panel_width_mm"])
    )

    best_candidate = recommended[0] if recommended else sorted(
        candidates,
        key=lambda x: (x["risk_score"], -x["pcs_per_panel"])
    )[0]

    return {
        "best_candidate": best_candidate,
        "candidates": candidates
    }


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
4
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

    # Panel outline
    content += dxf_rect(0, 0, panel_length, panel_width, "PANEL")

    # Board rectangles
    for i in range(x_count):
        for j in range(y_count):
            x = rail_width + i * single_board_length
            y = rail_width + j * single_board_width
            content += dxf_rect(x, y, single_board_length, single_board_width, "BOARD")

    # V-cut lines
    for i in range(1, x_count):
        x = rail_width + i * single_board_length
        content += dxf_line(x, rail_width, x, panel_width - rail_width, "VCUT")

    for j in range(1, y_count):
        y = rail_width + j * single_board_width
        content += dxf_line(rail_width, y, panel_length - rail_width, y, "VCUT")

    # Tooling holes
    hole_r = 1.6
    offset = rail_width / 2
    content += dxf_circle(offset, offset, hole_r, "TOOLING")
    content += dxf_circle(panel_length - offset, offset, hole_r, "TOOLING")
    content += dxf_circle(offset, panel_width - offset, hole_r, "TOOLING")
    content += dxf_circle(panel_length - offset, panel_width - offset, hole_r, "TOOLING")

    # Fiducials
    fid_r = 0.75
    content += dxf_circle(rail_width, rail_width, fid_r, "FIDUCIAL")
    content += dxf_circle(panel_length - rail_width, rail_width, fid_r, "FIDUCIAL")
    content += dxf_circle(rail_width, panel_width - rail_width, fid_r, "FIDUCIAL")

    content += dxf_footer()

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "PCB Panelization API",
        "version": "0.1.0",
        "message": "Use /docs to test API."
    }


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
    has_bga_qfn: bool = Form(False),
    has_dip: bool = Form(False),
    has_heavy_component: bool = Form(False),
    is_irregular_shape: bool = Form(False)
):
    result = calculate_candidates(
        single_board_length,
        single_board_width,
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

    return {
        "product_name": product_name,
        "input_file": dxf_file.filename,
        "stage": "phase_1_dxf_mvp",
        "note": "第一階段：根據輸入尺寸與製程限制，產生連版候選方案。",
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
        "candidates": result["candidates"]
    }


@app.post("/api/pcb/generate-panel-dxf")
async def generate_panel_dxf(
    dxf_file: UploadFile = File(...),
    product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...),
    single_board_width: float = Form(...),
    rail_width: float = Form(5.0),
    smt_max_length: float = Form(330.0),
    smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0),
    ict_max_width: float = Form(300.0),
    has_bga_qfn: bool = Form(False),
    has_dip: bool = Form(False),
    has_heavy_component: bool = Form(False),
    is_irregular_shape: bool = Form(False)
):
    result = calculate_candidates(
        single_board_length,
        single_board_width,
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

    safe_name = product_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
    output_name = f"panelized_{safe_name}_{candidate['panel_type']}.dxf"
    output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{output_name}")

    create_panel_dxf(
        output_path,
        product_name,
        single_board_length,
        single_board_width,
        rail_width,
        candidate
    )

    return FileResponse(
        output_path,
        media_type="application/dxf",
        filename=output_name
    )
