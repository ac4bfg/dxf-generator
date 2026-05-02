"""Isometric drawing engine. Refactored from test_engine.py PoC."""
import ezdxf
import math
import re
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


VARIANT_DIRECTIONS = {
    "start-BR": {"forward": 150, "back": 330, "right": 30, "left": 210, "up": 90, "down": 270},
    "start-BL": {"forward": 30, "back": 210, "right": 330, "left": 150, "up": 90, "down": 270},
    "start-TR": {"forward": 210, "back": 30, "right": 150, "left": 330, "up": 90, "down": 270},
    "start-TL": {"forward": 330, "back": 150, "right": 210, "left": 30, "up": 90, "down": 270},
    # SK: no isometric variant, uses standard orthogonal directions
    "sk": {"forward": 90, "back": 270, "right": 0, "left": 180, "up": 90, "down": 270},
}

VARIANT_SCALE = {
    "start-BR": (1, 1),
    "start-BL": (-1, 1),
    "start-TR": (-1, 1),
    "start-TL": (-1, 1),
    "sk": (1, 1),
}

# Default insert position per start variant (dipakai kalau request tidak provide start_insert)
VARIANT_START_INSERT = {
    "start-BR": (209.52, 124.98),
    "start-BL": (143.9556, 147.9640),
    "start-TR": (240, 168.2963),
    "start-TL": (160, 180),
    "sk": (150.0, 150.0),
}

# Block crossing per start variant — ditampilkan saat casing > 0
CROSSING_BLOCK_MAP = {
    "start-BR": "crossing-BR",
    "start-BL": "crossing-BL",
    "start-TR": "crossing-TR",
    "start-TL": "crossing-TL",
}

# Placeholder insert positions — sesuaikan setelah layout dikonfirmasi
CROSSING_INSERT_DEFAULT = {
    "start-BR": (215, 170),
    "start-BL": (210, 160),
    "start-TR": (230, 130),
    "start-TL": (170, 140),
}

_BASE_DIM_MAP = {
    30: (30, 90), 210: (30, 90),
    150: (330, 270), 330: (330, 270),
    90: (90, 210), 270: (90, 210),
}

# Orthogonal dim map for SK module (non-isometric axes).
# Extension lines run perpendicular to the pipe (standard CAD convention).
_BASE_DIM_MAP_SK = {
    0: (0, 90),   180: (0, 90),   # horizontal pipe → extension vertical
    90: (90, 0),  270: (90, 0),   # vertical pipe   → extension horizontal
}

STYLE_CYAN = {"layer": "0", "color": 4, "lineweight": 50, "ltscale": 0.5}
STYLE_SK = {"layer": "SK", "color": 5}

DIM_OVERRIDES = {
    "dimasz": 5.0, "dimscale": 0.8, "dimtxt": 2.5,
    "dimblk": "", "dimblk1": "_DOTSMALL", "dimblk2": "_DOTSMALL", "dimsah": 1,
}

BASE_BEND_RADIUS = 1.4

# Auto-variant selection untuk smart blocks.
# Key: nama block "virtual" yang dipakai di segment.
# Value: dict {angle -> nama block asli di DXF template}.
# Angle adalah arah pipa dalam derajat (sudah di-resolve oleh _resolve_pipe_angle).
# UP variant = untuk jalur DOWN (270°), karena entry dari atas → exit ke bawah.
# DOWN variant = untuk jalur UP (90°), karena entry dari bawah → exit ke atas.
_BALL_VALVE_1_2_MAP: Dict[int, str] = {
    270: "ball_valve_1-2_up",     # jalur ke bawah
    90:  "ball_valve_1-2_down",   # jalur ke atas
    330: "ball_valve_1-2_BR",     # jalur back  (start-BR)
    150: "ball_valve_1-2_TL",     # jalur forward (start-BR)
    30:  "ball_valve_1-2_TR",     # jalur right (start-BR)
    210: "ball_valve_1-2_BL",     # jalur left  (start-BR)
}

SMART_BLOCK_VARIANTS: Dict[str, Dict[int, str]] = {
    "ball_valve_1-2": _BALL_VALVE_1_2_MAP,
    "valve": _BALL_VALVE_1_2_MAP,   # alias lama, otomatis resolve ke variant yang tepat
}


def draw_breakline(msp, midpoint, angle_deg, style):
    """Draw a breakline zigzag symbol at `midpoint`, perpendicular to `angle_deg`.
    Used to indicate a pipe is drawn NTS (Not To Scale)."""
    rad = math.radians(angle_deg)
    # Pipe direction
    dx, dy = math.cos(rad), math.sin(rad)
    # Perpendicular direction
    px, py = -dy, dx

    # Breakline geometry (in DXF units)
    gap = 1.2    # half-length of break along pipe
    amp = 0.8    # zigzag amplitude perpendicular
    mx, my = midpoint

    # 4-point zigzag: start → up → down → end
    pts = [
        (mx - gap * dx,              my - gap * dy),              # start
        (mx - gap/3 * dx + amp * px, my - gap/3 * dy + amp * py), # peak up
        (mx + gap/3 * dx - amp * px, my + gap/3 * dy - amp * py), # peak down
        (mx + gap * dx,              my + gap * dy),              # end
    ]
    for j in range(len(pts) - 1):
        msp.add_line(pts[j], pts[j + 1], dxfattribs=style)


def mirror_angle_h(a): return (180 - a) % 360
def mirror_angle_v(a): return (-a) % 360


def transform_angle(angle: float, start_block: str) -> float:
    if start_block == "start-BR":
        return angle
    if start_block == "start-BL":
        return mirror_angle_h(angle)
    if start_block == "start-TR":
        return angle if angle in (90, 270) else mirror_angle_v(angle)
    if start_block == "start-TL":
        return angle if angle in (90, 270) else mirror_angle_h(mirror_angle_v(angle))
    return angle


def should_invert_arc(start_block: str) -> bool:
    return start_block in ("start-BR", "start-TL")


def mm_to_units(mm: float) -> float:
    return mm * (20 / 1000)


def calc_endpoint(cursor, angle_deg, length_units):
    rad = math.radians(angle_deg)
    return (cursor[0] + length_units * math.cos(rad),
            cursor[1] + length_units * math.sin(rad))


def auto_bend_radius(from_angle, to_angle):
    sweep = min((to_angle - from_angle) % 360, (from_angle - to_angle) % 360)
    if sweep <= 0:
        return BASE_BEND_RADIUS
    return BASE_BEND_RADIUS * (120.0 / sweep)


def get_block_exit_pos(doc, block_name, insert_pos, scale, rotation=0):
    """Return world-space exit point for cursor after component placement.
    Reads the first POINT entity found in the block (no layer requirement).
    Assumes block origin (0,0) = entry port, and the POINT marks the exit port.
    Returns None if no POINT entity exists in the block."""
    block_layout = doc.blocks.get(block_name)
    if not block_layout:
        return None

    for e in block_layout:
        if e.dxftype() == 'POINT':
            px = e.dxf.location.x * scale[0]
            py = e.dxf.location.y * scale[1]
            if rotation:
                r = math.radians(rotation)
                cos_r, sin_r = math.cos(r), math.sin(r)
                px, py = cos_r * px - sin_r * py, sin_r * px + cos_r * py
            return (insert_pos[0] + px, insert_pos[1] + py)

    return None


def get_block_entry_offset(doc, block_name, scale, rotation=0):
    block = doc.blocks.get(block_name)
    if not block:
        return None
    entry_pt = None
    for e in block:
        if e.dxftype() == 'POINT' and e.dxf.layer == '_ENTRY':
            entry_pt = (e.dxf.location.x, e.dxf.location.y)
            break
    if entry_pt is None:
        return None
    px = entry_pt[0] * scale[0]
    py = entry_pt[1] * scale[1]
    if rotation:
        r = math.radians(rotation)
        cos_r, sin_r = math.cos(r), math.sin(r)
        px, py = cos_r * px - sin_r * py, sin_r * px + cos_r * py
    return (-px, -py)


def get_block_base_point(doc, block_name):
    """Return (bx, by) from the BLOCK entity's base_point attribute.
    In AutoCAD BEDIT 'Set Basepoint' stores a non-zero base_point when the user
    marks the insertion reference without moving geometry.  We subtract this
    from insert_pos so the base_point lands exactly at the pipe cursor."""
    blk = doc.blocks.get(block_name)
    if not blk:
        return 0.0, 0.0
    try:
        bp = blk.block.dxf.base_point
        return float(bp.x), float(bp.y)
    except Exception:
        return 0.0, 0.0


def get_block_entry_retreat(doc, block_name, pipe_angle):
    """How far the block extends BEHIND origin (0,0) along pipe_angle direction.
    Used to shorten the incoming pipe so it ends flush with the block's back edge."""
    block = doc.blocks.get(block_name)
    if not block:
        return 0.0
    points = []
    for e in block:
        t = e.dxftype()
        if t == 'LINE':
            points += [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
        elif t in ('ARC', 'CIRCLE'):
            points.append((e.dxf.center.x, e.dxf.center.y))
        elif t == 'LWPOLYLINE':
            points += [(x, y) for x, y, *_ in e.get_points()]
    if not points:
        return 0.0
    rad = math.radians(pipe_angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    projs = [px * cos_a + py * sin_a for px, py in points]
    return max(0.0, -min(projs))


def get_block_gap(doc, block_name, pipe_angle):
    """Return (gap, retreat, min_proj).
    gap      = full extent of block along pipe_angle.
    retreat  = how far block extends BEHIND origin; used to shorten incoming pipe.
    min_proj = position of block's front face along pipe_angle; used to align
               block's entry with cursor (insert_pos = cursor - min_proj * direction)."""
    block = doc.blocks.get(block_name)
    if not block:
        return 0, 0, 0.0
    points = []
    for e in block:
        t = e.dxftype()
        if t == 'LINE':
            points.append((e.dxf.start.x, e.dxf.start.y))
            points.append((e.dxf.end.x, e.dxf.end.y))
        elif t in ('ARC', 'CIRCLE'):
            points.append((e.dxf.center.x, e.dxf.center.y))
        elif t == 'LWPOLYLINE':
            for x, y, *_ in e.get_points():
                points.append((x, y))
    if not points:
        return 0, 0, 0.0
    rad = math.radians(pipe_angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    projs = [px * cos_a + py * sin_a for px, py in points]
    min_p = min(projs)
    gap = max(projs) - min_p
    retreat = max(0.0, -min_p)  # how far block extends BEHIND origin (for pipe shortening)
    return gap, retreat, min_p  # min_p = front face position along pipe_angle


def calc_bend(cursor, from_angle, to_angle, radius, bend_side=None, real_from=None):
    def angle_match(a, b):
        diff = abs(a - b) % 360
        return diff < 1 or diff > 359

    if bend_side == "left":
        signs = [1]
    elif bend_side == "right":
        signs = [-1]
    else:
        signs = [1, -1]

    if real_from is None:
        real_from = from_angle

    expected_turn = min((to_angle - from_angle) % 360, (from_angle - to_angle) % 360)

    best = None
    for sign in signs:
        center_dir = from_angle + sign * 90
        cdr = math.radians(center_dir)
        cx = cursor[0] + radius * math.cos(cdr)
        cy = cursor[1] + radius * math.sin(cdr)
        a_in = math.degrees(math.atan2(cursor[1] - cy, cursor[0] - cx)) % 360

        tangent_in = (a_in + 90) % 360
        if not (angle_match(tangent_in, from_angle) or angle_match(tangent_in, (from_angle + 180) % 360)):
            continue

        for out_sign in [1, -1]:
            out_dir = to_angle + out_sign * 90
            odr = math.radians(out_dir)
            qx = cx + radius * math.cos(odr)
            qy = cy + radius * math.sin(odr)
            a_out = math.degrees(math.atan2(qy - cy, qx - cx)) % 360

            tangent_out = (a_out + 90) % 360
            if not (angle_match(tangent_out, to_angle) or angle_match(tangent_out, (to_angle + 180) % 360)):
                continue

            for arc_start, arc_end in [(a_out, a_in), (a_in, a_out)]:
                sweep = (arc_end - arc_start) % 360
                if 0 < sweep <= 180:
                    if (arc_start, arc_end) == (a_out, a_in):
                        tangent_phys = (a_in - 90) % 360
                    else:
                        tangent_phys = (a_in + 90) % 360
                    direct_match = 0 if angle_match(tangent_phys, real_from) else 1
                    score = (direct_match, abs(sweep - expected_turn))
                    if best is None or score < (best[0], best[1]):
                        best = (direct_match, abs(sweep - expected_turn),
                                (cx, cy), arc_start, arc_end, (qx, qy))
                    break

    if best:
        _, _, center, arc_start, arc_end, new_cursor = best
        return center, arc_start, arc_end, new_cursor
    return (cursor[0], cursor[1]), 0, 0, cursor


def _compute_dim_geometry(dim_entity, p1, p2, base, dim_angle_deg, oblique_deg, geom_block):
    """Compute isometric dim geometry and harvest styling from ezdxf-rendered block.

    Returns a dict with all data needed to draw the dimension directly to MSP.
    Does NOT modify the geometry block.
    """
    dim_rad = math.radians(dim_angle_deg)
    ext_rad = math.radians(oblique_deg)
    cos_ext, sin_ext = math.cos(ext_rad), math.sin(ext_rad)
    cos_dim, sin_dim = math.cos(dim_rad), math.sin(dim_rad)

    def intersect(px, py):
        det = cos_ext * (-sin_dim) - (-cos_dim) * sin_ext
        if abs(det) < 1e-10:
            return (px, py)
        dx, dy = base[0] - px, base[1] - py
        t = (dx * (-sin_dim) - (-cos_dim) * dy) / det
        return (px + t * cos_ext, py + t * sin_ext)

    arrow1 = intersect(p1[0], p1[1])
    arrow2 = intersect(p2[0], p2[1])
    tc = ((arrow1[0] + arrow2[0]) / 2, (arrow1[1] + arrow2[1]) / 2)

    # Harvest styling from ezdxf-rendered entities (read-only).
    line_attribs = {}
    insert_block_name = None
    insert_attribs = {}
    mtext_props = {}

    if geom_block:
        for e in geom_block:
            t = e.dxftype()
            if t == 'LINE' and not line_attribs:
                for attr in ('layer', 'color', 'lineweight'):
                    if e.dxf.hasattr(attr):
                        line_attribs[attr] = e.dxf.get(attr)
            elif t == 'INSERT' and insert_block_name is None:
                insert_block_name = e.dxf.name
                for attr in ('layer', 'color', 'xscale', 'yscale'):
                    if e.dxf.hasattr(attr):
                        insert_attribs[attr] = e.dxf.get(attr)
            elif t == 'MTEXT' and not mtext_props:
                for attr in ('layer', 'color', 'char_height', 'rotation', 'style'):
                    if e.dxf.hasattr(attr):
                        mtext_props[attr] = e.dxf.get(attr)

    return {
        'arrow1': arrow1, 'arrow2': arrow2, 'tc': tc,
        'cos_ext': cos_ext, 'sin_ext': sin_ext,
        'cos_dim': cos_dim, 'sin_dim': sin_dim,
        'line_attribs': line_attribs,
        'insert_block_name': insert_block_name,
        'insert_attribs': insert_attribs,
        'mtext_props': mtext_props,
    }


# Per-character width ratios measured from AutoCAD textbox() on 10-char repeated
# strings, divided by 10. This correctly accounts for oblique-overlap: single-char
# textbox overestimates per-char width because the oblique offset (H·tan θ) is
# counted once per string, not once per character. Dividing a 10-char measurement
# by 10 averages it correctly.
# Measured with style height=10, then divided by 100 (10 chars × height 10).
_CHAR_W_BY_STYLE: dict = {
    'ISO 30': {
        '0': 0.7684, '1': 0.7434, '2': 0.7837, '3': 0.7837, '4': 0.7623,
        '5': 0.7799, '6': 0.7705, '7': 0.7815, '8': 0.7771, '9': 0.7705,
        '.': 0.0762,
    },
    'ISO-30': {
        '0': 0.7684, '1': 0.7515, '2': 0.7837, '3': 0.7771, '4': 0.7434,
        '5': 0.7771, '6': 0.7667, '7': 0.7587, '8': 0.7771, '9': 0.7667,
        '.': 0.0762,
    },
}


def _estimate_text_width(text: str, char_height: float,
                         text_style: str = 'ISO 30') -> float:
    """Return estimated text width in DXF units for the given text style.

    Uses per-character widths from 10-char string measurements (divided by 10),
    which correctly accounts for oblique-overlap between characters. All digit
    widths cluster in the 0.74–0.78 range, so gaps are consistent regardless
    of which digits appear in the dimension text.
    """
    table = _CHAR_W_BY_STYLE.get(text_style, _CHAR_W_BY_STYLE['ISO 30'])
    default_w = 0.77  # average across all measured digits
    return sum(table.get(c, default_w) for c in text) * char_height


# Keep old name as thin wrapper so call sites don't break.
def fix_oblique_geometry(doc, dim_entity, p1, p2, base, dim_angle_deg, oblique_deg,
                         dim_text=""):
    geom_block = doc.blocks.get(dim_entity.dxf.geometry)
    data = _compute_dim_geometry(dim_entity, p1, p2, base,
                                 dim_angle_deg, oblique_deg, geom_block)
    return data['tc'], data['mtext_props']


def _clean_mtext(raw: str) -> str:
    """Strip DXF MTEXT formatting codes, return plain text."""
    raw = re.sub(r'\\S([^;^/]+)[;^/][^;]*;?', r'\1', raw)
    raw = re.sub(r'\\[AHWQTCfF][^;]*;', '', raw)
    raw = re.sub(r'\\[LlOoKk]', '', raw)
    raw = raw.replace(r'\P', ' ').replace(r'\~', ' ')
    raw = raw.replace('{', '').replace('}', '')
    return raw.strip()


def explode_dimension(doc, msp, dim_entity):
    geom_block = doc.blocks.get(dim_entity.dxf.geometry)
    if not geom_block:
        return False
    for entity in geom_block:
        etype = entity.dxftype()
        if etype == 'LINE':
            attribs = {}
            for attr in ['layer', 'color', 'lineweight', 'linetype', 'ltscale']:
                if entity.dxf.hasattr(attr):
                    attribs[attr] = entity.dxf.get(attr)
            msp.add_line(entity.dxf.start, entity.dxf.end, dxfattribs=attribs)
        elif etype == 'MTEXT':
            pass  # MTEXT written directly to MSP by _auto_dimension
        elif etype == 'INSERT':
            attribs = {}
            for attr in ['layer', 'color', 'xscale', 'yscale', 'zscale', 'rotation', 'lineweight']:
                if entity.dxf.hasattr(attr):
                    attribs[attr] = entity.dxf.get(attr)
            msp.add_blockref(entity.dxf.name, insert=entity.dxf.insert, dxfattribs=attribs)
    msp.delete_entity(dim_entity)
    return True


class IsometricEngine:
    """Core engine for dynamic isometric pipeline drawing."""

    def __init__(self, template_path: str):
        self.template_path = Path(template_path)
        if not self.template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

    def _resolve_pipe_angle(self, seg: Dict, start_block: str, start_rotation: float) -> float:
        dir_by_variant = seg.get("direction_by_variant") or {}
        direction = dir_by_variant.get(start_block, seg.get("direction"))
        if direction is not None:
            angle = VARIANT_DIRECTIONS.get(start_block, {}).get(direction)
            if angle is None:
                raise ValueError(f"Unknown direction '{direction}' for variant {start_block}")
        else:
            angle_by_variant = seg.get("angle_by_variant") or {}
            angle = angle_by_variant.get(start_block, seg.get("angle") or 0)
            if not seg.get("no_transform", False) and start_block not in angle_by_variant:
                angle = transform_angle(angle, start_block)
        return (angle + start_rotation) % 360

    def _build_dim_map(self, start_rotation: float) -> Dict[float, Tuple[float, float]]:
        angle_dim_map = {}
        for base_angle, (dim_a, obl_a) in _BASE_DIM_MAP.items():
            rotated = (base_angle + start_rotation) % 360
            rotated_dim = (dim_a + start_rotation) % 360
            rotated_obl = (obl_a + start_rotation) % 360
            angle_dim_map[rotated] = (rotated_dim, rotated_obl)
        return angle_dim_map

    def _auto_dimension(self, doc, msp, p1, p2, line_angle, length_mm,
                        start_block, angle_dim_map, dim_offset=6.0,
                        side="default", start_rotation=0):
        line_angle_key = round(line_angle) % 360
        if line_angle_key not in angle_dim_map:
            return None
        dim_angle, oblique_deg = angle_dim_map[line_angle_key]

        # Isometric 3 axes: X=30°, Y=150°, Z=90° (plus rotation offset).
        # Lateral directions for each pipe axis:
        #   Pipe on X (30/210) → lateral = Y (330/150)
        #   Pipe on Y (150/330) or Z (90/270) → lateral = X (30/210)
        r = start_rotation
        base_la = round((line_angle - r) % 360)

        is_sk_orthogonal = start_block not in (
            "start-BR", "start-BL", "start-TR", "start-TL"
        )

        if is_sk_orthogonal:
            # SK / orthogonal: laterals are simply perpendicular to the pipe
            _right_obl = (line_angle_key + 90) % 360
            _left_obl  = (line_angle_key + 270) % 360
            _top_obl   = 90.0
            _bottom_obl = 270.0
        else:
            if base_la in (30, 210):
                _right_obl = (330 + r) % 360
                _left_obl  = (150 + r) % 360
            else:
                _right_obl = (30 + r) % 360
                _left_obl  = (210 + r) % 360

            # "Reverse" isometric directions walk in the opposite sense, so
            # right/left obliques are mirrored:
            #   back  (330°): walking at 330°, right hand → 240° ≈ 210° (not 30°)
            #   left  (210°): walking at 210°, right hand → 120° ≈ 150° (not 330°)
            #   down  (270°): walking at 270°, right hand → 180° ≈ 210° (not 30°)
            if base_la in (210, 330, 270):
                _right_obl, _left_obl = _left_obl, _right_obl

            # For vertical isometric pipes, avoid top/bottom parallel to pipe
            if base_la in (90, 270):
                _top_obl    = (330 + r) % 360
                _bottom_obl = (150 + r) % 360
            else:
                _top_obl    = 90.0
                _bottom_obl = 270.0

        # Resolve oblique and dimstyle.
        # Explicit sides (top/bottom/right/left) bypass variant adjustments.
        # Relative sides (default/opposite) apply isometric variant adjustments.
        # Dimstyle per face — universal for all isometric variants:
        #   base_la in (150, 330)  [Y-axis pipes]: top/bottom=ISO -30, right/left=ISO 30
        #   all other pipes (X / Z axis):          top/bottom=ISO 30,  right/left=ISO -30
        _iso = not is_sk_orthogonal
        if side == "top":
            oblique_deg = _top_obl
            dimstyle_name = "ISOMETRIC -30"
            if _iso and base_la not in (150, 330):
                dimstyle_name = "ISOMETRIC 30"
        elif side == "bottom":
            oblique_deg = _bottom_obl
            dimstyle_name = "ISOMETRIC -30"
            if _iso and base_la not in (150, 330):
                dimstyle_name = "ISOMETRIC 30"
        elif side == "right":
            oblique_deg = _right_obl
            dimstyle_name = "ISOMETRIC -30"
            if _iso and base_la in (150, 330):
                dimstyle_name = "ISOMETRIC 30"
        elif side == "left":
            oblique_deg = _left_obl
            dimstyle_name = "ISOMETRIC -30"
            if _iso and base_la in (150, 330):
                dimstyle_name = "ISOMETRIC 30"
        else:
            # 'default' / 'opposite'
            if side == "opposite":
                oblique_deg = (oblique_deg + 180) % 360
            if is_sk_orthogonal:
                # Standard orthogonal: no isometric text-slant adjustments
                dimstyle_name = "STANDARD"
            else:
                dimstyle_name = "ISOMETRIC -30"
                if start_block == "start-BL":
                    dimstyle_name = "ISOMETRIC 30"
                    oblique_deg = (360 - oblique_deg) % 360
                elif start_block == "start-TR":
                    dimstyle_name = "ISOMETRIC 30"
                    oblique_deg = (oblique_deg + 180) % 360
                elif start_block == "start-TL":
                    if line_angle_key in (90, 270):
                        dimstyle_name = "ISOMETRIC 30"
                        oblique_deg = (360 - oblique_deg) % 360

        perp_rad = math.radians(oblique_deg)
        cos_p, sin_p = math.cos(perp_rad), math.sin(perp_rad)
        base = (p2[0] + dim_offset * cos_p, p2[1] + dim_offset * sin_p)

        dim_text = str(int(length_mm))
        # Render a temporary dim so ezdxf registers any required block definitions
        # (e.g. _DOTSMALL) and gives us a geometry block to harvest styling from.
        dimensi = msp.add_linear_dim(
            base=base, p1=p1, p2=p2, angle=dim_angle,
            text=dim_text, dimstyle=dimstyle_name,
            dxfattribs={"layer": "DIM SK"}, override=DIM_OVERRIDES,
        )
        dimensi.render()
        dim_entity = dimensi.dimension

        # Compute correct isometric geometry and harvest styling.
        geom_block = doc.blocks.get(dim_entity.dxf.geometry)
        gdata = _compute_dim_geometry(
            dim_entity, p1, p2, base, dim_angle, oblique_deg, geom_block
        )

        # Delete the ezdxf-rendered dim entity and its geometry block entirely.
        # We draw everything ourselves directly into MSP so nothing from the
        # ezdxf-generated geometry block ends up in the file.
        geom_block_name = dim_entity.dxf.geometry
        msp.delete_entity(dim_entity)
        if geom_block_name and geom_block_name in doc.blocks:
            try:
                doc.blocks.delete_block(geom_block_name, safe=False)
            except Exception:
                pass

        tc = gdata['tc']
        if tc is None:
            return None

        arrow1 = gdata['arrow1']
        arrow2 = gdata['arrow2']
        cos_ext = gdata['cos_ext']
        sin_ext = gdata['sin_ext']
        cos_dim = gdata['cos_dim']
        line_attribs = gdata['line_attribs']
        insert_block_name = gdata['insert_block_name']
        insert_attribs = gdata['insert_attribs']
        mtext_props = gdata['mtext_props']

        # Extension lines (small gap at the measured-point end).
        ext_gap = 0.4
        for p_pt, arrow in ((p1, arrow1), (p2, arrow2)):
            msp.add_line(
                (p_pt[0] + ext_gap * cos_ext, p_pt[1] + ext_gap * sin_ext, 0),
                (arrow[0], arrow[1], 0),
                dxfattribs=line_attribs,
            )

        # Dimension line split around tc.
        total = math.sqrt((arrow2[0] - arrow1[0])**2 + (arrow2[1] - arrow1[1])**2)
        # Always derive char_ht from DIM_OVERRIDES — the dimstyle in the template
        # may have a much larger char_height that would make text oversized.
        char_ht = DIM_OVERRIDES.get("dimtxt", 2.5) * DIM_OVERRIDES.get("dimscale", 0.8)

        # Measure text width, then open an ASYMMETRIC gap:
        #   - clearance_start: space before the first character (slightly more)
        #   - clearance_end  : space after the last character (tighter)
        # This matches the visual convention where a number "breathes" more
        # before its first digit than after its last.
        # Map dimstyle → text style directly (reliable: we set dimstyle_name ourselves).
        # mtext_props.get('style') is NOT used because ezdxf may omit the style
        # attribute in the geometry-block MTEXT, causing silent fallback to 'ISO 30'
        # for all dims — making gap estimates wrong for 'ISOMETRIC -30' dims.
        _DIM_TO_TEXT_STYLE = {'ISOMETRIC 30': 'ISO 30', 'ISOMETRIC -30': 'ISO-30'}
        _text_style = _DIM_TO_TEXT_STYLE.get(dimstyle_name, 'ISO 30')
        text_w = _estimate_text_width(dim_text, char_ht, text_style=_text_style)
        clearance_start = 0.85  # before first char  ← adjust di sini
        clearance_end   = 0.35  # after last char    ← adjust di sini
        cap = total * 0.44

        if total > 1e-6:
            dv = ((arrow2[0] - arrow1[0]) / total, (arrow2[1] - arrow1[1]) / total)
        else:
            dv = (cos_dim, math.sin(math.radians(dim_angle)))

        # Determine which gap end sits before the first character (reading direction).
        # Text flows at dim_angle; first char is in the dir(dim_angle+180°) direction.
        # If that aligns with +dv → first char is on the ge side.
        # If it aligns with -dv → first char is on the gs side.
        _r180 = math.radians(dim_angle + 180.0)
        _dot  = math.cos(_r180) * dv[0] + math.sin(_r180) * dv[1]
        _ge_is_start = _dot >= 0   # True = ge side holds the first char

        # "ISOMETRIC 30" is used on mirrored isometric faces (e.g. side="right"/"left"
        # for non-vertical pipes in BR/TL). The visual reading direction is reversed on
        # these faces, so flip the clearance assignment.
        if dimstyle_name == "ISOMETRIC 30":
            _ge_is_start = not _ge_is_start

        if _ge_is_start:
            hg_ge = min(text_w / 2.0 + clearance_start, cap)
            hg_gs = min(text_w / 2.0 + clearance_end,   cap)
        else:
            hg_ge = min(text_w / 2.0 + clearance_end,   cap)
            hg_gs = min(text_w / 2.0 + clearance_start, cap)

        gs = (tc[0] - hg_gs * dv[0], tc[1] - hg_gs * dv[1])
        ge = (tc[0] + hg_ge * dv[0], tc[1] + hg_ge * dv[1])
        msp.add_line((arrow1[0], arrow1[1], 0), (gs[0], gs[1], 0), dxfattribs=line_attribs)
        msp.add_line((ge[0], ge[1], 0), (arrow2[0], arrow2[1], 0), dxfattribs=line_attribs)

        # Dot markers at arrow tips.
        if insert_block_name and insert_block_name in doc.blocks:
            for arrow in (arrow1, arrow2):
                msp.add_blockref(insert_block_name,
                                 insert=(arrow[0], arrow[1], 0),
                                 dxfattribs=insert_attribs)

        # MTEXT at tc — MiddleCenter (attachment_point=5) + width=0 is correct for
        # AutoCAD/DWG: AutoCAD centers the text bounding box at the insert point.
        # ezdxf SVG/PDF rendering is fixed separately by fix_mtext_for_ezdxf_render()
        # in dxf_to_svg.py which converts attachment_point=5 → 4 (MiddleLeft) with
        # the insert shifted by -text_w/2 before rendering. Do NOT set width > 0 here
        # as that left-aligns text within the box in AutoCAD, shifting it out of the gap.
        rotation = dim_angle
        mt_attribs = {
            'insert': (tc[0], tc[1], 0),
            'attachment_point': 5,
            'char_height': char_ht,
            'rotation': rotation,
            'width': 0,
        }
        for attr in ('layer', 'color', 'style'):
            if attr in mtext_props:
                mt_attribs[attr] = mtext_props[attr]
        if 'layer' not in mt_attribs:
            mt_attribs['layer'] = 'DIM SK'
        msp.add_mtext(dim_text, dxfattribs=mt_attribs)

        return None  # dim_entity was deleted; all visual elements added directly to MSP

    def generate(self, request: Dict[str, Any], output_path: str) -> Tuple[bool, str, Optional[str]]:
        """Generate DXF from request dict. Returns (success, message, output_file_path)."""
        try:
            module = request.get("module", "SR")
            start_block = request.get("start_block", "start-BR")
            req_insert = request.get("start_insert")
            if req_insert is None:
                req_insert = VARIANT_START_INSERT.get(start_block, (209.52, 124.98))
            start_insert = tuple(req_insert)
            start_rotation = request.get("start_rotation") or 0
            segments = request["segments"]
            combined_dims = request.get("combined_dims", [])

            # SK: accumulate pipe points → LWPOLYLINE (warna global sk_line_color, const_width=1)
            # Pipes are rendered AFTER all components so they appear on top (higher z-order).
            _sk_color_raw = request.get("sk_line_color")
            _sk_color = int(_sk_color_raw) if _sk_color_raw and int(_sk_color_raw) > 0 else None
            sk_style = {**STYLE_SK, "color": _sk_color} if _sk_color else STYLE_SK

            sk_poly_pts: List[Tuple[float, float]] = []
            sk_deferred_polys: List[List[Tuple[float, float]]] = []
            pending_sk_dims: List[dict] = []

            def collect_sk_poly():
                """Save current pipe segment for deferred rendering (before component insert)."""
                if len(sk_poly_pts) >= 2:
                    sk_deferred_polys.append(list(sk_poly_pts))
                sk_poly_pts.clear()

            def flush_all_sk_polys():
                """Render all deferred pipe polylines then dimensions — called once at end."""
                if len(sk_poly_pts) >= 2:
                    sk_deferred_polys.append(list(sk_poly_pts))
                sk_poly_pts.clear()
                for pts in sk_deferred_polys:
                    pl = msp.add_lwpolyline(pts, dxfattribs=sk_style)
                    pl.dxf.const_width = 1.0
                sk_deferred_polys.clear()
                for d in pending_sk_dims:
                    self._auto_dimension(**d)
                pending_sk_dims.clear()

            doc = ezdxf.readfile(str(self.template_path))
            msp = doc.modelspace()

            if start_block and start_block not in doc.blocks:
                # SK atau custom module: tidak ada start block di template, skip saja
                print(f"[WARNING] Start block '{start_block}' not found in template, skipping")



            if module == "SK":
                angle_dim_map = dict(_BASE_DIM_MAP_SK)
            else:
                angle_dim_map = self._build_dim_map(start_rotation)
            cursor = start_insert
            prev_angle = None
            seg_positions: Dict[int, Dict[str, Tuple[float, float]]] = {}

            for i, seg in enumerate(segments):
                seg_type = seg.get("type", "pipe")

                if seg_type == "pipe":
                    length_mm = seg.get("length_mm")
                    if not length_mm:
                        warnings.warn(f"Segment {i} (pipe) missing length_mm, skipping")
                        seg_positions[i] = {"start": cursor, "end": cursor}
                        continue
                    angle = self._resolve_pipe_angle(seg, start_block, start_rotation)
                    want_dim = seg.get("dimension", False)
                    seg_color = seg.get("color") or None
                    pipe_style = {**STYLE_CYAN, "color": int(seg_color)} if seg_color and int(seg_color) > 0 else STYLE_CYAN

                    if module != "SK" and prev_angle is not None and angle != prev_angle:
                        if should_invert_arc(start_block):
                            arc_to = (angle + 180) % 360
                            arc_from = (prev_angle + 180) % 360
                        else:
                            arc_to = angle
                            arc_from = prev_angle
                        radius = auto_bend_radius(arc_from, arc_to)
                        bend_side = seg.get("bend_side")
                        bsv = (seg.get("bend_side_by_variant") or {}).get(start_block)
                        if bsv is not None:
                            bend_side = bsv
                        center, as_, ae_, new_cursor = calc_bend(
                            cursor, arc_from, arc_to, radius,
                            bend_side=bend_side, real_from=prev_angle
                        )
                        msp.add_arc(center=center, radius=radius,
                                    start_angle=as_, end_angle=ae_, dxfattribs=pipe_style)
                        cursor = new_cursor

                    seg_positions[i] = {"start": cursor}

                    # Per-segment breakline opt-in. When `seg.breakline` is set:
                    #   * style="zigzag"   -> render two half-lines with the
                    #     NTS zigzag symbol at the midpoint.
                    #   * style="straight" -> render a single straight line of
                    #     the visual length, no zigzag (uniform/template look).
                    # In both cases the dimension shows real_length_mm (or
                    # length_mm as fallback). When `breakline` is None the
                    # pipe is drawn at full length_mm with no break.
                    breakline_cfg = seg.get("breakline") or None
                    draw_breakline_symbol = False
                    if breakline_cfg:
                        visual_mm = float(breakline_cfg.get("visual_length_mm") or length_mm)
                        dim_length_mm = float(breakline_cfg.get("real_length_mm") or length_mm)
                        bl_style = (breakline_cfg.get("style") or "zigzag").lower()
                        draw_breakline_symbol = (bl_style == "zigzag")
                    else:
                        visual_mm = length_mm
                        dim_length_mm = length_mm
                    is_capped = draw_breakline_symbol  # only zigzag triggers split-line drawing
                    visual_units = mm_to_units(visual_mm)

                    endpoint = calc_endpoint(cursor, angle, visual_units)

                    if module == "SK":
                        if not sk_poly_pts:
                            sk_poly_pts.append(cursor)
                        sk_poly_pts.append(endpoint)
                    elif is_capped:
                        # Draw two half-lines with a breakline gap in the middle
                        midpoint = ((cursor[0] + endpoint[0]) / 2,
                                    (cursor[1] + endpoint[1]) / 2)
                        # Gap along pipe (DXF units): matches draw_breakline `gap`
                        break_gap = 1.2
                        gap_start = calc_endpoint(midpoint, angle, -break_gap)
                        gap_end   = calc_endpoint(midpoint, angle,  break_gap)
                        msp.add_line(cursor, gap_start, dxfattribs=pipe_style)
                        msp.add_line(gap_end, endpoint,  dxfattribs=pipe_style)
                        draw_breakline(msp, midpoint, angle, pipe_style)
                    else:
                        msp.add_line(cursor, endpoint, dxfattribs=pipe_style)

                    # Pipe overlays: stamp blocks at fractional positions
                    # along the visual pipe (e.g. direction arrow at 0.5).
                    # Rotation auto-follows the pipe's transformed angle so
                    # the overlay aligns with the pipe regardless of which
                    # start variant is active. The cursor is NOT advanced.
                    overlays = seg.get("overlays") or []
                    if overlays and module != "SK":
                        for ov in overlays:
                            ov_block = (ov.get("block_by_variant") or {}).get(
                                start_block, ov.get("block")
                            )
                            if not ov_block or ov_block not in doc.blocks:
                                if ov_block:
                                    warnings.warn(
                                        f"Segment {i} overlay block "
                                        f"{ov_block!r} not found, skipping"
                                    )
                                continue
                            pos = float(ov.get("position", 0.5))
                            pos = min(max(pos, 0.0), 1.0)
                            ov_x = cursor[0] + (endpoint[0] - cursor[0]) * pos
                            ov_y = cursor[1] + (endpoint[1] - cursor[1]) * pos
                            ov_scale = ov.get("scale") or [1.0, 1.0]
                            # Per-variant rotation_offset override (e.g. when
                            # direction-BR vs direction-BL blocks need
                            # different rotation tuning); falls back to the
                            # overlay's flat rotation_offset.
                            rot_off_map = ov.get("rotation_offset_by_variant") or {}
                            rot_off = rot_off_map.get(
                                start_block,
                                ov.get("rotation_offset", 0.0) or 0.0,
                            )
                            ov_rotation = angle + float(rot_off)
                            msp.add_blockref(
                                ov_block, (ov_x, ov_y),
                                dxfattribs={
                                    "rotation": ov_rotation,
                                    "xscale": float(ov_scale[0]),
                                    "yscale": float(ov_scale[1]),
                                },
                            )

                    if want_dim:
                        dim_kwargs = dict(doc=doc, msp=msp, p1=cursor, p2=endpoint,
                                         line_angle=angle, length_mm=dim_length_mm,
                                         start_block=start_block, angle_dim_map=angle_dim_map,
                                         side=seg.get("dimension_side", "default"),
                                         start_rotation=start_rotation)
                        if module == "SK":
                            # Defer: add dimension after LWPOLYLINE flush so it renders on top
                            pending_sk_dims.append(dim_kwargs)
                        else:
                            self._auto_dimension(**dim_kwargs)
                    cursor = endpoint
                    prev_angle = angle

                elif seg_type == "component":
                    seg_positions[i] = {"start": cursor}

                    is_start_macro = seg.get("block") == "start"

                    if is_start_macro:
                        block_name = start_block
                    else:
                        _block_default = seg.get("block")
                        block_name = (seg.get("block_by_variant") or {}).get(start_block, _block_default)
                        if not block_name:
                            warnings.warn(f"Segment {i} (component) missing block name, skipping")
                            seg_positions[i]["end"] = cursor
                            continue
                    
                    color = seg.get("color")

                    # Smart block: resolve virtual block name ke variant sesuai arah pipa.
                    # Dilakukan sebelum cek doc.blocks agar fallback ke nama asli jika variant
                    # tidak ada di template.
                    if block_name in SMART_BLOCK_VARIANTS and not is_start_macro:
                        _pipe_angle_for_smart = prev_angle if prev_angle is not None else 0
                        _angle_key = round(_pipe_angle_for_smart) % 360
                        _resolved = SMART_BLOCK_VARIANTS[block_name].get(_angle_key)
                        if _resolved and _resolved in doc.blocks:
                            print(f"[SMART_BLOCK] seg {i}: '{block_name}' angle={_angle_key}° → '{_resolved}'")
                            block_name = _resolved
                        elif _resolved:
                            print(f"[SMART_BLOCK] seg {i}: '{block_name}' angle={_angle_key}° → '{_resolved}' NOT IN TEMPLATE, fallback")
                            warnings.warn(
                                f"Smart block variant '{_resolved}' not in template, "
                                f"falling back to '{block_name}'"
                            )
                        else:
                            print(f"[SMART_BLOCK] seg {i}: '{block_name}' angle={_angle_key}° → no mapping found")

                    if block_name not in doc.blocks:
                        seg_positions[i]["end"] = cursor
                        continue

                    # Lookup next pipe angle for potential anchor bend
                    next_pipe_angle = None
                    for fs in segments[i + 1:]:
                        if fs.get("type", "pipe") == "pipe":
                            next_pipe_angle = self._resolve_pipe_angle(fs, start_block, start_rotation)
                            break

                    if (module != "SK"
                            and next_pipe_angle is not None and prev_angle is not None
                            and next_pipe_angle != prev_angle):
                        if should_invert_arc(start_block):
                            arc_to = (next_pipe_angle + 180) % 360
                            arc_from = (prev_angle + 180) % 360
                        else:
                            arc_to = next_pipe_angle
                            arc_from = prev_angle
                        radius = auto_bend_radius(arc_from, arc_to)
                        bend_side = seg.get("bend_side")
                        bsv = (seg.get("bend_side_by_variant") or {}).get(start_block)
                        if bsv is not None:
                            bend_side = bsv
                        center, as_, ae_, new_cursor = calc_bend(
                            cursor, arc_from, arc_to, radius,
                            bend_side=bend_side, real_from=prev_angle
                        )
                        msp.add_arc(center=center, radius=radius,
                                    start_angle=as_, end_angle=ae_, dxfattribs=STYLE_CYAN)
                        mid_rad = math.radians((as_ + ae_) / 2)
                        arc_mid = (center[0] + radius * math.cos(mid_rad),
                                   center[1] + radius * math.sin(mid_rad))
                        cursor = new_cursor
                        prev_angle = next_pipe_angle
                        seg["_arc_mid"] = arc_mid

                    pipe_angle = prev_angle if prev_angle is not None else (next_pipe_angle or 0)

                    # Cek apakah block punya POINT exit
                    blk_def = doc.blocks.get(block_name)
                    has_exit_point = (blk_def is not None and not is_start_macro and
                                      any(e.dxftype() == 'POINT' for e in blk_def))

                    # Save pipe-end cursor BEFORE any retreat — used for base_point alignment.
                    entry_cursor = cursor

                    if has_exit_point:
                        gap = 0; half_gap = 0; min_proj = 0.0
                    else:
                        explicit_gap = seg.get("gap")
                        if explicit_gap is not None:
                            gap = explicit_gap
                            half_gap = gap / 2  # manual gap: center block on pipe end
                            min_proj = -half_gap
                        elif is_start_macro:
                            gap = 0; half_gap = 0; min_proj = 0.0
                        else:
                            gap, retreat, min_proj = get_block_gap(doc, block_name, pipe_angle)
                            half_gap = retreat  # pipe shortening when block extends BEHIND origin
                        if half_gap > 0 and prev_angle is not None:
                            cursor = calc_endpoint(cursor, prev_angle, -half_gap)
                            if module == "SK":
                                if sk_poly_pts:
                                    sk_poly_pts[-1] = cursor
                            else:
                                all_lines = [e for e in msp if e.dxftype() == 'LINE']
                                if all_lines:
                                    all_lines[-1].dxf.end = (cursor[0], cursor[1], 0)

                    if module == "SK":
                        collect_sk_poly()

                    arc_mid = seg.get("_arc_mid")
                    insert_pos = arc_mid if arc_mid else calc_endpoint(cursor, pipe_angle, -min_proj)

                    # Scale
                    sbv = (seg.get("scale_by_variant") or {}).get(start_block)
                    if sbv is not None:
                        scale = list(sbv)
                    else:
                        scale = list(seg.get("scale") or [1.0, 1.0])
                        # Jangan auto-mirror if it's the "start" block itself
                        if seg.get("auto_mirror", True) and not is_start_macro:
                            vs = VARIANT_SCALE.get(start_block, (1, 1))
                            scale = [scale[0] * vs[0], scale[1] * vs[1]]

                    # Rotation
                    comp_dir = (seg.get("direction_by_variant") or {}).get(start_block, seg.get("direction"))
                    rbv = (seg.get("rotation_by_variant") or {}).get(start_block)
                    if comp_dir is not None:
                        dir_angle = VARIANT_DIRECTIONS.get(start_block, {}).get(comp_dir)
                        if dir_angle is None:
                            raise ValueError(f"Unknown direction '{comp_dir}' for {start_block}")
                        canonical = seg.get("canonical_direction_angle", 0)
                        rotation = (dir_angle - canonical + start_rotation) % 360
                    elif rbv is not None:
                        rotation = rbv
                    else:
                        rotation = (seg.get("rotation") or 0) + start_rotation

                    # Entry alignment: base_point (BEDIT) > _ENTRY POINT layer > insert_offset_by_variant
                    # base_point is direction-independent — correct for any pipe_angle.
                    # min_proj (geometry projection) is ONLY reliable when block aligns with pipe_angle.
                    if not is_start_macro:
                        bpx, bpy = get_block_base_point(doc, block_name)
                        if abs(bpx) > 1e-6 or abs(bpy) > 1e-6:
                            # Place block so base_point lands at entry_cursor (pipe end).
                            off_x = bpx * scale[0]
                            off_y = bpy * scale[1]
                            if rotation:
                                r = math.radians(rotation)
                                cos_r, sin_r = math.cos(r), math.sin(r)
                                off_x, off_y = (cos_r * off_x - sin_r * off_y,
                                                sin_r * off_x + cos_r * off_y)
                            insert_pos = (entry_cursor[0] - off_x, entry_cursor[1] - off_y)
                        else:
                            auto_offset = get_block_entry_offset(doc, block_name, scale, rotation)
                            if auto_offset is not None:
                                insert_pos = (insert_pos[0] + auto_offset[0], insert_pos[1] + auto_offset[1])
                            else:
                                ofbv = (seg.get("insert_offset_by_variant") or {}).get(start_block)
                                if ofbv:
                                    insert_pos = (insert_pos[0] + ofbv[0], insert_pos[1] + ofbv[1])

                    attribs = {"xscale": scale[0], "yscale": scale[1], "rotation": rotation}
                    if color is not None:
                        attribs["color"] = color
                    msp.add_blockref(block_name, insert=insert_pos, dxfattribs=attribs)

                    # Use _EXIT point if defined — exact pipe exit position
                    exit_pos = get_block_exit_pos(doc, block_name, insert_pos, scale, rotation)
                    if exit_pos is not None:
                        cursor = exit_pos
                    elif gap > 0:
                        dim_p1 = cursor
                        cursor = calc_endpoint(cursor, pipe_angle, gap)
                        if seg.get("dimension"):
                            self._auto_dimension(doc, msp, p1=dim_p1, p2=cursor,
                                                 line_angle=pipe_angle, length_mm=gap,
                                                 start_block=start_block, angle_dim_map=angle_dim_map,
                                                 side=seg.get("dimension_side", "default"),
                                                 start_rotation=start_rotation)

                elif seg_type == "crossing":
                    # Ditempatkan di koordinat absolut — cursor TIDAK bergerak.
                    # Block otomatis dipilih berdasarkan start_block variant.
                    seg_positions[i] = {"start": cursor, "end": cursor}
                    if module == "SR" and start_block in CROSSING_BLOCK_MAP:
                        crossing_block = CROSSING_BLOCK_MAP[start_block]
                        if crossing_block in doc.blocks:
                            raw_insert = seg.get("insert")
                            default_pos = CROSSING_INSERT_DEFAULT.get(start_block, (280.0, 90.0))
                            if raw_insert and len(raw_insert) >= 2:
                                cx = float(raw_insert[0]) if raw_insert[0] is not None else default_pos[0]
                                cy = float(raw_insert[1]) if raw_insert[1] is not None else default_pos[1]
                                crossing_pos = (cx, cy)
                            else:
                                crossing_pos = default_pos
                            raw_scale = seg.get("scale")
                            sx = float(raw_scale[0]) if raw_scale and raw_scale[0] is not None else 1.0
                            sy = float(raw_scale[1]) if raw_scale and raw_scale[1] is not None else 1.0
                            msp.add_blockref(crossing_block, insert=crossing_pos,
                                             dxfattribs={"xscale": sx, "yscale": sy})

                seg_positions[i]["end"] = cursor

            if module == "SK":
                flush_all_sk_polys()

            # Combined dimensions
            for cd in combined_dims:
                from_idx = cd["from_seg"]
                to_idx = cd["to_seg"]
                if from_idx not in seg_positions or to_idx not in seg_positions:
                    continue
                text_mm = cd.get("text_mm")
                if text_mm is None:
                    def _seg_real_mm(s):
                        bl = s.get("breakline") or None
                        if bl and bl.get("real_length_mm"):
                            return bl["real_length_mm"]
                        return s.get("length_mm", 0)
                    text_mm = sum(
                        _seg_real_mm(segments[j])
                        for j in range(from_idx, to_idx + 1)
                        if segments[j].get("type", "pipe") == "pipe"
                    )
                p1 = seg_positions[from_idx]["start"]
                p2 = seg_positions[to_idx]["end"]
                seg_angle = self._resolve_pipe_angle(segments[from_idx], start_block, start_rotation) \
                    if segments[from_idx].get("type", "pipe") == "pipe" else 90
                if round(seg_angle) % 360 in angle_dim_map:
                    cd_offset = cd.get("dim_offset") or 6.0
                    self._auto_dimension(doc, msp, p1=p1, p2=p2,
                                         line_angle=seg_angle, length_mm=text_mm,
                                         start_block=start_block, angle_dim_map=angle_dim_map,
                                         side=cd.get("side", "default"),
                                         start_rotation=start_rotation,
                                         dim_offset=cd_offset)


            if output_path:
                doc.saveas(output_path)
                return True, "Generated successfully", output_path
            # Return doc in-memory (untuk preview SVG)
            return True, "Generated in-memory", doc

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print("=" * 60)
            print("[ISOMETRIC ENGINE ERROR]")
            print(tb)
            print("=" * 60)
            return False, f"Engine error: {str(e)} | See server logs for traceback", None

    def generate_svg_preview(self, request: Dict[str, Any],
                             customer_data: Optional[Dict[str, Any]] = None,
                             font_dir=None) -> Tuple[bool, str]:
        """Generate drawing in-memory + text-replace customer data + render to SVG.
        Returns (success, svg_string_or_error)."""
        try:
            # Pastikan customer_data tersedia di dalam request agar crossing block
            # logic di generate() bisa membaca material casing.
            if customer_data is not None:
                request = {**request, "customer_data": customer_data}

            # Build drawing in-memory (pass output_path=None → returns doc)
            success, msg, doc = self.generate(request, output_path=None)
            if not success or not hasattr(doc, 'modelspace'):
                return False, msg or "Failed to build drawing"

            # Text replacement — always apply (blank when no customer, customer data when present)
            from app.services.dxf_service import DxfService
            from app.config import get_settings
            settings = get_settings()
            svc = DxfService(
                template_path=str(self.template_path),
                output_path=settings.output_path,
                oda_path=settings.oda_path,
                dwg_version=settings.dwg_version,
            )
            if customer_data:
                replacements = svc.prepare_data(customer_data)
            else:
                replacements = {
                    "[TANGGAL]": svc.generate_tanggal_indonesia(),
                    "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
                    "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[SEKTOR]": "-",
                    "[NO_MGRT]": "-", "[SN_AWAL]": "-", "[KOORDINAT_TAPPING]": "-",
                    "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0", "[21]": "0",
                    # SK-specific blanks (material IDs 1,2,3,5,6)
                    "[NO_SK]": "-",
                    "[1]": "0", "[2]": "0", "[3]": "0", "[6]": "0", "[7]": "0",
                }
            svc.process_modelspace(doc.modelspace(), replacements)
            svc.process_blocks(doc, replacements)

            from app.services.dxf_to_svg import render_dxf_to_svg
            svg = render_dxf_to_svg(doc, font_dir=font_dir)
            return True, svg
        except Exception as e:
            import traceback
            print("[SVG PREVIEW ERROR]", traceback.format_exc())
            return False, f"SVG preview error: {str(e)}"

    def list_blocks(self) -> List[Dict[str, Any]]:
        """Return list of user-usable blocks (filters out system/anonymous blocks)."""
        doc = ezdxf.readfile(str(self.template_path))
        blocks = []
        for blk in doc.blocks:
            name = blk.name
            if name.startswith('*') or name in ('$MODEL_SPACE', '$PAPER_SPACE'):
                continue
            xs, ys = [], []
            has_entry = False
            for e in blk:
                t = e.dxftype()
                if t == 'POINT' and e.dxf.layer == '_ENTRY':
                    has_entry = True
                elif t == 'LINE':
                    xs.extend([e.dxf.start.x, e.dxf.end.x])
                    ys.extend([e.dxf.start.y, e.dxf.end.y])
                elif t in ('ARC', 'CIRCLE'):
                    c, r = e.dxf.center, e.dxf.radius
                    xs.extend([c.x - r, c.x + r])
                    ys.extend([c.y - r, c.y + r])
                elif t == 'LWPOLYLINE':
                    try:
                        for p in e.get_points():
                            xs.append(p[0]); ys.append(p[1])
                    except Exception:
                        pass
            extent = None
            if xs and ys:
                extent = {"x_min": min(xs), "x_max": max(xs),
                          "y_min": min(ys), "y_max": max(ys)}
            blocks.append({"name": name, "has_entry_point": has_entry, "extent": extent})
        
        # Add virtual "start" block for the palette
        blocks.append({
            "name": "start",
            "has_entry_point": True,
            "extent": {"x_min": -5, "x_max": 5, "y_min": -5, "y_max": 5},
            "thumbnail_url": None # IsometricService will handle thumbnail mapping if needed
        })
        return blocks
