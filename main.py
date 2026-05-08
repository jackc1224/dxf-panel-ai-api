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
import json
import re

from minio import Minio
from minio.error import S3Error

import ezdxf
from ezdxf import bbox
from ezdxf.math import Matrix44


app = FastAPI(title="PCB Panelization API", version="0.9.5-me-template-edge-profile")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# 公司連板設計規範：程式內建規則庫
# =========================================================

PANEL_RULES = {
    "version": "company_panel_rules_v1.5_me_template_edge_profile",
    "source": "program_rule_base",

    "panel_size": {
        "absolute_max_length_mm": 510.0,
        "absolute_max_width_mm": 360.0,
        "juki_min_length_mm": 50.0,
        "juki_min_width_mm": 50.0,
        "juki_max_length_mm": 510.0,
        "juki_max_width_mm": 360.0,
        "smt_magazine_length_mm": 253.0,
        "smt_magazine_width_mm": 350.0,
    },

    "layout": {
        "candidate_patterns": [
            (1, 1), (1, 2), (2, 1), (2, 2), (3, 1),
            (1, 3), (3, 2), (2, 3), (4, 1), (1, 4),
        ],
        "preferred_aspect_ratio": 1.5,
        "near_square_min": 1.0,
        "near_square_max": 1.25,
        "caution_aspect_ratio": 3.0,
        "not_recommended_aspect_ratio": 4.0,
        "max_gap_between_boards_mm": 30.0,
    },

    "rail": {
        "company_recommended_min_mm": 8.0,
        "company_recommended_max_mm": 10.0,
        "default_mm": 8.0,
        "minimum_warning_mm": 4.0,
        "f_line_keepout_mm": 10.0,
        "four_side_required_when_tooling": True,
        "two_side_allowed_for_simple_vcut": True,
        "template_note": "依連板圖模板，若需 Tooling Hole / Fiducial / Router Tab，優先採四邊工藝邊；簡單 V-CUT 且無治具需求時才允許兩側工藝邊。",
    },

    "fiducial": {
        "panel_count": 3,
        "diameter_mm": 1.5,
        "strict_checklist_diameter_mm": 1.5,
        "clearance_mm": 5.0,
        "positions": ["top-left", "top-right", "bottom-left"],
        "internal_min_count": 2,
        "template_callout": "3-Ø1.5 Fiducial Mark",
    },

    "tooling_hole": {
        "count": 4,
        "diameter_mm": 3.05,
        "optional_jig_diameter_mm": 3.05,
        "clearance_mm": 5.0,
        "positions": ["bottom-left", "bottom-right", "top-left", "top-right"],
        "poka_yoke_offset_mm": 1.0,
        "template_callout": "4-Ø3.05 ±0.05 Tooling hole",
    },

    "tab": {
        "tab_length_mm": 7.0,
        "tab_width_mm": 2.0,
        "route_gap_mm": 2.0,
        "edge_tab_enabled": True,
        "edge_tab_count_per_board_edge": 2,
        "edge_tab_note": "板與板之間及 PCB edge side 與工藝邊交界處皆規劃 Router / Tab 連接點；外側板邊採白色凸榫型 ROUTE 輪廓線，Tab 從 PCB 邊緣凸出進入工藝邊，ROUTE 各連板間距至少 2 mm，需由 ME/CAM 確認分板應力。",
    },

    "dimension": {
        "leader_enabled": True,
        "leader_text_height": 1.8,
        "dimension_text_height": 1.8,
        "callout_text_height": 1.8,
        "dimension_offset_mm": 8.0,
        "dimension_stack_gap_mm": 6.0,
        "hole_position_reference_x_mm": 5.0,
        "hole_position_reference_y_mm": 10.0,
        "tooling_to_fiducial_distance_mm": 7.70,
        "tooling_callout_offset_x_mm": 9.0,
        "fiducial_callout_offset_y_mm": 5.0,
    },

    "white_marking": {
        "size_mm": "4.5 x 30 mm 或 5 x 30 mm",
        "count": 4,
        "description": "板邊正反面右側及對角需增加白框不塗滿 / 雷雕框，ODM 產品需特別確認。",
    },

    "cutting": {
        "vcut_component_distance_mm": 2.0,
        "vcut_height_check_distance_mm": 5.0,
        "vcut_max_height_within_5mm_mm": 10.0,
        "vcut_depth": "PCB 兩邊各 1/3",
        "vcut_angle_deg": 30,
        "router_min_gap_mm": 2.0,
        "router_tool_diameter_mm": 1.5,
        "connector_clearance_mm": 3.0,
        "mlcc_edge_distance_mm": 5.0,
        "router_high_part_rule": "ROUTE 正面朝上時，連接點 1 cm 內不得有 1.5 cm 高零件；1.5 cm 內不得有 3 cm 高零件。",
    },

    "risk_score": {
        "exceed_absolute_max": 1000,
        "below_min_size": 100,
        "exceed_smt_input": 100,
        "exceed_ict_input": 30,
        "near_square": 15,
        "aspect_gt_3": 25,
        "aspect_gt_4": 100,
        "irregular_shape": 15,
        "bga_qfn": 15,
        "dip": 10,
        "heavy_component": 10,
        "vcut_conflict": 100,
        "mlcc_near_edge_vcut": 100,
    },

    "check_items": [
        "Gerber 轉 DXF 尺寸比例是否為 1:1。",
        "單板尺寸與連板尺寸是否標示清楚。",
        "Panel 長寬是否超過最大 510 x 360 mm 或客戶 / 設備輸入限制。",
        "Panel 尺寸是否低於最小 50 x 50 mm。",
        "板內是否至少有 2 點光學點分布於對角。",
        "Panel 光學點與工具孔距離板邊是否至少 5 mm。",
        "板邊是否有 4 點固定點分布於對角並具防呆設計。",
        "板邊是否有 3 點光學點分布於對角並具防呆設計。",
        "板邊與連接點需以白色線規劃，並確認 Tab 位置與切點間距。",
        "是否加註 4.5 x 30 mm 或 5 x 30 mm 白框不塗滿 / 雷雕框。",
        "雙面零件排版是否有做讓位，零件側是否朝外避免上件干涉。",
        "零件是否重疊板邊，是否確認無干涉。",
        "MLCC 0805 以上距板邊 5 mm 以內時，是否改用 Router 製程。",
        "金手指周圍是否避免連接點與 V-CUT 線。",
        "排版設計 CONN 是否朝外側；RJ45 等 CONN 是否配合波峰焊方向。",
        "單板之間距離是否未超過 30 mm。",
        "是否避免陰陽板；若使用陰陽板，是否有註明原因。",
        "SMT F 線大尺寸 PCB 10 mm 內是否未放工具孔與光學點。",
        "V-CUT 線路徑是否無干涉零件。",
        "零件與 V-CUT 線距離是否大於 2 mm。",
        "V-CUT 線邊 5 mm 內零件高度是否小於等於 10 mm。",
        "V-CUT 深度是否為 PCB 兩邊各 1/3，開槽角度是否 30 度。",
        "ROUTE 切點間距是否至少 2 mm 以上。",
        "連接點是否取消郵票孔或 V-CUT 線。",
        "圓弧或斜線切點是否加註白線。",
        "連接點與凸出零件是否相距至少 3 mm。",
        "ROUTE 正面朝上設計是否符合高件距離限制。",
        "ODM EV/DV 階段可用郵票孔，MV 階段是否避免郵票孔。",
        "正式投產前是否由 ME / CAM 工程師確認。",
    ],
}


# =========================================================
# MinIO / Dify 環境變數
# =========================================================

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "pcb-dxf")
MINIO_SECURE = os.getenv("MINIO_SECURE", "true").lower() == "true"

DIFY_API_BASE = os.getenv("DIFY_API_BASE", "")
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "")

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
    return v in ["yes", "true", "1", "是", "y"]


def safe_filename_name(filename: str) -> str:
    return (
        filename
        .replace("/", "_").replace("\\", "_").replace(" ", "_")
        .replace(":", "_").replace("*", "_").replace("?", "_")
        .replace('"', "_").replace("<", "_").replace(">", "_").replace("|", "_")
    )


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def status_to_zh(status_code: str) -> str:
    mapping = {"recommended": "建議", "use_with_caution": "謹慎使用", "not_recommended": "不建議"}
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
    status_priority = {"recommended": 0, "use_with_caution": 1, "not_recommended": 2}

    def sort_key(c):
        aspect_delta = abs(float(c.get("aspect_ratio", 99)) - PANEL_RULES["layout"]["preferred_aspect_ratio"])
        return (
            status_priority.get(c.get("status_code", ""), 9),
            -int(c.get("pcs_per_panel", 0)),
            int(c.get("risk_score", 9999)),
            aspect_delta,
            float(c.get("panel_length_mm", 9999)) * float(c.get("panel_width_mm", 9999)),
        )

    return sorted(candidates, key=sort_key)[:limit]


# =========================================================
# strategy_json 解析與程式規則策略
# =========================================================

def parse_strategy_json(strategy_json: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not strategy_json or not str(strategy_json).strip():
        return None, None

    raw = str(strategy_json).strip()
    raw = raw.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed, None
        return None, "strategy_json parsed but is not an object"
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, None
            return None, "extracted strategy_json is not an object"
        except Exception as e:
            return None, f"json.loads extracted object failed: {str(e)}"

    return None, "strategy_json is not valid JSON"


def determine_base_split_method(has_bga_qfn: bool, has_heavy_component: bool, is_irregular_shape: bool) -> str:
    if is_irregular_shape or has_bga_qfn or has_heavy_component:
        return "Router / Tab"
    return "V-cut"


def determine_rail_policy(
    split_method: str,
    has_bga_qfn: bool,
    has_dip: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
    force_tooling_hole: bool = True,
) -> str:
    if force_tooling_hole:
        return "four_sides"
    if "Router" in split_method or "Tab" in split_method:
        return "four_sides"
    if has_bga_qfn or has_dip or has_heavy_component or is_irregular_shape:
        return "four_sides"
    return "two_sides"


def sanitize_strategy(strategy: dict) -> dict:
    if not isinstance(strategy, dict):
        strategy = {}

    layout = strategy.get("layout") if isinstance(strategy.get("layout"), dict) else {}
    fid = strategy.get("fiducial_rule") if isinstance(strategy.get("fiducial_rule"), dict) else {}
    hole = strategy.get("tooling_hole_rule") if isinstance(strategy.get("tooling_hole_rule"), dict) else {}
    cutting = strategy.get("cutting_rule") if isinstance(strategy.get("cutting_rule"), dict) else {}

    method = cutting.get("method") or strategy.get("recommended_method") or "Router / Tab"
    if method not in ["V-cut", "V-CUT", "Router / Tab"]:
        method = "Router / Tab"
    if method == "V-CUT":
        method = "V-cut"

    default_gap = PANEL_RULES["cutting"]["router_min_gap_mm"] if method == "Router / Tab" else 0.0

    def sf(d, k, v):
        try:
            return float(d.get(k, v))
        except Exception:
            return v

    def si(d, k, v):
        try:
            return int(float(d.get(k, v)))
        except Exception:
            return v

    return {
        "strategy_source": strategy.get("strategy_source") or PANEL_RULES["source"],
        "recommended_method": method,
        "layout": {
            "columns": max(1, si(layout, "columns", 1)),
            "rows": max(1, si(layout, "rows", 1)),
            "gap_x_mm": max(PANEL_RULES["cutting"]["router_min_gap_mm"] if method == "Router / Tab" else 0.0, sf(layout, "gap_x_mm", default_gap)),
            "gap_y_mm": max(PANEL_RULES["cutting"]["router_min_gap_mm"] if method == "Router / Tab" else 0.0, sf(layout, "gap_y_mm", default_gap)),
            "rail_left_mm": max(PANEL_RULES["rail"]["minimum_warning_mm"], sf(layout, "rail_left_mm", PANEL_RULES["rail"]["default_mm"])),
            "rail_right_mm": max(PANEL_RULES["rail"]["minimum_warning_mm"], sf(layout, "rail_right_mm", PANEL_RULES["rail"]["default_mm"])),
            "rail_top_mm": max(PANEL_RULES["rail"]["minimum_warning_mm"], sf(layout, "rail_top_mm", PANEL_RULES["rail"]["default_mm"])),
            "rail_bottom_mm": max(PANEL_RULES["rail"]["minimum_warning_mm"], sf(layout, "rail_bottom_mm", PANEL_RULES["rail"]["default_mm"])),
        },
        "fiducial_rule": {
            "count": max(2, si(fid, "count", PANEL_RULES["fiducial"]["panel_count"])),
            "diameter_mm": sf(fid, "diameter_mm", PANEL_RULES["fiducial"]["diameter_mm"]),
            "clearance_mm": max(PANEL_RULES["fiducial"]["clearance_mm"], sf(fid, "clearance_mm", PANEL_RULES["fiducial"]["clearance_mm"])),
            "positions": fid.get("positions") if isinstance(fid.get("positions"), list) else PANEL_RULES["fiducial"]["positions"],
            "reason": fid.get("reason") or "依程式內建公司連板規範，Panel 光學點採三點對角防呆配置。",
        },
        "tooling_hole_rule": {
            "count": max(4, si(hole, "count", PANEL_RULES["tooling_hole"]["count"])),
            "diameter_mm": sf(hole, "diameter_mm", PANEL_RULES["tooling_hole"]["diameter_mm"]),
            "clearance_mm": max(PANEL_RULES["tooling_hole"]["clearance_mm"], sf(hole, "clearance_mm", PANEL_RULES["tooling_hole"]["clearance_mm"])),
            "positions": hole.get("positions") if isinstance(hole.get("positions"), list) else PANEL_RULES["tooling_hole"]["positions"],
            "reason": hole.get("reason") or "依程式內建公司連板規範，固定點採四點分布於對角四邊，並需由 ME/CAM 確認防呆距離。",
        },
        "cutting_rule": {
            "method": method,
            "reason": cutting.get("reason") or "依程式內建公司連板規範，異形板、BGA/QFN 或重零件優先採 Router / Tab。",
        },
        "risk_items": strategy.get("risk_items") if isinstance(strategy.get("risk_items"), list) else [],
        "me_cam_check_items": strategy.get("me_cam_check_items") if isinstance(strategy.get("me_cam_check_items"), list) else PANEL_RULES["check_items"],
    }


def build_program_rule_strategy(
    best_candidate: dict,
    has_bga_qfn: bool,
    has_dip: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
    input_rail_width: float,
) -> dict:
    method = determine_base_split_method(has_bga_qfn=has_bga_qfn, has_heavy_component=has_heavy_component, is_irregular_shape=is_irregular_shape)
    gap = PANEL_RULES["cutting"]["router_min_gap_mm"] if method == "Router / Tab" else 0.0
    rail = max(input_rail_width, PANEL_RULES["rail"]["company_recommended_min_mm"])
    rail_policy = determine_rail_policy(method, has_bga_qfn, has_dip, has_heavy_component, is_irregular_shape, force_tooling_hole=True)

    risk_items = [
        "光學點及工具孔需距離板邊 5 mm 以上。",
        "單板之間距離不得超過 30 mm，避免 SMT Sensor 誤判。",
        "板邊、連接點、光學點與定位點需以白色線規劃。",
        "板邊需判斷兩側或四側；若需治具定位、Fiducial 或 Router Tab，採四邊工藝邊。",
        "連板圖面需標註單板尺寸、連板尺寸與連板數。",
        "MLCC 0805 以上若距板邊 5 mm 以內，應使用 Router 製程。",
        "金手指周圍不可設計連接點和 V-CUT 線。",
        "雙面零件排版需做讓位，零件側應朝外避免上件干涉。",
        "正式投產前需由 ME / CAM 工程師確認 V-CUT、Router、Fiducial、Tooling Hole、Tab 連接點與分板應力。",
    ]

    if method == "Router / Tab":
        risk_items.append("ROUTE 各切點間距至少 2 mm 以上，因銑刀直徑約 1.5 mm。")
        risk_items.append("Router / Tab 時，連接點不可同時作郵票孔和 V-CUT 線。")
    else:
        risk_items += [
            "V-CUT 線路徑上不得有干涉零件。",
            "零件與 V-CUT 線距離需大於 2 mm。",
            "V-CUT 線邊 5 mm 內零件高度不可超過 10 mm。",
            "V-CUT 深度需為 PCB 兩邊各 1/3，開槽角度需 30 度。",
        ]

    if has_bga_qfn:
        risk_items.append("有 BGA/QFN，需確認元件距分板邊界與 Fiducial 的安全距離，避免分板應力造成焊點裂紋。")
    if has_dip:
        risk_items.append("有 DIP，需確認波峰焊方向、錫流方向與治具需求。")
    if has_heavy_component:
        risk_items.append("有重零件，需評估過爐板彎、支撐治具與分板應力。")
    if is_irregular_shape:
        risk_items.append("異形板不建議直接使用 V-CUT，建議採 Router / Tab。")

    strategy = {
        "strategy_source": PANEL_RULES["source"],
        "recommended_method": method,
        "rail_policy": rail_policy,
        "layout": {
            "columns": int(best_candidate.get("columns", best_candidate.get("x_count", 1))),
            "rows": int(best_candidate.get("rows", best_candidate.get("y_count", 1))),
            "gap_x_mm": gap,
            "gap_y_mm": gap,
            "rail_left_mm": rail,
            "rail_right_mm": rail,
            "rail_top_mm": rail if rail_policy == "four_sides" else 0.0,
            "rail_bottom_mm": rail if rail_policy == "four_sides" else 0.0,
        },
        "fiducial_rule": {
            "count": PANEL_RULES["fiducial"]["panel_count"],
            "diameter_mm": PANEL_RULES["fiducial"]["diameter_mm"],
            "clearance_mm": PANEL_RULES["fiducial"]["clearance_mm"],
            "positions": PANEL_RULES["fiducial"]["positions"],
            "reason": "依程式內建公司連板規範，Panel 光學點採 3 點分布於對角並具防呆設計。",
        },
        "tooling_hole_rule": {
            "count": PANEL_RULES["tooling_hole"]["count"],
            "diameter_mm": PANEL_RULES["tooling_hole"]["diameter_mm"],
            "clearance_mm": PANEL_RULES["tooling_hole"]["clearance_mm"],
            "positions": PANEL_RULES["tooling_hole"]["positions"],
            "reason": "依程式內建公司連板規範，固定點採 4 點分布於對角四邊，並由 ME/CAM 確認防呆距離。",
        },
        "cutting_rule": {
            "method": method,
            "reason": "依程式內建公司連板規範，異形板、BGA/QFN 或重零件優先採 Router / Tab；一般矩形板且無高風險元件時才可評估 V-CUT。",
        },
        "risk_items": risk_items,
        "me_cam_check_items": PANEL_RULES["check_items"],
    }

    return sanitize_strategy(strategy)


def get_strategy_for_backend(
    strategy_json: str,
    best_candidate: dict,
    has_bga_qfn: bool,
    has_dip: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
    input_rail_width: float,
) -> Tuple[dict, bool, Optional[str]]:
    parsed_strategy, parse_error = parse_strategy_json(strategy_json)

    if parsed_strategy:
        strategy = sanitize_strategy(parsed_strategy)
        if not strategy.get("strategy_source"):
            strategy["strategy_source"] = "dify_strategy_json"
        return strategy, True, None

    program_strategy = build_program_rule_strategy(
        best_candidate=best_candidate,
        has_bga_qfn=has_bga_qfn,
        has_dip=has_dip,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
        input_rail_width=input_rail_width,
    )

    return program_strategy, True, parse_error


def apply_strategy_to_candidate(
    base_candidate: Dict[str, Any],
    strategy: Optional[Dict[str, Any]],
    board_w: float,
    board_h: float,
    default_rail_width: float,
) -> Dict[str, Any]:
    candidate = dict(base_candidate)

    if not strategy:
        return candidate

    layout = strategy.get("layout", {})
    cutting_rule = strategy.get("cutting_rule", {})

    columns = max(1, to_int(layout.get("columns", candidate.get("columns", 1)), 1))
    rows = max(1, to_int(layout.get("rows", candidate.get("rows", 1)), 1))
    gap_x = max(0.0, to_float(layout.get("gap_x_mm", candidate.get("gap_x_mm", 0.0)), 0.0))
    gap_y = max(0.0, to_float(layout.get("gap_y_mm", candidate.get("gap_y_mm", 0.0)), 0.0))
    rail_left = max(0.0, to_float(layout.get("rail_left_mm", default_rail_width), default_rail_width))
    rail_right = max(0.0, to_float(layout.get("rail_right_mm", default_rail_width), default_rail_width))
    rail_top = max(0.0, to_float(layout.get("rail_top_mm", default_rail_width), default_rail_width))
    rail_bottom = max(0.0, to_float(layout.get("rail_bottom_mm", default_rail_width), default_rail_width))

    split_method = cutting_rule.get("method") or strategy.get("recommended_method") or candidate.get("split_method", "V-cut")

    if "Router" in str(split_method) or "Tab" in str(split_method):
        min_route_gap = PANEL_RULES["cutting"]["router_min_gap_mm"]
        gap_x = max(gap_x, min_route_gap)
        gap_y = max(gap_y, min_route_gap)

    panel_w = rail_left + columns * board_w + max(columns - 1, 0) * gap_x + rail_right
    panel_h = rail_bottom + rows * board_h + max(rows - 1, 0) * gap_y + rail_top
    aspect_ratio = max(panel_w, panel_h) / max(min(panel_w, panel_h), 0.001)

    candidate.update({
        "panel_type": f"{columns}x{rows}",
        "x_count": columns, "y_count": rows,
        "columns": columns, "rows": rows,
        "gap_x_mm": gap_x, "gap_y_mm": gap_y,
        "rail_left_mm": rail_left, "rail_right_mm": rail_right,
        "rail_top_mm": rail_top, "rail_bottom_mm": rail_bottom,
        "panel_length_mm": round(panel_w, 2),
        "panel_width_mm": round(panel_h, 2),
        "panel_size": f"{panel_w:.1f} x {panel_h:.1f} mm",
        "pcs_per_panel": columns * rows,
        "aspect_ratio": round(aspect_ratio, 2),
        "split_method": split_method,
        "rail_policy": strategy.get("rail_policy", "four_sides"),
        "strategy_override_applied": True,
        "strategy_source": strategy.get("strategy_source", PANEL_RULES["source"]),
        "rule_source": PANEL_RULES["source"],
    })

    return candidate


def update_result_with_strategy_candidate(result: Dict[str, Any], strategy_candidate: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(result)
    all_candidates = list(result.get("all_candidates", []))
    updated["best_candidate"] = strategy_candidate
    filtered = [c for c in all_candidates if c.get("panel_type") != strategy_candidate.get("panel_type")]
    updated["display_candidates"] = [strategy_candidate] + select_display_candidates(filtered, limit=2)
    updated["all_candidates"] = all_candidates
    updated["candidates"] = all_candidates
    return updated


# =========================================================
# MinIO 工具
# =========================================================

def get_minio_client() -> Minio:
    if not MINIO_ENDPOINT:
        raise HTTPException(status_code=500, detail="MINIO_ENDPOINT is not configured")
    if not MINIO_ACCESS_KEY:
        raise HTTPException(status_code=500, detail="MINIO_ACCESS_KEY is not configured")
    if not MINIO_SECRET_KEY:
        raise HTTPException(status_code=500, detail="MINIO_SECRET_KEY is not configured")
    endpoint = MINIO_ENDPOINT.replace("https://", "").replace("http://", "")
    return Minio(endpoint=endpoint, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)


def ensure_bucket_exists():
    client = get_minio_client()
    try:
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"MinIO bucket error: {str(e)}")


def upload_file_to_minio(local_path: str, object_name: str, content_type: str = "application/dxf"):
    ensure_bucket_exists()
    client = get_minio_client()
    try:
        client.fput_object(bucket_name=MINIO_BUCKET, object_name=object_name, file_path=local_path, content_type=content_type)
    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"MinIO upload error: {str(e)}")


def download_file_from_minio(object_name: str, local_path: str):
    ensure_bucket_exists()
    client = get_minio_client()
    try:
        client.fget_object(bucket_name=MINIO_BUCKET, object_name=object_name, file_path=local_path)
    except S3Error as e:
        raise HTTPException(status_code=404, detail=f"MinIO object not found or download failed: {str(e)}")


def get_presigned_download_url(object_name: str, hours: int = 24) -> str:
    ensure_bucket_exists()
    client = get_minio_client()
    try:
        return client.presigned_get_object(bucket_name=MINIO_BUCKET, object_name=object_name, expires=timedelta(hours=hours))
    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"MinIO presigned url error: {str(e)}")


# =========================================================
# 基礎 API
# =========================================================

@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "PCB Panelization API",
        "version": "0.9.5-me-template-edge-profile",
        "program_rules_applied_by_default": True,
        "rule_version": PANEL_RULES["version"],
        "message": "Company panelization rules are embedded in backend.",
    }


@app.get("/api/panel-rules")
def get_panel_rules():
    return PANEL_RULES


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
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
        return {"status": "ok", "message": "MinIO connection success", "bucket": MINIO_BUCKET, "endpoint": MINIO_ENDPOINT, "secure": MINIO_SECURE, "bucket_created_or_exists": True}
    except Exception as e:
        return {"status": "error", "message": "MinIO connection failed", "endpoint": MINIO_ENDPOINT, "secure": MINIO_SECURE, "bucket": MINIO_BUCKET, "error_type": type(e).__name__, "error_detail": str(e)}


@app.get("/api/health/dify")
def health_dify():
    return {
        "status": "ok" if DIFY_API_BASE and DIFY_API_KEY else "error",
        "DIFY_API_BASE_configured": bool(DIFY_API_BASE),
        "DIFY_API_KEY_configured": bool(DIFY_API_KEY),
        "DIFY_API_BASE": DIFY_API_BASE,
        "expected_workflow_run_url": f"{DIFY_API_BASE.rstrip('/')}/workflows/run" if DIFY_API_BASE else "",
    }


# =========================================================
# 候選方案
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
    is_irregular_shape: bool,
) -> dict:

    candidates = []
    split_method = determine_base_split_method(has_bga_qfn=has_bga_qfn, has_heavy_component=has_heavy_component, is_irregular_shape=is_irregular_shape)
    rail_policy = determine_rail_policy(split_method=split_method, has_bga_qfn=has_bga_qfn, has_dip=has_dip, has_heavy_component=has_heavy_component, is_irregular_shape=is_irregular_shape, force_tooling_hole=True)
    gap = PANEL_RULES["cutting"]["router_min_gap_mm"] if split_method == "Router / Tab" else 0.0
    rail = max(rail_width, PANEL_RULES["rail"]["company_recommended_min_mm"])

    for x_count, y_count in PANEL_RULES["layout"]["candidate_patterns"]:
        rail_left = rail
        rail_right = rail
        rail_top = rail if rail_policy == "four_sides" else 0.0
        rail_bottom = rail if rail_policy == "four_sides" else 0.0

        panel_length = rail_left + single_board_length * x_count + gap * max(x_count - 1, 0) + rail_right
        panel_width = rail_bottom + single_board_width * y_count + gap * max(y_count - 1, 0) + rail_top
        pcs_per_panel = x_count * y_count
        aspect_ratio = max(panel_length, panel_width) / max(min(panel_length, panel_width), 0.001)

        reasons = []
        risk_score = 0

        if panel_length > PANEL_RULES["panel_size"]["absolute_max_length_mm"] or panel_width > PANEL_RULES["panel_size"]["absolute_max_width_mm"]:
            reasons.append("Panel 尺寸超過公司最大連板尺寸 510 x 360 mm，不可推薦。")
            risk_score += PANEL_RULES["risk_score"]["exceed_absolute_max"]
        if panel_length < PANEL_RULES["panel_size"]["juki_min_length_mm"] or panel_width < PANEL_RULES["panel_size"]["juki_min_width_mm"]:
            reasons.append("Panel 尺寸低於 JUKI / SMT 最小 50 x 50 mm，可能無法穩定輸送。")
            risk_score += PANEL_RULES["risk_score"]["below_min_size"]
        if panel_length > smt_max_length:
            reasons.append(f"Panel 長度 {panel_length:.1f} mm 超過 SMT 最大長度 {smt_max_length:.1f} mm。")
            risk_score += PANEL_RULES["risk_score"]["exceed_smt_input"]
        if panel_width > smt_max_width:
            reasons.append(f"Panel 寬度 {panel_width:.1f} mm 超過 SMT 最大寬度 {smt_max_width:.1f} mm。")
            risk_score += PANEL_RULES["risk_score"]["exceed_smt_input"]
        if ict_max_length > 0 and ict_max_width > 0:
            if panel_length > ict_max_length or panel_width > ict_max_width:
                reasons.append(f"Panel 尺寸 {panel_length:.1f} x {panel_width:.1f} mm 可能超過 ICT 治具限制 {ict_max_length:.1f} x {ict_max_width:.1f} mm。")
                risk_score += PANEL_RULES["risk_score"]["exceed_ict_input"]
        if PANEL_RULES["layout"]["near_square_min"] <= aspect_ratio <= PANEL_RULES["layout"]["near_square_max"]:
            reasons.append("Panel 長寬比趨近正方形，依公司規範需謹慎評估板彎，建議接近 3:2。")
            risk_score += PANEL_RULES["risk_score"]["near_square"]
        if aspect_ratio > PANEL_RULES["layout"]["not_recommended_aspect_ratio"]:
            reasons.append(f"Panel 長寬比 {aspect_ratio:.2f} 大於 4.0，板彎或輸送不穩風險高。")
            risk_score += PANEL_RULES["risk_score"]["aspect_gt_4"]
        elif aspect_ratio > PANEL_RULES["layout"]["caution_aspect_ratio"]:
            reasons.append(f"Panel 長寬比 {aspect_ratio:.2f} 大於 3.0，可能有板彎或輸送不穩風險。")
            risk_score += PANEL_RULES["risk_score"]["aspect_gt_3"]
        if is_irregular_shape:
            reasons.append("異形板不建議直接使用 V-CUT，建議 Router / Tab。")
            risk_score += PANEL_RULES["risk_score"]["irregular_shape"]
        if has_bga_qfn:
            reasons.append("有 BGA/QFN，需確認距 V-CUT 或 Router 邊界安全距離，避免分板應力。")
            risk_score += PANEL_RULES["risk_score"]["bga_qfn"]
        if has_dip:
            reasons.append("有 DIP，需確認波峰焊方向、錫流方向與治具需求。")
            risk_score += PANEL_RULES["risk_score"]["dip"]
        if has_heavy_component:
            reasons.append("有重零件，需評估過爐板彎、支撐治具與分板應力。")
            risk_score += PANEL_RULES["risk_score"]["heavy_component"]
        if rail_width < PANEL_RULES["rail"]["company_recommended_min_mm"]:
            reasons.append(f"輸入工藝邊 {rail_width:.1f} mm 低於公司建議 8~10 mm，本系統已以 {rail:.1f} mm 進行保守設計。")
        reasons.append(f"板邊策略：{rail_policy}；若需 Tooling Hole / Fiducial / Router Tab，採四邊工藝邊。")

        if risk_score >= 100:
            status_code = "not_recommended"
        elif risk_score >= 40:
            status_code = "use_with_caution"
        else:
            status_code = "recommended"

        candidates.append({
            "panel_type": f"{x_count}x{y_count}",
            "x_count": x_count, "y_count": y_count,
            "columns": x_count, "rows": y_count,
            "gap_x_mm": gap, "gap_y_mm": gap,
            "rail_left_mm": rail_left, "rail_right_mm": rail_right,
            "rail_top_mm": rail_top, "rail_bottom_mm": rail_bottom,
            "rail_policy": rail_policy,
            "panel_length_mm": round(panel_length, 2),
            "panel_width_mm": round(panel_width, 2),
            "panel_size": f"{panel_length:.1f} x {panel_width:.1f} mm",
            "pcs_per_panel": pcs_per_panel,
            "aspect_ratio": round(aspect_ratio, 2),
            "split_method": split_method,
            "risk_score": int(risk_score),
            "risk_level_zh": risk_level_zh(int(risk_score)),
            "status_code": status_code,
            "status": status_to_zh(status_code),
            "status_zh": status_to_zh(status_code),
            "reasons": reasons,
            "reason_text": reasons_to_text(reasons),
            "rule_source": PANEL_RULES["source"],
        })

    display_candidates = select_display_candidates(candidates, limit=3)
    recommended_candidates = [c for c in candidates if c["status_code"] == "recommended"]
    caution_candidates = [c for c in candidates if c["status_code"] == "use_with_caution"]
    not_recommended_candidates = [c for c in candidates if c["status_code"] == "not_recommended"]

    if recommended_candidates:
        best_candidate = select_display_candidates(recommended_candidates, limit=1)[0]
    elif caution_candidates:
        best_candidate = select_display_candidates(caution_candidates, limit=1)[0]
    else:
        best_candidate = select_display_candidates(not_recommended_candidates, limit=1)[0]

    return {
        "best_candidate": best_candidate,
        "display_candidates": display_candidates,
        "candidates": candidates,
        "all_candidates": candidates,
        "recommended_candidates": recommended_candidates,
        "caution_candidates": caution_candidates,
        "not_recommended_candidates": not_recommended_candidates,
        "rule_source": PANEL_RULES["source"],
        "rule_version": PANEL_RULES["version"],
    }


def build_comparison_table_markdown(candidates: List[Dict[str, Any]], limit: int = 3) -> str:
    display_candidates = select_display_candidates(candidates, limit=limit)
    lines = ["| 方案 | Panel 尺寸 | pcs/panel | 分板方式 | 板邊策略 | 風險分數 | 風險等級 | 狀態 | 建議原因 |", "|---|---:|---:|---|---|---:|---|---|---|"]
    for c in display_candidates:
        lines.append(f"| {c['panel_type']} | {c['panel_size']} | {c['pcs_per_panel']} | {c['split_method']} | {c.get('rail_policy', 'four_sides')} | {c['risk_score']} | {c['risk_level_zh']} | {c['status_zh']} | {c['reason_text']} |")
    return "\n".join(lines)


def build_caution_and_not_summary(candidates: List[Dict[str, Any]], limit: int = 3) -> str:
    display_candidates = select_display_candidates(candidates, limit=limit)
    items = [c for c in display_candidates if c["status_code"] in ["use_with_caution", "not_recommended"]]
    if not items:
        return "本次顯示的前三個候選方案皆為建議方案，未出現謹慎使用或不建議方案。"
    return "\n".join([f"- 方案 {c['panel_type']}：{c['status_zh']}，原因：{c['reason_text']}" for c in items])


# =========================================================
# DXF 幾何工具
# =========================================================

def get_modelspace_bbox(doc) -> Optional[Tuple[float, float, float, float]]:
    try:
        msp = doc.modelspace()
        ext = bbox.extents(msp, fast=True)
        if not ext.has_data:
            return None
        min_x, min_y = float(ext.extmin.x), float(ext.extmin.y)
        max_x, max_y = float(ext.extmax.x), float(ext.extmax.y)
        if max_x <= min_x or max_y <= min_y:
            return None
        return min_x, min_y, max_x, max_y
    except Exception:
        return None


def ensure_layer(doc, name: str, color: int = 7):
    try:
        if name not in doc.layers:
            doc.layers.add(name=name, color=color)
        else:
            doc.layers.get(name).dxf.color = color
    except Exception:
        pass


def transform_entity_safe(entity, matrix: Matrix44) -> bool:
    try:
        entity.transform(matrix)
        return True
    except Exception:
        return False


def add_lwpolyline_rect(msp, x: float, y: float, w: float, h: float, layer: str):
    msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)], dxfattribs={"layer": layer, "closed": True})


def add_lwpolyline_open(msp, points: List[Tuple[float, float]], layer: str):
    msp.add_lwpolyline(points, dxfattribs={"layer": layer, "closed": False})


def add_line(msp, x1: float, y1: float, x2: float, y2: float, layer: str):
    msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer})


def add_circle(msp, x: float, y: float, r: float, layer: str):
    msp.add_circle(center=(x, y), radius=r, dxfattribs={"layer": layer})


def add_text(msp, text: str, x: float, y: float, height: float, layer: str):
    try:
        msp.add_text(text, dxfattribs={"layer": layer, "height": height, "insert": (x, y)})
    except Exception:
        pass


def add_leader_callout(msp, start_x, start_y, elbow_x, elbow_y, text_x, text_y, text, layer="PANEL_DIMENSION", text_height=1.8):
    add_line(msp, start_x, start_y, elbow_x, elbow_y, layer)
    add_line(msp, elbow_x, elbow_y, text_x - 1.0, text_y, layer)
    add_line(msp, text_x - 1.0, text_y, text_x + max(35.0, len(text) * text_height * 1.7), text_y, layer)
    add_text(msp, text, text_x, text_y + 0.8, text_height, layer)


def add_dimension_line(msp, x1, y1, x2, y2, text, text_x, text_y, layer="PANEL_DIMENSION", text_height=1.8, extension=3.0):
    add_line(msp, x1, y1, x2, y2, layer)
    if abs(y2 - y1) < abs(x2 - x1):
        add_line(msp, x1, y1 - extension, x1, y1 + extension, layer)
        add_line(msp, x2, y2 - extension, x2, y2 + extension, layer)
    else:
        add_line(msp, x1 - extension, y1, x1 + extension, y1, layer)
        add_line(msp, x2 - extension, y2, x2 + extension, y2, layer)
    add_text(msp, text, text_x, text_y, text_height, layer)


def position_to_xy(position: str, panel_w: float, panel_h: float, margin: float) -> Tuple[float, float]:
    p = str(position).strip().lower()
    mapping = {
        "bottom-left": (margin, margin), "bottom-right": (panel_w - margin, margin),
        "top-left": (margin, panel_h - margin), "top-right": (panel_w - margin, panel_h - margin),
        "left-top": (margin, panel_h - margin), "right-top": (panel_w - margin, panel_h - margin),
        "left-bottom": (margin, margin), "right-bottom": (panel_w - margin, margin),
        "center-left": (margin, panel_h / 2.0), "center-right": (panel_w - margin, panel_h / 2.0),
        "top-center": (panel_w / 2.0, panel_h - margin), "bottom-center": (panel_w / 2.0, margin),
        "center": (panel_w / 2.0, panel_h / 2.0),
    }
    return mapping.get(p, (margin, margin))


def normalize_positions(positions: Any, default_positions: List[str], count: int) -> List[str]:
    output = [str(p) for p in positions if str(p).strip()] if isinstance(positions, list) else []
    if not output:
        output = list(default_positions)
    while len(output) < count:
        for p in default_positions:
            if len(output) >= count:
                break
            output.append(p)
    return output[:count]


def auto_determine_panel_features(
    panel_w, panel_h, rail_width, columns, rows, split_method,
    has_bga_qfn=False, has_dip=False, has_heavy_component=False,
    is_irregular_shape=False, strategy=None,
) -> Dict[str, Any]:

    warnings_list: List[str] = []
    notes: List[str] = []

    fiducial_rule = strategy.get("fiducial_rule", {}) if strategy and isinstance(strategy.get("fiducial_rule"), dict) else {}
    tooling_hole_rule = strategy.get("tooling_hole_rule", {}) if strategy and isinstance(strategy.get("tooling_hole_rule"), dict) else {}

    fid_d = to_float(fiducial_rule.get("diameter_mm", PANEL_RULES["fiducial"]["diameter_mm"]), PANEL_RULES["fiducial"]["diameter_mm"])
    fid_r = fid_d / 2.0
    fid_cnt = max(3, to_int(fiducial_rule.get("count", PANEL_RULES["fiducial"]["panel_count"]), PANEL_RULES["fiducial"]["panel_count"]))
    fid_cl = max(PANEL_RULES["fiducial"]["clearance_mm"], to_float(fiducial_rule.get("clearance_mm", PANEL_RULES["fiducial"]["clearance_mm"]), PANEL_RULES["fiducial"]["clearance_mm"]))

    th_d = to_float(tooling_hole_rule.get("diameter_mm", PANEL_RULES["tooling_hole"]["diameter_mm"]), PANEL_RULES["tooling_hole"]["diameter_mm"])
    th_r = th_d / 2.0
    th_cnt = max(4, to_int(tooling_hole_rule.get("count", PANEL_RULES["tooling_hole"]["count"]), PANEL_RULES["tooling_hole"]["count"]))
    th_cl = max(PANEL_RULES["tooling_hole"]["clearance_mm"], to_float(tooling_hole_rule.get("clearance_mm", PANEL_RULES["tooling_hole"]["clearance_mm"]), PANEL_RULES["tooling_hole"]["clearance_mm"]))

    th_margin = min(max(th_cl, rail_width / 2.0), max(panel_w / 4.0, 1.0), max(panel_h / 4.0, 1.0))
    fid_margin = min(max(fid_cl, rail_width / 2.0), max(panel_w / 4.0, 1.0), max(panel_h / 4.0, 1.0))

    if rail_width < PANEL_RULES["rail"]["company_recommended_min_mm"]:
        warnings_list.append(f"工藝邊 {rail_width:.1f} mm 低於公司建議 8~10 mm，請 ME/CAM 確認是否需加寬。")
    if rail_width < fid_cl:
        warnings_list.append(f"工藝邊 {rail_width:.1f} mm 小於 Fiducial clearance {fid_cl:.1f} mm。")
    if rail_width < th_cl:
        warnings_list.append(f"工藝邊 {rail_width:.1f} mm 小於 Tooling Hole clearance {th_cl:.1f} mm。")
    if has_bga_qfn:
        notes.append("有 BGA/QFN，需確認 Fiducial 是否足以支援高精度貼裝，並確認分板邊界安全距離。")
    if has_dip:
        notes.append("有 DIP，Tooling Hole 與治具定位需確認不干涉波峰焊治具與錫流方向。")
    if has_heavy_component:
        notes.append("有重零件，需確認定位孔與支撐治具是否足以降低過爐板彎。")
    if is_irregular_shape or "Router" in split_method or "Tab" in split_method:
        notes.append("異形板或 Router / Tab 分板時，Fiducial 與 Tooling Hole 優先放在 Panel 工藝邊。")

    th_positions = normalize_positions(tooling_hole_rule.get("positions"), PANEL_RULES["tooling_hole"]["positions"], th_cnt)
    fid_positions = normalize_positions(fiducial_rule.get("positions"), PANEL_RULES["fiducial"]["positions"], fid_cnt)

    tooling_holes = []
    for i, pos in enumerate(th_positions):
        x, y = position_to_xy(pos, panel_w, panel_h, th_margin)
        tooling_holes.append({"name": f"TH{i+1}", "x": round(x, 3), "y": round(y, 3), "diameter_mm": th_d, "radius_mm": th_r, "location": pos})

    fiducials = []
    for i, pos in enumerate(fid_positions):
        x, y = position_to_xy(pos, panel_w, panel_h, fid_margin)
        fiducials.append({"name": f"FD{i+1}", "x": round(x, 3), "y": round(y, 3), "diameter_mm": fid_d, "radius_mm": fid_r, "location": pos})

    return {
        "rule_version": "company_panel_feature_rule_v1.4_me_template_detail_edge_routertab",
        "strategy_used": bool(strategy),
        "rule_source": PANEL_RULES["source"],
        "fiducials": fiducials,
        "tooling_holes": tooling_holes,
        "fiducial_count": len(fiducials),
        "tooling_hole_count": len(tooling_holes),
        "fiducial_diameter_mm": fid_d,
        "tooling_hole_diameter_mm": th_d,
        "fiducial_rule_from_strategy": fiducial_rule,
        "tooling_hole_rule_from_strategy": tooling_hole_rule,
        "warnings": warnings_list,
        "notes": notes,
        "rule_summary": (
            f"依程式內建公司連板規範產生：Panel 光學點 {len(fiducials)} 點，直徑 {fid_d:.2f} mm；"
            f"Tooling Hole {len(tooling_holes)} 點，直徑 {th_d:.2f} mm；距板邊 clearance 不低於 5.0 mm。"
        ),
    }


def draw_panel_features(msp, feature_result, tooling_layer="PANEL_TOOLING", fiducial_layer="PANEL_FIDUCIAL", text_layer="PANEL_TEXT"):
    for hole in feature_result.get("tooling_holes", []):
        x, y, r = float(hole["x"]), float(hole["y"]), float(hole["radius_mm"])
        add_circle(msp, x, y, r, tooling_layer)
        add_text(msp, str(hole["name"]), x + r + 0.8, y + r + 0.8, 1.5, text_layer)
    for fid in feature_result.get("fiducials", []):
        x, y, r = float(fid["x"]), float(fid["y"]), float(fid["radius_mm"])
        add_circle(msp, x, y, r, fiducial_layer)
        add_text(msp, str(fid["name"]), x + r + 0.8, y + r + 0.8, 1.5, text_layer)


def draw_edge_tabs(msp, panel_x0, panel_y0, rail_left, rail_bottom, board_w, board_h, pitch_x, pitch_y, columns, rows, tab_length, tab_width, layer="PANEL_TAB"):
    for row in range(rows):
        for col in range(columns):
            bx = panel_x0 + rail_left + col * pitch_x
            by = panel_y0 + rail_bottom + row * pitch_y
            for ratio in [0.30, 0.70]:
                y = by + board_h * ratio
                add_lwpolyline_rect(msp, bx - tab_width / 2.0, y - tab_length / 2.0, tab_width, tab_length, layer)
                add_lwpolyline_rect(msp, bx + board_w - tab_width / 2.0, y - tab_length / 2.0, tab_width, tab_length, layer)
            for ratio in [0.30, 0.70]:
                x = bx + board_w * ratio
                add_lwpolyline_rect(msp, x - tab_length / 2.0, by - tab_width / 2.0, tab_length, tab_width, layer)
                add_lwpolyline_rect(msp, x - tab_length / 2.0, by + board_h - tab_width / 2.0, tab_length, tab_width, layer)


# =========================================================
# ★ MODIFIED: draw_outer_edge_tabs
#
# 修改說明（對照圖片）：
#   圖片中白色線條的 Tab 形狀為「凸榫型」：
#   - Tab 從 PCB 本體邊緣凸出，延伸「進入」工藝邊區域
#   - 底部：⊓ 型（開口朝上，嵌入 PCB 下邊緣缺口）
#   - 頂部：∪ 型（開口朝下，嵌入 PCB 上邊緣缺口）
#   - 左側：⊏ 型（開口朝右，嵌入 PCB 左邊緣缺口）
#   - 右側：⊐ 型（開口朝左，嵌入 PCB 右邊緣缺口）
#   另於 Tab 根部（PCB 邊緣側）加切點間距標記線
# =========================================================

def draw_outer_edge_tabs(
    msp,
    panel_x0: float,
    panel_y0: float,
    rail_left: float,
    rail_bottom: float,
    board_w: float,
    board_h: float,
    pitch_x: float,
    pitch_y: float,
    columns: int,
    rows: int,
    tab_length: float,
    tab_width: float,
    layer: str = "PANEL_EDGE_PROFILE",
):
    """
    PCB edge side Router / Tab 外側板邊連接點（凸榫型，對照圖片白色線設計）。

    設計邏輯：
    - 黃色線 = PCB 本體邊框（原始 DXF，程式不重畫）
    - 白色線 = 工藝邊框 + Tab 凸榫輪廓

    Tab 幾何（以底部工藝邊為例）：
      PCB 下邊緣 y = by_bottom，Tab 中心 x = cx
      Tab 向下凸入工藝邊 vertical_depth mm
      三段白色線形成 ⊓ 型：
        左側邊：(cx-half, by_bottom) → (cx-half, by_bottom - depth)
        底部橫：(cx-half, by_bottom - depth) → (cx+half, by_bottom - depth)
        右側邊：(cx+half, by_bottom - depth) → (cx+half, by_bottom)
      切點間距標記線（距 PCB 邊緣 tab_width 處，表示 Router 切點最小間距）
    """

    min_gap = max(PANEL_RULES["cutting"]["router_min_gap_mm"], 2.0)
    tab_w = max(tab_width, min_gap)
    tab_l = max(tab_length, 7.0)

    # Tab 凸入工藝邊深度：至少 4 mm，不超過工藝邊寬度 60%（避免貫穿）
    v_depth = max(4.0, min(rail_bottom * 0.6, 6.0))
    h_depth = max(4.0, min(rail_left * 0.6, 6.0))

    def draw_bottom_tab(cx: float, edge_y: float):
        """底部：⊓ 型，開口朝上（朝 PCB 方向）"""
        half = tab_l / 2.0
        y_tip = edge_y - v_depth
        add_line(msp, cx - half, edge_y,  cx - half, y_tip,  layer)
        add_line(msp, cx - half, y_tip,   cx + half, y_tip,  layer)
        add_line(msp, cx + half, y_tip,   cx + half, edge_y, layer)
        # 切點間距標記線
        add_line(msp, cx - half, edge_y - tab_w, cx + half, edge_y - tab_w, layer)

    def draw_top_tab(cx: float, edge_y: float):
        """頂部：∪ 型，開口朝下（朝 PCB 方向）"""
        half = tab_l / 2.0
        y_tip = edge_y + v_depth
        add_line(msp, cx - half, edge_y,  cx - half, y_tip,  layer)
        add_line(msp, cx - half, y_tip,   cx + half, y_tip,  layer)
        add_line(msp, cx + half, y_tip,   cx + half, edge_y, layer)
        add_line(msp, cx - half, edge_y + tab_w, cx + half, edge_y + tab_w, layer)

    def draw_left_tab(edge_x: float, cy: float):
        """左側：⊏ 型，開口朝右（朝 PCB 方向）"""
        half = tab_l / 2.0
        x_tip = edge_x - h_depth
        add_line(msp, edge_x, cy - half, x_tip, cy - half, layer)
        add_line(msp, x_tip,  cy - half, x_tip, cy + half, layer)
        add_line(msp, x_tip,  cy + half, edge_x, cy + half, layer)
        add_line(msp, edge_x - tab_w, cy - half, edge_x - tab_w, cy + half, layer)

    def draw_right_tab(edge_x: float, cy: float):
        """右側：⊐ 型，開口朝左（朝 PCB 方向）"""
        half = tab_l / 2.0
        x_tip = edge_x + h_depth
        add_line(msp, edge_x, cy - half, x_tip, cy - half, layer)
        add_line(msp, x_tip,  cy - half, x_tip, cy + half, layer)
        add_line(msp, x_tip,  cy + half, edge_x, cy + half, layer)
        add_line(msp, edge_x + tab_w, cy - half, edge_x + tab_w, cy + half, layer)

    # 底部外側：第一列 PCB 下邊緣與下工藝邊交界
    by_bottom = panel_y0 + rail_bottom
    for col in range(columns):
        bx = panel_x0 + rail_left + col * pitch_x
        for ratio in [0.30, 0.70]:
            draw_bottom_tab(bx + board_w * ratio, by_bottom)

    # 頂部外側：最後一列 PCB 上邊緣與上工藝邊交界
    by_top = panel_y0 + rail_bottom + (rows - 1) * pitch_y + board_h
    for col in range(columns):
        bx = panel_x0 + rail_left + col * pitch_x
        for ratio in [0.30, 0.70]:
            draw_top_tab(bx + board_w * ratio, by_top)

    # 左側外側：第一欄 PCB 左邊緣與左工藝邊交界
    bx_left = panel_x0 + rail_left
    for row in range(rows):
        by = panel_y0 + rail_bottom + row * pitch_y
        for ratio in [0.30, 0.70]:
            draw_left_tab(bx_left, by + board_h * ratio)

    # 右側外側：最後一欄 PCB 右邊緣與右工藝邊交界
    bx_right = panel_x0 + rail_left + (columns - 1) * pitch_x + board_w
    for row in range(rows):
        by = panel_y0 + rail_bottom + row * pitch_y
        for ratio in [0.30, 0.70]:
            draw_right_tab(bx_right, by + board_h * ratio)


# =========================================================
# 報告文字
# =========================================================

def build_feature_summary_text(panel_features: Dict[str, Any]) -> str:
    if not panel_features:
        return "尚未取得 Fiducial / Tooling Hole 自動配置資訊。"
    fiducials = panel_features.get("fiducials", [])
    tooling_holes = panel_features.get("tooling_holes", [])
    warnings_list = panel_features.get("warnings", [])
    notes = panel_features.get("notes", [])
    fid_lines = [f"- {f.get('name')}：({f.get('x')}, {f.get('y')}) mm，直徑 {f.get('diameter_mm')} mm，位置 {f.get('location')}" for f in fiducials]
    tooling_lines = [f"- {h.get('name')}：({h.get('x')}, {h.get('y')}) mm，直徑 {h.get('diameter_mm')} mm，位置 {h.get('location')}" for h in tooling_holes]
    return f"""
配置規則：{panel_features.get("rule_summary", "")}

Fiducial 配置：
{chr(10).join(fid_lines) if fid_lines else "- 無"}

Tooling Hole 配置：
{chr(10).join(tooling_lines) if tooling_lines else "- 無"}

注意事項：
{chr(10).join(["- " + n for n in notes]) if notes else "- 依公司連板規範配置"}

警告：
{chr(10).join(["- " + w for w in warnings_list]) if warnings_list else "- 無重大警告"}
""".strip()


def build_strategy_summary_text(strategy, strategy_applied, strategy_parse_error):
    if strategy_applied and strategy:
        return f"""
- 策略套用狀態：已套用
- 策略來源：{strategy.get("strategy_source", PANEL_RULES["source"])}
- 程式規則版本：{PANEL_RULES["version"]}
- 板邊策略：{strategy.get("rail_policy", "four_sides")}
- 建議分板方式：{strategy.get("recommended_method", "")}
- Layout：{json.dumps(strategy.get("layout", {}), ensure_ascii=False)}
- Fiducial Rule：{json.dumps(strategy.get("fiducial_rule", {}), ensure_ascii=False)}
- Tooling Hole Rule：{json.dumps(strategy.get("tooling_hole_rule", {}), ensure_ascii=False)}
""".strip()
    if strategy_parse_error:
        return f"- 策略套用狀態：已改用程式內建公司規範\n- Dify strategy_json 解析錯誤：{strategy_parse_error}"
    return "- 策略套用狀態：已套用程式內建公司規範"


def build_ai_report_markdown(product_name, object_key, result, panel_dxf=None, strategy=None, strategy_applied=False, strategy_parse_error=None):
    best = result["best_candidate"]
    comparison_table = build_comparison_table_markdown(result.get("display_candidates", result["all_candidates"]), limit=3)
    caution_text = build_caution_and_not_summary(result.get("display_candidates", result["all_candidates"]), limit=3)
    dxf_info = ""
    feature_text = ""
    if panel_dxf:
        geometry_info = panel_dxf.get("geometry_info", {}) or {}
        panel_features = geometry_info.get("panel_features", {}) or {}
        feature_text = build_feature_summary_text(panel_features)
        dxf_info = f"""
- 輸出 DXF 檔名：{panel_dxf.get("output_filename", "")}
- 輸出 object_key：{panel_dxf.get("output_object_key", "")}
- 下載連結：{panel_dxf.get("download_url", "")}
- 幾何產出模式：{panel_dxf.get("geometry_mode", "")}
- 程式規則版本：{panel_dxf.get("program_rule_version", "")}
- 策略來源：{panel_dxf.get("strategy_source", "")}
""".strip()
    strategy_text = build_strategy_summary_text(strategy, strategy_applied, strategy_parse_error)
    report = f"""
# PCB 連版規劃 AI 建議報告

## 一、AI 建議結論
- 產品名稱：{product_name}
- 原始 DXF object_key：{object_key}
- 建議連版方式：{best["panel_type"]}
- 建議 Panel 尺寸：{best["panel_size"]}
- 每 Panel 數量：{best["pcs_per_panel"]} pcs/panel
- 建議分板方式：{best["split_method"]}
- 板邊策略：{best.get("rail_policy", "four_sides")}
- 風險分數：{best["risk_score"]}
- 風險等級：{best["risk_level_zh"]}
- 狀態：{best["status_zh"]}
- 是否可進入下一階段：{"可進入下一階段，但仍需 ME / CAM 工程師確認" if best["status_code"] == "recommended" else "需先由 ME / CAM 工程師審查後再決定"}

## 二、前三個候選方案比較表
{comparison_table}

## 三、公司連板規範套用說明
{strategy_text}

## 四、推薦方案說明
本次系統推薦方案為 {best["panel_type"]}，Panel 尺寸為 {best["panel_size"]}，每 Panel 可生產 {best["pcs_per_panel"]} pcs，建議分板方式為 {best["split_method"]}。風險分數 {best["risk_score"]}，風險等級「{best["risk_level_zh"]}」，狀態「{best["status_zh"]}」。主要判斷原因：{best["reason_text"]}

## 五、謹慎使用與不建議方案原因
{caution_text}

## 六、DXF 輸出資訊
{dxf_info if dxf_info else "DXF 已由系統產生，請於下載連結取得。"}

## 七、Fiducial / Tooling Hole 自動配置
{feature_text if feature_text else "尚未取得 Fiducial / Tooling Hole 配置資訊。"}

## 八、製程風險提醒
- 光學點及工具孔需距離板邊 5 mm 以上。
- 板邊需判斷兩側或四側；若需治具定位、Fiducial 或 Router Tab，採四邊工藝邊。
- 板邊、連接點、光學點與定位點需以白色線規劃。
- 板邊建議 8～10 mm，若輸入值較小，需由 ME/CAM 確認。
- ROUTE 切點間距至少 2 mm 以上。
- 零件與 V-CUT 線距離需大於 2 mm。
- V-CUT 線邊 5 mm 內，零件高度不可超過 10 mm。
- MLCC 0805 以上若距板邊 5 mm 內，應使用 Router 製程。
- 金手指周圍不可設計連接點和 V-CUT 線。

## 九、ME / CAM 最終確認清單
{chr(10).join(["- " + item for item in PANEL_RULES["check_items"]])}

## 十、輸出限制說明
本階段輸出的 DXF 為 AI 建議版，正式投產前仍需 ME / CAM 工程師確認。
"""
    return report.strip()


# =========================================================
# DXF 產生
# =========================================================

def create_real_panel_dxf_from_source(
    source_path, output_path, product_name,
    single_board_length, single_board_width, rail_width,
    candidate, has_bga_qfn=False, has_dip=False,
    has_heavy_component=False, is_irregular_shape=False, strategy=None,
) -> Dict[str, Any]:

    doc = ezdxf.readfile(source_path)
    msp = doc.modelspace()

    white_layers = [
        "PANEL_OUTLINE", "PANEL_VCUT", "PANEL_ROUTE", "PANEL_TAB",
        "PANEL_TOOLING", "PANEL_FIDUCIAL", "PANEL_TEXT", "PANEL_NOTE",
        "PANEL_WARNING", "PANEL_DIMENSION", "PANEL_FRAME", "PANEL_TITLE",
        "PANEL_RAIL", "PANEL_LEADER", "PANEL_EDGE_PROFILE",
    ]
    for ln in white_layers:
        ensure_layer(doc, ln, 7)

    detected_bbox = get_modelspace_bbox(doc)
    if detected_bbox:
        min_x, min_y, max_x, max_y = detected_bbox
        board_w = max_x - min_x or single_board_length
        board_h = max_y - min_y or single_board_width
    else:
        min_x, min_y = 0.0, 0.0
        board_w, board_h = single_board_length, single_board_width

    columns = int(candidate.get("x_count", candidate.get("columns", 1)))
    rows = int(candidate.get("y_count", candidate.get("rows", 1)))
    split_method = str(candidate.get("split_method", "Router / Tab"))

    rail_policy = str(strategy.get("rail_policy", candidate.get("rail_policy", "four_sides"))) if strategy else str(candidate.get("rail_policy", "four_sides"))
    if rail_policy not in ["four_sides", "two_sides"]:
        rail_policy = "four_sides"

    gap_x = float(candidate.get("gap_x_mm", PANEL_RULES["cutting"]["router_min_gap_mm"]))
    gap_y = float(candidate.get("gap_y_mm", PANEL_RULES["cutting"]["router_min_gap_mm"]))
    if "Router" in split_method or "Tab" in split_method:
        gap_x = max(gap_x, PANEL_RULES["cutting"]["router_min_gap_mm"])
        gap_y = max(gap_y, PANEL_RULES["cutting"]["router_min_gap_mm"])

    min_rail = PANEL_RULES["rail"]["company_recommended_min_mm"]
    rail_left  = max(float(candidate.get("rail_left_mm",  PANEL_RULES["rail"]["default_mm"])), min_rail)
    rail_right = max(float(candidate.get("rail_right_mm", PANEL_RULES["rail"]["default_mm"])), min_rail)
    rail_top    = max(float(candidate.get("rail_top_mm",    PANEL_RULES["rail"]["default_mm"])), min_rail) if rail_policy == "four_sides" else 0.0
    rail_bottom = max(float(candidate.get("rail_bottom_mm", PANEL_RULES["rail"]["default_mm"])), min_rail) if rail_policy == "four_sides" else 0.0

    pitch_x = board_w + gap_x
    pitch_y = board_h + gap_y
    panel_w = rail_left + columns * board_w + max(columns - 1, 0) * gap_x + rail_right
    panel_h = rail_bottom + rows * board_h + max(rows - 1, 0) * gap_y + rail_top
    pcs_per_panel = columns * rows

    frame_w = max(panel_w + 190.0, 340.0)
    frame_h = max(panel_h + 155.0, 240.0)
    frame_x0, frame_y0 = 0.0, 0.0
    frame_x1, frame_y1 = frame_x0 + frame_w, frame_y0 + frame_h
    inner_margin = 8.0
    bottom_title_height = 32.0

    panel_x0 = frame_x0 + (frame_w - panel_w) / 2.0 - 12.0
    panel_y0 = frame_y0 + bottom_title_height + 34.0

    original_entities = list(msp)

    # 圖框
    add_lwpolyline_rect(msp, frame_x0, frame_y0, frame_w, frame_h, "PANEL_FRAME")
    add_lwpolyline_rect(msp, frame_x0 + inner_margin, frame_y0 + inner_margin, frame_w - inner_margin * 2, frame_h - inner_margin * 2, "PANEL_FRAME")
    usable_w = frame_w - inner_margin * 2
    usable_h = frame_h - inner_margin * 2
    for i in range(1, 14):
        x = frame_x0 + inner_margin + i * usable_w / 14
        add_line(msp, x, frame_y0 + inner_margin, x, frame_y0 + inner_margin + 3.0, "PANEL_FRAME")
        add_line(msp, x, frame_y1 - inner_margin, x, frame_y1 - inner_margin - 3.0, "PANEL_FRAME")
    for i in range(1, 8):
        y = frame_y0 + inner_margin + i * usable_h / 8
        add_line(msp, frame_x0 + inner_margin, y, frame_x0 + inner_margin + 3.0, y, "PANEL_FRAME")
        add_line(msp, frame_x1 - inner_margin, y, frame_x1 - inner_margin - 3.0, y, "PANEL_FRAME")

    # 上方說明區
    note_y = frame_y1 - 20.0
    lx, cx, rx = frame_x0 + 22.0, frame_x0 + frame_w * 0.42, frame_x0 + frame_w * 0.68
    for i, t in enumerate(["white silk block規則如下", "1. 白色框不可塗滿印刷", "2. 尺寸 4.5*30mm 或 5*30mm", "3. 正反面右側及對角需設計", "4. 倍留補件放置圖", "5. 不可塗到光學點"]):
        add_text(msp, t, lx, note_y - i * 5.0, 2.2 if i == 0 else 1.8, "PANEL_NOTE")
    process_text = "No V-CUT and stamp hole" if ("Router" in split_method or "Tab" in split_method) else "V-CUT process"
    for i, t in enumerate(["Unit:mm", f"One piece={board_w:.2f} x {board_h:.2f} mm", f"Panel size={panel_w:.2f} x {panel_h:.2f} mm", f"{pcs_per_panel} pcs/panel", f"Rail policy={rail_policy}", process_text]):
        add_text(msp, t, cx, note_y - i * 5.0, 2.0, "PANEL_NOTE")
    add_text(msp, "PS: 判板或過爐方向之設定由製造建議為準", rx, note_y, 1.8, "PANEL_WARNING")
    add_text(msp, "EQ：一條線或鋼網尺寸管制依製程檢核", rx, note_y - 5.0, 1.8, "PANEL_WARNING")
    add_text(msp, "板邊、連接點、光學點、定位點皆以白色線規劃", rx, note_y - 12.0, 1.8, "PANEL_NOTE")
    add_text(msp, "若採 ROUTE，不可製作 V-CUT 線和郵票孔", rx, note_y - 17.0, 1.8, "PANEL_NOTE")

    # 複製原始單板
    base_matrix = Matrix44.translate(panel_x0 + rail_left - min_x, panel_y0 + rail_bottom - min_y, 0)
    for entity in original_entities:
        transform_entity_safe(entity, base_matrix)

    for row in range(rows):
        for col in range(columns):
            if row == 0 and col == 0:
                continue
            copy_matrix = Matrix44.translate(col * pitch_x, row * pitch_y, 0)
            for entity in original_entities:
                try:
                    copied = entity.copy()
                    transform_entity_safe(copied, copy_matrix)
                    msp.add_entity(copied)
                except Exception:
                    continue

    # Panel 外框與工藝邊線
    add_lwpolyline_rect(msp, panel_x0, panel_y0, panel_w, panel_h, "PANEL_OUTLINE")
    if rail_left > 0:
        add_line(msp, panel_x0 + rail_left, panel_y0, panel_x0 + rail_left, panel_y0 + panel_h, "PANEL_RAIL")
    if rail_right > 0:
        add_line(msp, panel_x0 + panel_w - rail_right, panel_y0, panel_x0 + panel_w - rail_right, panel_y0 + panel_h, "PANEL_RAIL")
    if rail_top > 0:
        add_line(msp, panel_x0, panel_y0 + panel_h - rail_top, panel_x0 + panel_w, panel_y0 + panel_h - rail_top, "PANEL_RAIL")
    if rail_bottom > 0:
        add_line(msp, panel_x0, panel_y0 + rail_bottom, panel_x0 + panel_w, panel_y0 + rail_bottom, "PANEL_RAIL")

    # 每片單板外框
    for row in range(rows):
        for col in range(columns):
            add_lwpolyline_rect(msp, panel_x0 + rail_left + col * pitch_x, panel_y0 + rail_bottom + row * pitch_y, board_w, board_h, "PANEL_OUTLINE")

    # Router / Tab 或 V-cut
    tab_length = 7.0
    tab_width = 2.0

    if "V-cut" in split_method or "V-CUT" in split_method:
        for col in range(1, columns):
            x = panel_x0 + rail_left + col * board_w + (col - 0.5) * gap_x
            add_line(msp, x, panel_y0 + rail_bottom, x, panel_y0 + panel_h - rail_top, "PANEL_VCUT")
        for row in range(1, rows):
            y = panel_y0 + rail_bottom + row * board_h + (row - 0.5) * gap_y
            add_line(msp, panel_x0 + rail_left, y, panel_x0 + panel_w - rail_right, y, "PANEL_VCUT")
    else:
        route_width = max(gap_x, 2.0)

        # 垂直板間 Router channel + Tab
        for col in range(1, columns):
            x_center = panel_x0 + rail_left + col * board_w + (col - 0.5) * gap_x
            add_lwpolyline_rect(msp, x_center - route_width / 2.0, panel_y0 + rail_bottom, route_width, panel_h - rail_top - rail_bottom, "PANEL_ROUTE")
            for row in range(rows):
                board_y = panel_y0 + rail_bottom + row * pitch_y
                for ratio in [0.30, 0.70]:
                    ty = board_y + board_h * ratio
                    add_lwpolyline_rect(msp, x_center - tab_width / 2.0, ty - tab_length / 2.0, tab_width, tab_length, "PANEL_TAB")

        # 水平板間 Router channel + Tab
        for row in range(1, rows):
            y_center = panel_y0 + rail_bottom + row * board_h + (row - 0.5) * gap_y
            add_lwpolyline_rect(msp, panel_x0 + rail_left, y_center - route_width / 2.0, panel_w - rail_left - rail_right, route_width, "PANEL_ROUTE")
            for col in range(columns):
                board_x = panel_x0 + rail_left + col * pitch_x
                for ratio in [0.30, 0.70]:
                    tx = board_x + board_w * ratio
                    add_lwpolyline_rect(msp, tx - tab_length / 2.0, y_center - tab_width / 2.0, tab_length, tab_width, "PANEL_TAB")

        # ★ 外側板邊凸榫型 Tab（對照圖片白色線設計）
        draw_outer_edge_tabs(
            msp=msp,
            panel_x0=panel_x0, panel_y0=panel_y0,
            rail_left=rail_left, rail_bottom=rail_bottom,
            board_w=board_w, board_h=board_h,
            pitch_x=pitch_x, pitch_y=pitch_y,
            columns=columns, rows=rows,
            tab_length=tab_length, tab_width=tab_width,
            layer="PANEL_EDGE_PROFILE",
        )

    # Tooling Hole / Fiducial
    feature_result = auto_determine_panel_features(
        panel_w=panel_w, panel_h=panel_h,
        rail_width=max(min(rail_left, rail_right, rail_top if rail_top > 0 else rail_left, rail_bottom if rail_bottom > 0 else rail_left), 0.0),
        columns=columns, rows=rows, split_method=split_method,
        has_bga_qfn=has_bga_qfn, has_dip=has_dip,
        has_heavy_component=has_heavy_component, is_irregular_shape=is_irregular_shape,
        strategy=strategy,
    )

    shifted_feature_result = dict(feature_result)
    shifted_feature_result["tooling_holes"] = [dict(h, x=round(float(h["x"]) + panel_x0, 3), y=round(float(h["y"]) + panel_y0, 3)) for h in feature_result.get("tooling_holes", [])]
    shifted_feature_result["fiducials"] = [dict(f, x=round(float(f["x"]) + panel_x0, 3), y=round(float(f["y"]) + panel_y0, 3)) for f in feature_result.get("fiducials", [])]

    draw_panel_features(msp=msp, feature_result=shifted_feature_result)

    tooling_callout = PANEL_RULES["tooling_hole"]["template_callout"]
    fiducial_callout = PANEL_RULES["fiducial"]["template_callout"]

    th_ref = sorted(shifted_feature_result["tooling_holes"], key=lambda h: (float(h["x"]), -float(h["y"])), reverse=True)[0] if shifted_feature_result["tooling_holes"] else {"x": panel_x0 + panel_w - 5.0, "y": panel_y0 + 5.0}
    th_x, th_y = float(th_ref["x"]), float(th_ref["y"])
    fd_ref = sorted(shifted_feature_result["fiducials"], key=lambda f: abs(float(f["x"]) - th_x) + abs(float(f["y"]) - th_y))[0] if shifted_feature_result["fiducials"] else {"x": th_x - 7.7, "y": th_y - 5.0}
    fd_x, fd_y = float(fd_ref["x"]), float(fd_ref["y"])

    callout_text_x = min(th_x + 32.0, frame_x1 - 85.0)
    callout_y1 = max(panel_y0 - 12.0, frame_y0 + 38.0)
    callout_y2 = callout_y1 - 10.0

    add_leader_callout(msp, th_x, th_y, th_x + 16.0, callout_y1, callout_text_x, callout_y1, tooling_callout, "PANEL_DIMENSION", 1.8)
    add_leader_callout(msp, fd_x, fd_y, fd_x + 18.0, callout_y2, callout_text_x, callout_y2, fiducial_callout, "PANEL_DIMENSION", 1.8)
    add_dimension_line(msp, th_x, th_y, th_x, th_y + 10.0, "10.00", th_x + 2.0, th_y + 4.5, "PANEL_DIMENSION", 1.8)
    add_dimension_line(msp, th_x - 5.0, th_y, th_x, th_y, "5.00", th_x - 4.0, th_y - 4.0, "PANEL_DIMENSION", 1.8)
    add_dimension_line(msp, fd_x, fd_y, th_x, th_y, "7.70", (fd_x + th_x) / 2.0 - 3.0, (fd_y + th_y) / 2.0 + 2.0, "PANEL_DIMENSION", 1.8)
    add_dimension_line(msp, fd_x, fd_y - 7.0, th_x, fd_y - 7.0, "9.00", (fd_x + th_x) / 2.0 - 3.0, fd_y - 10.0, "PANEL_DIMENSION", 1.8)

    # 尺寸標註
    dim_top_y = panel_y0 + panel_h + 8.0
    dim_left_x = panel_x0 - 10.0
    add_dimension_line(msp, panel_x0, dim_top_y, panel_x0 + panel_w, dim_top_y, f"{panel_w:.2f}", panel_x0 + panel_w / 2.0, dim_top_y + 1.5, "PANEL_DIMENSION")
    add_dimension_line(msp, panel_x0 + rail_left, dim_top_y + 6.0, panel_x0 + rail_left + board_w, dim_top_y + 6.0, f"{board_w:.2f}", panel_x0 + rail_left + board_w / 2.0, dim_top_y + 7.5, "PANEL_DIMENSION")
    effective_w = columns * board_w + max(columns - 1, 0) * gap_x
    add_dimension_line(msp, panel_x0 + rail_left, dim_top_y + 12.0, panel_x0 + rail_left + effective_w, dim_top_y + 12.0, f"{effective_w:.2f}", panel_x0 + rail_left + effective_w / 2.0, dim_top_y + 13.5, "PANEL_DIMENSION")
    add_dimension_line(msp, dim_left_x, panel_y0, dim_left_x, panel_y0 + panel_h, f"{panel_h:.2f}", dim_left_x - 9.0, panel_y0 + panel_h / 2.0, "PANEL_DIMENSION")
    add_dimension_line(msp, dim_left_x - 7.0, panel_y0 + rail_bottom, dim_left_x - 7.0, panel_y0 + rail_bottom + board_h, f"{board_h:.2f}", dim_left_x - 17.0, panel_y0 + rail_bottom + board_h / 2.0, "PANEL_DIMENSION")
    if rail_left > 0:
        add_dimension_line(msp, panel_x0, panel_y0 - 8.0, panel_x0 + rail_left, panel_y0 - 8.0, f"{rail_left:.2f}", panel_x0 + rail_left / 2.0 - 2.0, panel_y0 - 11.0, "PANEL_DIMENSION")
    if rail_bottom > 0:
        add_dimension_line(msp, panel_x0 - 18.0, panel_y0, panel_x0 - 18.0, panel_y0 + rail_bottom, f"{rail_bottom:.2f}", panel_x0 - 27.0, panel_y0 + rail_bottom / 2.0, "PANEL_DIMENSION")

    tab_cx = panel_x0 + rail_left + board_w + gap_x / 2.0 if columns > 1 else panel_x0 + rail_left + board_w
    tab_cy = panel_y0 + rail_bottom + board_h * 0.70
    add_leader_callout(msp, tab_cx, tab_cy, tab_cx + 14.0, tab_cy + 18.0, tab_cx + 20.0, tab_cy + 18.0,
                       f"TAB {tab_width:.2f} x {tab_length:.2f}, gap≥{PANEL_RULES['cutting']['router_min_gap_mm']:.2f}", "PANEL_DIMENSION", 1.8)

    # 標題欄
    title_w, title_h = 92.0, 24.0
    title_x = frame_x1 - inner_margin - title_w
    title_y = frame_y0 + inner_margin
    add_lwpolyline_rect(msp, title_x, title_y, title_w, title_h, "PANEL_TITLE")
    for dy in [6.0, 12.0, 18.0]:
        add_line(msp, title_x, title_y + dy, title_x + title_w, title_y + dy, "PANEL_TITLE")
    for dx in [20.0, 54.0]:
        add_line(msp, title_x + dx, title_y, title_x + dx, title_y + title_h, "PANEL_TITLE")
    for label, dy in [("DRAWN", 19.0), ("CHECK", 13.0), ("APPROVED", 7.0), ("MODEL", 1.0)]:
        add_text(msp, label, title_x + 2.0, title_y + dy, 1.4, "PANEL_TITLE")
    add_text(msp, product_name, title_x + 22.0, title_y + 1.0, 1.5, "PANEL_TITLE")

    # 產圖資訊
    info_x = frame_x0 + inner_margin + 8.0
    info_y = frame_y0 + inner_margin + 12.0
    strategy_source = strategy.get("strategy_source", PANEL_RULES["source"]) if strategy else PANEL_RULES["source"]
    for i, t in enumerate([
        f"Product: {product_name}",
        f"One piece: {board_w:.2f} x {board_h:.2f} mm",
        f"Panel size: {panel_w:.2f} x {panel_h:.2f} mm",
        f"{pcs_per_panel} pcs/panel, Layout: {columns} x {rows}, Split: {split_method}",
        f"Rail: {rail_policy}, L/R/T/B={rail_left:.2f}/{rail_right:.2f}/{rail_top:.2f}/{rail_bottom:.2f}",
        f"Fiducial: {len(shifted_feature_result['fiducials'])} pcs, Tooling Hole: {len(shifted_feature_result['tooling_holes'])} pcs",
        f"Strategy used: True, Source: {strategy_source}",
    ]):
        add_text(msp, t, info_x, info_y + 26.0 - i * 4.5, 1.7, "PANEL_TEXT")

    doc.saveas(output_path)

    return {
        "detected_board_width_mm": round(board_w, 3),
        "detected_board_height_mm": round(board_h, 3),
        "panel_width_mm": round(panel_w, 3),
        "panel_height_mm": round(panel_h, 3),
        "columns": columns, "rows": rows,
        "pcs_per_panel": pcs_per_panel,
        "gap_x_mm": gap_x, "gap_y_mm": gap_y,
        "rail_left_mm": rail_left, "rail_right_mm": rail_right,
        "rail_top_mm": rail_top, "rail_bottom_mm": rail_bottom,
        "rail_policy": rail_policy,
        "split_method": split_method,
        "panel_features": shifted_feature_result,
        "template_style": "ME_panel_drawing_template_v5_edge_profile",
        "template_notes": {
            "white_line_planning": True,
            "rail_policy": rail_policy,
            "edge_tabs": "outer PCB edge side uses white mortise-tenon TAB profile (⊓/∪/⊏/⊐ shape), tabs protrude INTO rail area from PCB edge, minimum 2 mm route gap enforced",
            "route_gap_rule": "ROUTE 各連板間距至少 2.0 mm 以上",
            "tooling_hole_callout": tooling_callout,
            "fiducial_callout": fiducial_callout,
        },
    }


def create_simple_panel_dxf_fallback(
    output_path, product_name, single_board_length, single_board_width,
    rail_width, candidate, has_bga_qfn=False, has_dip=False,
    has_heavy_component=False, is_irregular_shape=False, strategy=None,
) -> Dict[str, Any]:

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for ln in ["PANEL_OUTLINE", "PANEL_VCUT", "PANEL_ROUTE", "PANEL_TAB", "PANEL_TOOLING", "PANEL_FIDUCIAL", "PANEL_TEXT", "PANEL_DIMENSION"]:
        ensure_layer(doc, ln, 7)

    columns = int(candidate.get("x_count", 1))
    rows = int(candidate.get("y_count", 1))
    gap_x = float(candidate.get("gap_x_mm", 0.0))
    gap_y = float(candidate.get("gap_y_mm", 0.0))
    rail_left = float(candidate.get("rail_left_mm", rail_width))
    rail_right = float(candidate.get("rail_right_mm", rail_width))
    rail_top = float(candidate.get("rail_top_mm", rail_width))
    rail_bottom = float(candidate.get("rail_bottom_mm", rail_width))

    panel_w = rail_left + rail_right + single_board_length * columns + gap_x * max(columns - 1, 0)
    panel_h = rail_top + rail_bottom + single_board_width * rows + gap_y * max(rows - 1, 0)
    pitch_x = single_board_length + gap_x
    pitch_y = single_board_width + gap_y

    add_lwpolyline_rect(msp, 0, 0, panel_w, panel_h, "PANEL_OUTLINE")
    for row in range(rows):
        for col in range(columns):
            add_lwpolyline_rect(msp, rail_left + col * pitch_x, rail_bottom + row * pitch_y, single_board_length, single_board_width, "PANEL_OUTLINE")

    split_method = candidate.get("split_method", "V-cut")
    if "Router" in str(split_method) or "Tab" in str(split_method):
        gap_x = max(gap_x, PANEL_RULES["cutting"]["router_min_gap_mm"])
        gap_y = max(gap_y, PANEL_RULES["cutting"]["router_min_gap_mm"])

    tab_length = PANEL_RULES["tab"]["tab_length_mm"]
    tab_width = PANEL_RULES["tab"]["tab_width_mm"]

    if "V-cut" in split_method or "V-CUT" in split_method:
        for col in range(1, columns):
            x = rail_left + col * single_board_length + (col - 0.5) * gap_x
            add_line(msp, x, rail_bottom, x, panel_h - rail_top, "PANEL_VCUT")
        for row in range(1, rows):
            y = rail_bottom + row * single_board_width + (row - 0.5) * gap_y
            add_line(msp, rail_left, y, panel_w - rail_right, y, "PANEL_VCUT")
    else:
        for col in range(1, columns):
            x1 = rail_left + col * single_board_length + (col - 1) * gap_x
            add_lwpolyline_rect(msp, x1, rail_bottom, max(gap_x, 0.3), panel_h - rail_top - rail_bottom, "PANEL_ROUTE")
        for row in range(1, rows):
            y1 = rail_bottom + row * single_board_width + (row - 1) * gap_y
            add_lwpolyline_rect(msp, rail_left, y1, panel_w - rail_left - rail_right, max(gap_y, 0.3), "PANEL_ROUTE")

    draw_edge_tabs(msp=msp, panel_x0=0, panel_y0=0, rail_left=rail_left, rail_bottom=rail_bottom,
                   board_w=single_board_length, board_h=single_board_width, pitch_x=pitch_x, pitch_y=pitch_y,
                   columns=columns, rows=rows, tab_length=tab_length, tab_width=tab_width, layer="PANEL_TAB")

    feature_result = auto_determine_panel_features(
        panel_w=panel_w, panel_h=panel_h,
        rail_width=min(rail_left, rail_right, rail_top, rail_bottom),
        columns=columns, rows=rows, split_method=split_method,
        has_bga_qfn=has_bga_qfn, has_dip=has_dip,
        has_heavy_component=has_heavy_component, is_irregular_shape=is_irregular_shape, strategy=strategy,
    )
    draw_panel_features(msp=msp, feature_result=feature_result)

    strategy_source = strategy.get("strategy_source", PANEL_RULES["source"]) if strategy else PANEL_RULES["source"]
    add_text(msp, f"Product: {product_name}", 0, panel_h + 8, 2.5, "PANEL_TEXT")
    add_text(msp, f"Panel: {columns} x {rows}, {columns * rows} pcs", 0, panel_h + 4, 2.5, "PANEL_TEXT")
    add_text(msp, f"Fiducial: {feature_result.get('fiducial_count')} pcs, Tooling Hole: {feature_result.get('tooling_hole_count')} pcs", 0, panel_h, 2.5, "PANEL_TEXT")
    add_text(msp, f"Strategy used: True, Source: {strategy_source}", 0, panel_h - 4, 2.5, "PANEL_TEXT")
    add_text(msp, f"Program rule: {PANEL_RULES['version']}", 0, panel_h - 8, 2.5, "PANEL_TEXT")
    doc.saveas(output_path)

    return {
        "detected_board_width_mm": single_board_length, "detected_board_height_mm": single_board_width,
        "panel_width_mm": round(panel_w, 3), "panel_height_mm": round(panel_h, 3),
        "columns": columns, "rows": rows, "pcs_per_panel": columns * rows,
        "gap_x_mm": gap_x, "gap_y_mm": gap_y,
        "rail_left_mm": rail_left, "rail_right_mm": rail_right,
        "rail_top_mm": rail_top, "rail_bottom_mm": rail_bottom,
        "split_method": split_method, "panel_features": feature_result, "fallback": True,
    }


# =========================================================
# 本地產出
# =========================================================

def generate_local_panelization_outputs(inputs: Dict[str, str], strategy_json: str = "") -> Dict[str, Any]:
    object_key = str(inputs.get("object_key", ""))
    product_name = str(inputs.get("product_name", "UNKNOWN"))
    single_board_length = to_float(inputs.get("single_board_length", 120), 120)
    single_board_width = to_float(inputs.get("single_board_width", 80), 80)
    rail_width = to_float(inputs.get("rail_width", PANEL_RULES["rail"]["default_mm"]), PANEL_RULES["rail"]["default_mm"])
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

    detected_length, detected_width = single_board_length, single_board_width
    try:
        source_doc = ezdxf.readfile(local_input)
        detected_bbox = get_modelspace_bbox(source_doc)
        if detected_bbox:
            min_x, min_y, max_x, max_y = detected_bbox
            dl, dw = max_x - min_x, max_y - min_y
            detected_length = dl if dl > 0 else single_board_length
            detected_width = dw if dw > 0 else single_board_width
    except Exception:
        pass

    result = calculate_candidates(detected_length, detected_width, rail_width, smt_max_length, smt_max_width, ict_max_length, ict_max_width, has_bga_qfn, has_dip, has_heavy_component, is_irregular_shape)
    strategy, strategy_applied, strategy_parse_error = get_strategy_for_backend(strategy_json=strategy_json, best_candidate=result["best_candidate"], has_bga_qfn=has_bga_qfn, has_dip=has_dip, has_heavy_component=has_heavy_component, is_irregular_shape=is_irregular_shape, input_rail_width=rail_width)
    candidate = apply_strategy_to_candidate(base_candidate=result["best_candidate"], strategy=strategy, board_w=detected_length, board_h=detected_width, default_rail_width=rail_width)
    result = update_result_with_strategy_candidate(result, candidate)

    safe_name = safe_filename_name(product_name)
    output_name = f"panelized_{safe_name}_{candidate['panel_type']}_real.dxf"
    output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{output_name}")
    geometry_info = {}

    try:
        geometry_info = create_real_panel_dxf_from_source(source_path=local_input, output_path=output_path, product_name=product_name, single_board_length=detected_length, single_board_width=detected_width, rail_width=rail_width, candidate=candidate, has_bga_qfn=has_bga_qfn, has_dip=has_dip, has_heavy_component=has_heavy_component, is_irregular_shape=is_irregular_shape, strategy=strategy)
        geometry_mode = "real_source_dxf_copied_me_template_detail"
    except Exception as e:
        geometry_info = create_simple_panel_dxf_fallback(output_path=output_path, product_name=product_name, single_board_length=detected_length, single_board_width=detected_width, rail_width=rail_width, candidate=candidate, has_bga_qfn=has_bga_qfn, has_dip=has_dip, has_heavy_component=has_heavy_component, is_irregular_shape=is_irregular_shape, strategy=strategy)
        geometry_info["real_dxf_error"] = str(e)
        geometry_mode = "simple_fallback"

    try:
        os.remove(local_input)
    except Exception:
        pass

    output_object_key = f"outputs/{uuid.uuid4()}/{output_name}"
    upload_file_to_minio(local_path=output_path, object_name=output_object_key, content_type="application/dxf")
    try:
        os.remove(output_path)
    except Exception:
        pass

    minio_presigned_url = get_presigned_download_url(output_object_key, hours=24)
    api_download_url = make_public_api_download_url(output_object_key)

    panel_dxf = {
        "status": "success", "product_name": product_name,
        "input_object_key": object_key, "output_object_key": output_object_key,
        "output_filename": output_name, "download_url": api_download_url,
        "api_download_url": api_download_url, "minio_presigned_url": minio_presigned_url,
        "expires_hours": 24, "geometry_mode": geometry_mode, "geometry_info": geometry_info,
        "best_candidate": candidate, "display_candidates": result["display_candidates"],
        "all_candidates": result["all_candidates"], "candidates": result["candidates"],
        "strategy_applied": True, "strategy_source": strategy.get("strategy_source", PANEL_RULES["source"]),
        "strategy_json": strategy, "strategy_parse_error": strategy_parse_error,
        "program_rules_applied": True, "program_rule_version": PANEL_RULES["version"],
        "message": "Panelized DXF generated with embedded company panel rules and ME template layout.",
    }

    report_text = build_ai_report_markdown(product_name=product_name, object_key=object_key, result=result, panel_dxf=panel_dxf, strategy=strategy, strategy_applied=strategy_applied, strategy_parse_error=strategy_parse_error)

    return {
        "report_text": report_text, "panel_dxf": panel_dxf, "panel_dxf_info": panel_dxf,
        "output_object_key": output_object_key, "output_filename": output_name,
        "download_url": api_download_url, "api_download_url": api_download_url,
        "minio_presigned_url": minio_presigned_url, "best_candidate": candidate,
        "display_candidates": result["display_candidates"], "all_candidates": result["all_candidates"],
        "candidates": result["candidates"], "geometry_info": geometry_info, "geometry_mode": geometry_mode,
        "strategy_applied": True, "strategy_source": strategy.get("strategy_source", PANEL_RULES["source"]),
        "strategy_json": strategy, "strategy_parse_error": strategy_parse_error,
        "program_rules_applied": True, "program_rule_version": PANEL_RULES["version"],
    }


def merge_local_outputs_into_dify_result(dify_result, local_outputs):
    if not isinstance(dify_result, dict):
        dify_result = {}
    if "data" not in dify_result or not isinstance(dify_result.get("data"), dict):
        dify_result["data"] = {}
    if "outputs" not in dify_result["data"] or not isinstance(dify_result["data"].get("outputs"), dict):
        dify_result["data"]["outputs"] = {}
    outputs = dify_result["data"]["outputs"]
    outputs["report_text"] = outputs.get("report_text") or local_outputs["report_text"]
    outputs["panel_dxf"] = outputs.get("panel_dxf") or local_outputs["panel_dxf"]
    outputs["panel_dxf_info"] = outputs.get("panel_dxf_info") or local_outputs["panel_dxf_info"]
    outputs["fallback_output_object_key"] = local_outputs["output_object_key"]
    outputs["fallback_output_filename"] = local_outputs["output_filename"]
    outputs["fallback_download_url"] = local_outputs["download_url"]
    outputs["geometry_mode"] = local_outputs["geometry_mode"]
    outputs["geometry_info"] = local_outputs["geometry_info"]
    outputs["display_candidates"] = local_outputs["display_candidates"]
    outputs["strategy_applied"] = local_outputs["strategy_applied"]
    outputs["strategy_source"] = local_outputs["strategy_source"]
    outputs["strategy_json"] = local_outputs["strategy_json"]
    outputs["strategy_parse_error"] = local_outputs["strategy_parse_error"]
    outputs["program_rules_applied"] = local_outputs["program_rules_applied"]
    outputs["program_rule_version"] = local_outputs["program_rule_version"]
    dify_result["data"]["outputs"] = outputs
    return dify_result


# =========================================================
# API：上傳 DXF
# =========================================================

@app.post("/api/pcb/upload-dxf-to-minio")
async def upload_dxf_to_minio(dxf_file: UploadFile = File(...)):
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
    upload_file_to_minio(local_path=tmp_path, object_name=object_key, content_type="application/dxf")
    try:
        os.remove(tmp_path)
    except Exception:
        pass
    return {"status": "success", "file_id": file_id, "object_key": object_key, "filename": safe_filename, "file_size_bytes": file_size, "message": "DXF uploaded to MinIO successfully."}


# =========================================================
# API：候選方案
# =========================================================

@app.post("/api/pcb/generate-panel-candidates-from-minio")
async def generate_panel_candidates_from_minio(
    object_key: str = Form(...), product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...), single_board_width: float = Form(...),
    rail_width: float = Form(PANEL_RULES["rail"]["default_mm"]),
    smt_max_length: float = Form(330.0), smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0), ict_max_width: float = Form(300.0),
    has_bga_qfn: str = Form("No"), has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"), is_irregular_shape: str = Form("No"),
):
    local_input = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_input.dxf")
    download_file_from_minio(object_key, local_input)
    detected_length, detected_width = single_board_length, single_board_width
    try:
        source_doc = ezdxf.readfile(local_input)
        detected_bbox = get_modelspace_bbox(source_doc)
        if detected_bbox:
            min_x, min_y, max_x, max_y = detected_bbox
            detected_length, detected_width = max_x - min_x, max_y - min_y
    except Exception:
        pass
    try:
        os.remove(local_input)
    except Exception:
        pass
    result = calculate_candidates(detected_length, detected_width, rail_width, smt_max_length, smt_max_width, ict_max_length, ict_max_width, yes_no_to_bool(has_bga_qfn), yes_no_to_bool(has_dip), yes_no_to_bool(has_heavy_component), yes_no_to_bool(is_irregular_shape))
    report_text = build_ai_report_markdown(product_name=product_name, object_key=object_key, result=result)
    return {
        "product_name": product_name, "object_key": object_key,
        "stage": "program_rule_base_panelization",
        "detected_single_board": {"length_mm": round(detected_length, 3), "width_mm": round(detected_width, 3)},
        "rule_source": PANEL_RULES["source"], "rule_version": PANEL_RULES["version"],
        "best_candidate": result["best_candidate"], "display_candidates": result["display_candidates"],
        "all_candidates": result["all_candidates"], "candidates": result["candidates"],
        "recommended_candidates": result["recommended_candidates"],
        "caution_candidates": result["caution_candidates"],
        "not_recommended_candidates": result["not_recommended_candidates"],
        "comparison_table_markdown": build_comparison_table_markdown(result["all_candidates"], limit=3),
        "caution_and_not_recommended_summary": build_caution_and_not_summary(result["all_candidates"], limit=3),
        "report_text": report_text, "report_markdown": report_text,
    }


# =========================================================
# API：產生 DXF
# =========================================================

@app.post("/api/pcb/generate-panel-dxf-from-minio")
async def generate_panel_dxf_from_minio(
    object_key: str = Form(...), product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...), single_board_width: float = Form(...),
    rail_width: float = Form(PANEL_RULES["rail"]["default_mm"]),
    smt_max_length: float = Form(330.0), smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0), ict_max_width: float = Form(300.0),
    has_bga_qfn: str = Form("No"), has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"), is_irregular_shape: str = Form("No"),
    strategy_json: str = Form(""),
):
    inputs = {
        "object_key": object_key, "product_name": product_name,
        "single_board_length": str(single_board_length), "single_board_width": str(single_board_width),
        "rail_width": str(rail_width), "smt_max_length": str(smt_max_length), "smt_max_width": str(smt_max_width),
        "ict_max_length": str(ict_max_length), "ict_max_width": str(ict_max_width),
        "has_bga_qfn": has_bga_qfn, "has_dip": has_dip, "has_heavy_component": has_heavy_component, "is_irregular_shape": is_irregular_shape,
    }
    outputs = generate_local_panelization_outputs(inputs, strategy_json=strategy_json)
    return outputs["panel_dxf"]


# =========================================================
# API：下載 DXF
# =========================================================

@app.get("/api/pcb/download")
def download_from_minio(object_key: str):
    local_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{os.path.basename(object_key)}")
    download_file_from_minio(object_key, local_path)
    return FileResponse(local_path, media_type="application/dxf", filename=os.path.basename(object_key))


# =========================================================
# 背景任務
# =========================================================

def run_dify_job_background(job_id: str, inputs: Dict[str, str]):
    JOB_STORE[job_id]["status"] = "running"
    JOB_STORE[job_id]["started_at"] = datetime.utcnow().isoformat()

    try:
        local_outputs = generate_local_panelization_outputs(inputs, strategy_json="")

        def make_fallback_result(warning_msg, extra=None):
            r = {
                "task_id": None, "workflow_run_id": None,
                "data": {
                    "status": "program_rule_success",
                    "outputs": {
                        "report_text": local_outputs["report_text"],
                        "panel_dxf": local_outputs["panel_dxf"],
                        "panel_dxf_info": local_outputs["panel_dxf_info"],
                        "fallback_output_object_key": local_outputs["output_object_key"],
                        "fallback_output_filename": local_outputs["output_filename"],
                        "fallback_download_url": local_outputs["download_url"],
                        "geometry_mode": local_outputs["geometry_mode"],
                        "geometry_info": local_outputs["geometry_info"],
                        "display_candidates": local_outputs["display_candidates"],
                        "strategy_applied": local_outputs["strategy_applied"],
                        "strategy_source": local_outputs["strategy_source"],
                        "strategy_json": local_outputs["strategy_json"],
                        "strategy_parse_error": local_outputs["strategy_parse_error"],
                        "program_rules_applied": local_outputs["program_rules_applied"],
                        "program_rule_version": local_outputs["program_rule_version"],
                    },
                    "warning": warning_msg,
                },
            }
            if extra:
                r["data"].update(extra)
            return r

        if not DIFY_API_BASE or not DIFY_API_KEY:
            JOB_STORE[job_id].update({"status": "success", "message": "本地公司規範 DXF 已成功產生", "result": make_fallback_result("Dify API not configured."), "error": None, "finished_at": datetime.utcnow().isoformat()})
            return

        url = f"{DIFY_API_BASE.rstrip('/')}/workflows/run"
        payload = {
            "inputs": {k: normalize_yes_no(v) if k in ["has_bga_qfn", "has_dip", "has_heavy_component", "is_irregular_shape"] else str(v) for k, v in inputs.items()},
            "response_mode": "blocking", "user": "pcb-upload-page",
        }
        headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
        response = requests.post(url, json=payload, headers=headers, timeout=600)

        if response.status_code >= 400:
            JOB_STORE[job_id].update({"status": "success", "message": "Dify 失敗，但 DXF 已成功產生", "result": make_fallback_result("Dify API failed.", {"dify_error": response.text}), "error": None, "finished_at": datetime.utcnow().isoformat()})
            return

        if "application/json" not in response.headers.get("content-type", ""):
            JOB_STORE[job_id].update({"status": "success", "message": "Dify 回傳非 JSON，但 DXF 已成功產生", "result": make_fallback_result("Dify non-JSON.", {"dify_response_preview": response.text[:1500]}), "error": None, "finished_at": datetime.utcnow().isoformat()})
            return

        result_json = response.json()
        if result_json.get("data", {}).get("status") == "failed":
            result_json["data"]["status"] = "program_rule_success"
            result_json["data"]["warning"] = "Dify workflow failed, but program-rule DXF succeeded."
        result_json = merge_local_outputs_into_dify_result(result_json, local_outputs)
        JOB_STORE[job_id].update({"status": "success", "message": "DXF 已完成，Dify 結果已合併", "result": result_json, "error": None, "finished_at": datetime.utcnow().isoformat()})

    except Exception as e:
        JOB_STORE[job_id].update({"status": "failed", "message": "DXF 產生失敗", "error": {"error_type": type(e).__name__, "error_detail": str(e), "traceback": traceback.format_exc()}, "finished_at": datetime.utcnow().isoformat()})


@app.post("/api/pcb/start-dify-job")
async def start_dify_job(
    background_tasks: BackgroundTasks,
    object_key: str = Form(...), product_name: str = Form(...),
    single_board_length: str = Form(...), single_board_width: str = Form(...),
    rail_width: str = Form(str(PANEL_RULES["rail"]["default_mm"])),
    smt_max_length: str = Form("330"), smt_max_width: str = Form("250"),
    ict_max_length: str = Form("350"), ict_max_width: str = Form("300"),
    has_bga_qfn: str = Form("No"), has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"), is_irregular_shape: str = Form("No"),
):
    job_id = str(uuid.uuid4())
    inputs = {
        "object_key": object_key, "product_name": product_name,
        "single_board_length": single_board_length, "single_board_width": single_board_width,
        "rail_width": rail_width, "smt_max_length": smt_max_length, "smt_max_width": smt_max_width,
        "ict_max_length": ict_max_length, "ict_max_width": ict_max_width,
        "has_bga_qfn": has_bga_qfn, "has_dip": has_dip, "has_heavy_component": has_heavy_component, "is_irregular_shape": is_irregular_shape,
    }
    JOB_STORE[job_id] = {"job_id": job_id, "status": "queued", "message": "任務已建立，等待背景執行", "inputs": inputs, "created_at": datetime.utcnow().isoformat(), "started_at": None, "finished_at": None, "result": None, "error": None}
    background_tasks.add_task(run_dify_job_background, job_id, inputs)
    return {"status": "accepted", "job_id": job_id, "message": "Dify Workflow background job started."}


@app.get("/api/pcb/job-status/{job_id}")
def get_job_status(job_id: str):
    job = JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    return job


@app.post("/api/pcb/run-dify-panelization")
async def run_dify_panelization(
    object_key: str = Form(...), product_name: str = Form(...),
    single_board_length: str = Form(...), single_board_width: str = Form(...),
    rail_width: str = Form(str(PANEL_RULES["rail"]["default_mm"])),
    smt_max_length: str = Form("330"), smt_max_width: str = Form("250"),
    ict_max_length: str = Form("350"), ict_max_width: str = Form("300"),
    has_bga_qfn: str = Form("No"), has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"), is_irregular_shape: str = Form("No"),
    strategy_json: str = Form(""),
):
    inputs = {
        "object_key": object_key, "product_name": product_name,
        "single_board_length": single_board_length, "single_board_width": single_board_width,
        "rail_width": rail_width, "smt_max_length": smt_max_length, "smt_max_width": smt_max_width,
        "ict_max_length": ict_max_length, "ict_max_width": ict_max_width,
        "has_bga_qfn": has_bga_qfn, "has_dip": has_dip, "has_heavy_component": has_heavy_component, "is_irregular_shape": is_irregular_shape,
    }
    local_outputs = generate_local_panelization_outputs(inputs, strategy_json=strategy_json)
    return {
        "status": "success",
        "message": "Synchronous API used real DXF panelization output with embedded company panel rules.",
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
                "display_candidates": local_outputs["display_candidates"],
                "strategy_applied": local_outputs["strategy_applied"],
                "strategy_source": local_outputs["strategy_source"],
                "strategy_json": local_outputs["strategy_json"],
                "strategy_parse_error": local_outputs["strategy_parse_error"],
                "program_rules_applied": local_outputs["program_rules_applied"],
                "program_rule_version": local_outputs["program_rule_version"],
            }
        },
    }
