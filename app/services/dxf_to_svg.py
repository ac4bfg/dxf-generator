"""DXF → SVG converter menggunakan ezdxf SVG backend.
Output di-normalize ke DXF native coord space (paper mm) supaya
pipeline drawing (yang pakai DXF coords) bisa overlay dengan benar."""
import math
import re
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.addons.drawing import layout
from ezdxf.addons.drawing.config import Configuration, LineweightPolicy

# MTEXT format-code stripper (used by fix_mtext_for_ezdxf_render)
_MTEXT_CODE_RE = re.compile(r'\\[A-Za-z][^;]*;|\\P|\{|\}')

# Process-level caches — keyed by (style_name) and (style_name, text, char_ht).
# Safe to cache indefinitely: font metrics and text widths don't change within a process.
_FONT_OBJ_CACHE: dict = {}  # style_name → (font_obj, width_factor) | None
_WIDTH_CACHE: dict = {}     # (style_name, plain_text, char_ht) → float

# Per-character width ratios — same values as _CHAR_W_BY_STYLE in isometric_engine,
# measured from AutoCAD textbox() on 10-char strings divided by 10.
# Used as fallback when ezdxf font measurement is unavailable.
_MTEXT_CHAR_W = {
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
    '_default': {
        '0': 0.7684, '1': 0.7475, '2': 0.7837, '3': 0.7804, '4': 0.7529,
        '5': 0.7785, '6': 0.7686, '7': 0.7701, '8': 0.7771, '9': 0.7686,
        '.': 0.0762,
    },
}


def _get_mtext_render_width(entity, doc, plain_text: str, char_ht: float) -> float:
    """Return text render width in DXF units.

    Primary path: ezdxf font_manager (font object cached per style_name).
    Fallback: per-style measured tables (same source as DWG gap calculation).
    Results are cached per (style_name, plain_text, char_ht) — safe because
    font metrics are process-constant once fonts are configured.
    """
    style_name = entity.dxf.get('style', 'Standard') or 'Standard'

    # Level-1 cache: exact (style, text, height) result
    cache_key = (style_name, plain_text, char_ht)
    cached = _WIDTH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # Level-2 cache: font object per style (avoids font_manager lookups)
    if style_name not in _FONT_OBJ_CACHE:
        try:
            import os
            from ezdxf.fonts import fonts as _ef
            style_ent = doc.styles.get(style_name)
            font_file = (style_ent.dxf.get('font', '') if style_ent else '') or ''
            wf = float((style_ent.dxf.get('width', 1.0) if style_ent else 1.0) or 1.0)
            family = os.path.splitext(font_file)[0].lower() if font_file else 'standard'
            face = _ef.font_manager.find_best_match(family=family)
            if face is None:
                raise LookupError(f'font not found: {family!r}')
            font_obj = _ef.font_manager.get_font(face)
            if font_obj is None or not hasattr(font_obj, 'text_width'):
                raise LookupError('AbstractFont unavailable')
            _FONT_OBJ_CACHE[style_name] = (font_obj, wf)
        except Exception:
            _FONT_OBJ_CACHE[style_name] = None

    font_entry = _FONT_OBJ_CACHE[style_name]
    if font_entry is not None:
        font_obj, wf = font_entry
        try:
            result = font_obj.text_width(plain_text) * char_ht * wf
            _WIDTH_CACHE[cache_key] = result
            return result
        except Exception:
            pass

    table = _MTEXT_CHAR_W.get(style_name, _MTEXT_CHAR_W['_default'])
    result = sum(table.get(c, 0.77) for c in plain_text) * char_ht
    _WIDTH_CACHE[cache_key] = result
    return result


# Position correction for crossing block MTEXT rendered by ezdxf without \A1;.
# Without \A1;, ezdxf uses top-alignment instead of middle, causing a systematic
# vertical offset from the DXF insert point (= AutoCAD MiddleCenter position).
#
# SVG corrections — tuned against the ezdxf SVG renderer.
# Key: rounded text angle (degrees). Value: (dx, dy) to ADD to the DXF insert.
# X corrections are intentionally 0: vertex-centroid measurement was biased by
# letter shapes (C, G have more vertices on their left side); only Y is reliable.
_BLOCK_MTEXT_ANGLE_CORRECTION = {
    -30: (0.55, -0.55),   # crossing-BR, crossing-TL
     30: (0.086, -0.245), # crossing-BL, crossing-TR
}

# PDF corrections — ezdxf PDF renderer places MTEXT at a different offset than SVG.
# Per-angle (dx_delta, dy_delta) added on top of the SVG correction above.
# Positive dx_delta = PDF text moves further RIGHT relative to SVG.
# Positive dy_delta = PDF text moves further UP relative to SVG.
_PDF_MTEXT_DELTA = {
    -30: (-0.55, 0.55),  # BR/TL: PDF needs less X-right and less Y-down than SVG
     30: (  0.0, 0.25),  # BL/TR: PDF only needs less Y-down
}
_BLOCK_MTEXT_ANGLE_CORRECTION_PDF = {
    k: (dx + _PDF_MTEXT_DELTA.get(k, (0.0, 0.0))[0],
        dy + _PDF_MTEXT_DELTA.get(k, (0.0, 0.0))[1])
    for k, (dx, dy) in _BLOCK_MTEXT_ANGLE_CORRECTION.items()
}


def fix_mtext_for_ezdxf_render(doc) -> None:
    """Normalize MTEXT MiddleCenter entities so ezdxf renders them correctly.

    ezdxf SVG/PDF renderer treats MTEXT attachment_point=5 (MiddleCenter) as
    if it were MiddleLeft — the insert ends up at the LEFT edge of the text
    instead of the center. This function converts those entities to
    attachment_point=4 (MiddleLeft) and shifts the insert LEFT by half the
    ACTUAL rendered text width (measured from ezdxf's font system), so the
    visual center stays at the original position and matches the DWG output.

    Also corrects MTEXT inside crossing blocks: without \\A1; the rendered
    centroid is offset from the insert by a measured per-angle amount.

    Call ONLY on a doc used exclusively for rendering (never for DXF/DWG
    saving), because it mutates the entities in place.
    """
    for entity in doc.modelspace().query('MTEXT'):
        if entity.dxf.get('attachment_point', 1) != 5:
            continue
        char_ht  = float(entity.dxf.get('char_height', 2.5) or 2.5)
        rotation = float(entity.dxf.get('rotation', 0) or 0)
        td = entity.dxf.get('text_direction', None)
        if td is not None:
            td_angle = math.degrees(math.atan2(td.y, td.x))
            if abs(td_angle) > 0.1:
                rotation = td_angle
                entity.dxf.rotation = rotation
                entity.dxf.discard('text_direction')
        plain  = _MTEXT_CODE_RE.sub('', entity.text or '').strip()
        text_w = _get_mtext_render_width(entity, doc, plain, char_ht)
        if text_w < 1e-6:
            continue
        tc  = entity.dxf.insert
        rad = math.radians(rotation)
        entity.dxf.insert = (
            float(tc.x) - (text_w / 2) * math.cos(rad),
            float(tc.y) - (text_w / 2) * math.sin(rad),
            float(getattr(tc, 'z', 0)),
        )
        entity.dxf.attachment_point = 4

    # Fix MTEXT inside crossing blocks: without \A1; the ezdxf renderer places the
    # text centroid at a measured offset from the declared insert (top-aligned bias).
    # Apply a pre-measured correction so the visual center matches the DXF insert.
    for block in doc.blocks:
        if 'crossing' not in block.name.lower():
            continue
        for entity in block:
            if entity.dxftype() != 'MTEXT':
                continue
            if entity.dxf.get('attachment_point', 1) not in (4, 5, 6):
                continue
            if '\\A1;' in (entity.text or ''):
                continue
            td = entity.dxf.get('text_direction', None)
            if td is not None:
                angle_deg = math.degrees(math.atan2(td.y, td.x))
            else:
                angle_deg = float(entity.dxf.get('rotation', 0) or 0)
            correction = _BLOCK_MTEXT_ANGLE_CORRECTION.get(round(angle_deg))
            if correction is None:
                continue
            dx, dy = correction
            tc = entity.dxf.insert
            entity.dxf.insert = (
                float(tc.x) + dx,
                float(tc.y) + dy,
                float(getattr(tc, 'z', 0)),
            )


# A3 paper size mm (SR template default)
PAPER_WIDTH_MM = 420.0
PAPER_HEIGHT_MM = 297.0

# Final stroke scale: dibagi berapa nilai stroke-width SVG yg dikeluarkan ezdxf.
# Ini post-process regex — paling reliable untuk ngontrol ketebalan garis preview
# karena `lineweight_scaling`/`fixed_stroke_width` di ezdxf sering saling override.
# Semakin BESAR nilainya, semakin TIPIS garis. 1.0 = apa adanya dari ezdxf.
STROKE_SHRINK_FACTOR = 0.5


def render_dxf_to_svg(doc, paper_width_mm: float = PAPER_WIDTH_MM,
                      paper_height_mm: float = PAPER_HEIGHT_MM,
                      font_dir=None) -> str:
    """Render modelspace of doc ke SVG string, viewBox dalam paper mm coords.

    Output SVG's viewBox = "0 0 paper_width paper_height".
    Content di-wrap dalam <g transform="scale(...)"> supaya ezdxf's 1M coord space
    di-convert ke paper mm.

    font_dir: optional Path ke direktori font AutoCAD (sama seperti PDF renderer).
    Jika diberikan, _prepare_doc() dipanggil sebelum rendering sehingga font,
    style patches, dan MTEXT rewrites identik dengan output PDF.
    """
    if font_dir is not None:
        from pathlib import Path
        from app.services.pdf_renderer import (
            _apply_ezdxf_patches, configure_ezdxf_fonts, patch_styles,
            replace_dot_blocks, rewrite_mtext_inline_fonts,
        )
        _apply_ezdxf_patches()
        configure_ezdxf_fonts(Path(font_dir))
        patch_styles(doc)
        replace_dot_blocks(doc)
        rewrite_mtext_inline_fonts(doc)
        # _single_pass_entity_pipeline intentionally skipped:
        # SVG preview preserves original entity colors (no monochrome conversion).
    fix_mtext_for_ezdxf_render(doc)
    ctx = RenderContext(doc)
    backend = SVGBackend()
    config = Configuration(
        lineweight_policy=LineweightPolicy.ABSOLUTE,
        lineweight_scaling=0.25,
    )
    Frontend(ctx, backend, config=config).draw_layout(doc.modelspace())

    page = layout.Page(
        width=paper_width_mm, height=paper_height_mm,
        units=layout.Units.mm, margins=layout.Margins.all(0),
    )
    # Tidak pakai fixed_stroke_width — biar lineweight_scaling yg jalan.
    # min/max dibiarkan lebar supaya tidak di-clamp.
    settings = layout.Settings(
        fit_page=True,
        min_stroke_width=1,
        max_stroke_width=200,
    )
    svg_raw = backend.get_string(page, settings=settings, xml_declaration=False)

    # Post-process: bagi semua stroke-width dengan STROKE_SHRINK_FACTOR.
    # Ini jaminan kontrol finest regardless of ezdxf internal quirks.
    if STROKE_SHRINK_FACTOR and STROKE_SHRINK_FACTOR != 1.0:
        def _shrink(m):
            try:
                val = float(m.group(1)) / STROKE_SHRINK_FACTOR
                return f'stroke-width: {val:g}'
            except ValueError:
                return m.group(0)
        svg_raw = re.sub(r'stroke-width:\s*([\d.]+)', _shrink, svg_raw)

    # ezdxf 1.3.0 bug: outputs <def>...</def> instead of standard <defs>...</defs>.
    # Browsers treat unknown tag → CSS classes inside tidak berlaku → elements tidak terlihat.
    svg_raw = svg_raw.replace('<def>', '<defs>').replace('</def>', '</defs>')

    # Extract viewBox (ezdxf default: "0 0 1000000 707143" untuk A3)
    vb_match = re.search(r'viewBox="([^"]+)"', svg_raw)
    if not vb_match:
        return svg_raw
    parts = vb_match.group(1).split()
    if len(parts) != 4:
        return svg_raw
    _, _, vb_w, vb_h = map(float, parts)

    # Scale factor: coord 1M (internal) → paper mm
    sx = paper_width_mm / vb_w if vb_w else 1
    sy = paper_height_mm / vb_h if vb_h else 1

    # Compute modelspace DXF bounding box for sketch coordinate calibration.
    dxf_extent_x = 200.0; dxf_extent_y = 120.0
    dxf_min_x = 100.0;    dxf_min_y = 80.0
    try:
        _xs: list = []; _ys: list = []
        for _e in doc.modelspace():
            try:
                t = _e.dxftype()
                if t == 'LINE':
                    _xs += [_e.dxf.start.x, _e.dxf.end.x]
                    _ys += [_e.dxf.start.y, _e.dxf.end.y]
                elif t == 'LWPOLYLINE':
                    _xs += [p[0] for p in _e.get_points()]
                    _ys += [p[1] for p in _e.get_points()]
                elif t == 'INSERT':
                    _xs.append(_e.dxf.insert.x)
                    _ys.append(_e.dxf.insert.y)
            except Exception:
                pass
        if _xs:
            _w = max(_xs) - min(_xs)
            if _w > 1: dxf_extent_x = float(_w)
            dxf_min_x = float(min(_xs))
        if _ys:
            _h = max(_ys) - min(_ys)
            if _h > 1: dxf_extent_y = float(_h)
            dxf_min_y = float(min(_ys))
    except Exception:
        pass

    svg_open_match = re.match(r'^(<svg[^>]*>)([\s\S]*)(</svg>)\s*$', svg_raw)
    if not svg_open_match:
        return svg_raw
    inner = svg_open_match.group(2)

    new_vb = f'0 0 {paper_width_mm} {paper_height_mm}'
    new_svg_open = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="100%" height="100%" '
        f'viewBox="{new_vb}" '
        f'data-dxf-extent-x="{dxf_extent_x:.2f}" '
        f'data-dxf-extent-y="{dxf_extent_y:.2f}" '
        f'data-dxf-min-x="{dxf_min_x:.2f}" '
        f'data-dxf-min-y="{dxf_min_y:.2f}" '
        f'style="background:#1f2937">'
    )
    wrapped_inner = f'<g transform="scale({sx:.6f} {sy:.6f})">{inner}</g>'
    return new_svg_open + wrapped_inner + '</svg>'