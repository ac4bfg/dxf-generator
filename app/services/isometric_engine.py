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
    "start-TR": {"forward": 210, "back": 30, "right": 330, "left": 150, "up": 90, "down": 270},
    "start-TL": {"forward": 330, "back": 150, "right": 30, "left": 210, "up": 90, "down": 270},
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

_BASE_DIM_MAP = {
    30: (30, 90), 210: (30, 90),
    150: (330, 270), 330: (330, 270),
    90: (90, 210), 270: (90, 210),
}

STYLE_CYAN = {"layer": "0", "color": 4, "lineweight": 50, "ltscale": 0.5}
STYLE_SK = {"layer": "SK", "color": 5}

DIM_OVERRIDES = {
    "dimasz": 5.0, "dimscale": 0.8, "dimtxt": 2.5,
    "dimblk": "", "dimblk1": "_DOTSMALL", "dimblk2": "_DOTSMALL", "dimsah": 1,
}

BASE_BEND_RADIUS = 1.4

# Maximum visual pipe length (mm) — pipes longer than this are drawn clamped
# with a breakline symbol. Dimension text still shows the real length.
MAX_VISUAL_LENGTH_MM = 2500


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


def fix_oblique_geometry(doc, dim_entity, p1, p2, base, dim_angle_deg, oblique_deg):
    geom_block = doc.blocks.get(dim_entity.dxf.geometry)
    if not geom_block:
        return
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
    lines = [e for e in geom_block if e.dxftype() == 'LINE']
    inserts = [e for e in geom_block if e.dxftype() == 'INSERT']
    ext_lines, dim_lines = [], []
    for line in lines:
        sx, sy = line.dxf.start.x, line.dxf.start.y
        d1 = math.sqrt((sx - p1[0])**2 + (sy - p1[1])**2)
        d2 = math.sqrt((sx - p2[0])**2 + (sy - p2[1])**2)
        (ext_lines if d1 < 3 or d2 < 3 else dim_lines).append(line)
    for el in ext_lines:
        sx, sy = el.dxf.start.x, el.dxf.start.y
        d1 = math.sqrt((sx - p1[0])**2 + (sy - p1[1])**2)
        d2 = math.sqrt((sx - p2[0])**2 + (sy - p2[1])**2)
        if d1 < d2:
            el.dxf.start = (p1[0] + d1 * cos_ext, p1[1] + d1 * sin_ext, 0)
            el.dxf.end = (arrow1[0], arrow1[1], 0)
        else:
            el.dxf.start = (p2[0] + d2 * cos_ext, p2[1] + d2 * sin_ext, 0)
            el.dxf.end = (arrow2[0], arrow2[1], 0)
    if len(dim_lines) >= 2:
        mt = [e for e in geom_block if e.dxftype() == 'MTEXT']
        tc = (mt[0].dxf.insert.x, mt[0].dxf.insert.y) if mt \
            else ((arrow1[0] + arrow2[0]) / 2, (arrow1[1] + arrow2[1]) / 2)
        total = math.sqrt((arrow2[0] - arrow1[0])**2 + (arrow2[1] - arrow1[1])**2)
        orig = sum(math.sqrt((d.dxf.end.x - d.dxf.start.x)**2 + (d.dxf.end.y - d.dxf.start.y)**2) for d in dim_lines)
        hg = max(total - orig, 2.0) / 2
        dv = ((arrow2[0] - arrow1[0]) / total, (arrow2[1] - arrow1[1]) / total) if total > 0 else (cos_dim, sin_dim)
        gs = (tc[0] - hg * dv[0], tc[1] - hg * dv[1])
        ge = (tc[0] + hg * dv[0], tc[1] + hg * dv[1])
        for dl in dim_lines:
            mx = (dl.dxf.start.x + dl.dxf.end.x) / 2
            my = (dl.dxf.start.y + dl.dxf.end.y) / 2
            if math.sqrt((mx - arrow1[0])**2 + (my - arrow1[1])**2) < math.sqrt((mx - arrow2[0])**2 + (my - arrow2[1])**2):
                dl.dxf.start = (arrow1[0], arrow1[1], 0); dl.dxf.end = (gs[0], gs[1], 0)
            else:
                dl.dxf.start = (arrow2[0], arrow2[1], 0); dl.dxf.end = (ge[0], ge[1], 0)
    for ins in inserts:
        d1 = math.sqrt((ins.dxf.insert.x - p1[0])**2 + (ins.dxf.insert.y - p1[1])**2)
        d2 = math.sqrt((ins.dxf.insert.x - p2[0])**2 + (ins.dxf.insert.y - p2[1])**2)
        ins.dxf.insert = (arrow1[0], arrow1[1], 0) if d1 < d2 else (arrow2[0], arrow2[1], 0)


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
            attribs = {}
            for attr in ['layer', 'color', 'char_height', 'rotation', 'attachment_point', 'style', 'width']:
                if entity.dxf.hasattr(attr):
                    attribs[attr] = entity.dxf.get(attr)
            attribs['insert'] = entity.dxf.insert
            msp.add_mtext(entity.dxf.text, dxfattribs=attribs)
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
                        start_block, angle_dim_map, dim_offset=6.0, do_explode=True,
                        side="default", start_rotation=0):
        if line_angle not in angle_dim_map:
            return None
        dim_angle, oblique_deg = angle_dim_map[line_angle]

        # Compute lateral (right/left) isometric directions for this pipe angle.
        # Isometric 3 axes: X=30°, Y=150°, Z=90° (plus rotation offset).
        # Pipe on X-axis → lateral = Y-axis (330°/150°)
        # Pipe on Y-axis → lateral = X-axis (30°/210°)
        # Pipe on Z-axis → lateral = X-axis (30°/210°)
        r = start_rotation
        base_la = round((line_angle - r) % 360)
        if base_la in (30, 210):
            _right_obl = (330 + r) % 360
            _left_obl  = (150 + r) % 360
        else:  # 150/330 (Y-axis) and 90/270 (Z-axis) both use X-axis laterals
            _right_obl = (30 + r) % 360
            _left_obl  = (210 + r) % 360

        # For vertical pipes (Z-axis), top/bottom 90°/270° obliques would be parallel
        # to the pipe → dim merges with pipe line. Use Y-iso axis instead (330°/150°).
        if base_la in (90, 270):
            _top_obl = (330 + r) % 360
            _bottom_obl = (150 + r) % 360
        else:
            _top_obl = 90.0
            _bottom_obl = 270.0

        # Resolve oblique and dimstyle based on side value.
        # Explicit positions (top/bottom/right/left) bypass variant adjustments.
        # Relative positions (default/opposite) apply variant adjustments.
        if side == "top":
            oblique_deg = _top_obl
            dimstyle_name = "ISOMETRIC -30"
            # Vertical pipe: text rotation 90° + ISO-30 oblique tilts text to kanan-atas.
            # Swap to ISO 30 so text tilts to kiri-atas (correct isometric direction).
            if base_la in (90, 270) and start_block in ("start-BR", "start-TL"):
                dimstyle_name = "ISOMETRIC 30"
        elif side == "bottom":
            oblique_deg = _bottom_obl
            dimstyle_name = "ISOMETRIC -30"
            if base_la in (90, 270) and start_block in ("start-BR", "start-TL"):
                dimstyle_name = "ISOMETRIC 30"
        elif side == "right":
            oblique_deg = _right_obl
            # Variant-specific dimstyle swap for non-vertical pipes only.
            # Vertical pipes keep ISO -30 since rotation 90° + swap gives wrong text lean.
            dimstyle_name = "ISOMETRIC -30"
            if base_la not in (90, 270) and start_block in ("start-BR", "start-TL"):
                dimstyle_name = "ISOMETRIC 30"
        elif side == "left":
            oblique_deg = _left_obl
            dimstyle_name = "ISOMETRIC -30"
            if base_la not in (90, 270) and start_block in ("start-BR", "start-TL"):
                dimstyle_name = "ISOMETRIC 30"
        else:
            # 'default' / 'opposite' — relative to pipe, apply variant-specific adjustments
            if side == "opposite":
                oblique_deg = (oblique_deg + 180) % 360
            dimstyle_name = "ISOMETRIC -30"
            if start_block == "start-BL":
                dimstyle_name = "ISOMETRIC 30"
                oblique_deg = (360 - oblique_deg) % 360
            elif start_block == "start-TR":
                dimstyle_name = "ISOMETRIC 30"
                oblique_deg = (oblique_deg + 180) % 360
            elif start_block == "start-TL":
                if line_angle in (90, 270):
                    dimstyle_name = "ISOMETRIC 30"
                    oblique_deg = (360 - oblique_deg) % 360

        perp_rad = math.radians(oblique_deg)
        cos_p, sin_p = math.cos(perp_rad), math.sin(perp_rad)
        base = (p2[0] + dim_offset * cos_p, p2[1] + dim_offset * sin_p)
        mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
        text_pos = (mid[0] + dim_offset * cos_p, mid[1] + dim_offset * sin_p)

        dimensi = msp.add_linear_dim(
            base=base, p1=p1, p2=p2, angle=dim_angle,
            text=str(int(length_mm)), dimstyle=dimstyle_name,
            dxfattribs={"layer": "DIM SK"}, override=DIM_OVERRIDES,
        )
        dimensi.user_location_override(text_pos)
        dimensi.render()
        dim_entity = dimensi.dimension

        dim_entity.dxf.defpoint = (base[0], base[1], 0)
        fix_oblique_geometry(doc, dim_entity, p1, p2, base, dim_angle, oblique_deg)

        if do_explode:
            explode_dimension(doc, msp, dim_entity)
        return dim_entity

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

            # SK: accumulate pipe points → LWPOLYLINE (blue, const_width=1)
            # Pipes are rendered AFTER all components so they appear on top (higher z-order).
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
                    pl = msp.add_lwpolyline(pts, dxfattribs=STYLE_SK)
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
                                    start_angle=as_, end_angle=ae_, dxfattribs=STYLE_CYAN)
                        cursor = new_cursor

                    seg_positions[i] = {"start": cursor}

                    # Cap visual length at MAX_VISUAL_LENGTH_MM; draw breakline if capped
                    is_capped = length_mm > MAX_VISUAL_LENGTH_MM
                    visual_mm = min(length_mm, MAX_VISUAL_LENGTH_MM)
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
                        # Gap size in DXF units (matches draw_breakline gap param)
                        break_gap = 1.2
                        gap_start = calc_endpoint(midpoint, angle, -break_gap)
                        gap_end   = calc_endpoint(midpoint, angle,  break_gap)
                        msp.add_line(cursor, gap_start, dxfattribs=STYLE_CYAN)
                        msp.add_line(gap_end, endpoint,  dxfattribs=STYLE_CYAN)
                        draw_breakline(msp, midpoint, angle, STYLE_CYAN)
                    else:
                        msp.add_line(cursor, endpoint, dxfattribs=STYLE_CYAN)

                    if want_dim:
                        dim_kwargs = dict(doc=doc, msp=msp, p1=cursor, p2=endpoint,
                                         line_angle=angle, length_mm=length_mm,
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
                    text_mm = sum(
                        segments[j].get("length_mm", 0)
                        for j in range(from_idx, to_idx + 1)
                        if segments[j].get("type", "pipe") == "pipe"
                    )
                p1 = seg_positions[from_idx]["start"]
                p2 = seg_positions[to_idx]["end"]
                seg_angle = self._resolve_pipe_angle(segments[from_idx], start_block, start_rotation) \
                    if segments[from_idx].get("type", "pipe") == "pipe" else 90
                if seg_angle in angle_dim_map:
                    self._auto_dimension(doc, msp, p1=p1, p2=p2,
                                         line_angle=seg_angle, length_mm=text_mm,
                                         start_block=start_block, angle_dim_map=angle_dim_map,
                                         side=cd.get("side", "default"),
                                         start_rotation=start_rotation)

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
                             customer_data: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """Generate drawing in-memory + text-replace customer data + render to SVG.
        Returns (success, svg_string_or_error)."""
        try:
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
                    "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0",
                    # SK-specific blanks (material IDs 1,2,3,5,6)
                    "[NO_SK]": "-",
                    "[1]": "0", "[2]": "0", "[3]": "0", "[6]": "0", "[7]": "0",
                }
            svc.process_modelspace(doc.modelspace(), replacements)
            svc.process_blocks(doc, replacements)

            from app.services.dxf_to_svg import render_dxf_to_svg
            svg = render_dxf_to_svg(doc)
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
