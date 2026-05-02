"""Production PDF renderer for SR/SK isometric drawings.

Ported from `testing/test_dxf_pdf.py` (verified visually against AutoCAD's
"DWG to PDF" plot) and adapted for in-memory `ezdxf.Drawing` documents.

Public entry points
-------------------
* ``render_doc_to_pdf(doc, output_path, ...)`` — render a Drawing to a PDF on
  disk (used by the /generate endpoint).
* ``render_doc_to_pdf_bytes(doc, ...)`` — same, returns the PDF bytes
  (used by the /preview-drawing-pdf endpoint).

Both apply, before rendering:
* font support-dirs registration & font-manager rebuild (autocad_fonts +
  Windows Fonts when available)
* monkey-patches for ezdxf 1.3 bugs (weight-aware ``find_best_match``,
  TEXT/MTEXT oblique + width_factor, optional faux-bold via offset fills)
* hybrid font substitution + ``acad.fmp`` emulation (e.g. "Arial" without
  XData → ARIALN.TTF; ``romans.shx`` → ``romand.ttf``)
* MTEXT inline ``\\f<Family>;`` rewrites (e.g. ``Arial`` → ``Arial Narrow``)
* `_DotSmall` block content rewrite to a clean filled CIRCLE + HATCH
* selective monochrome (signatures / blue true-colors stay coloured)
* OLE2FRAME overlay compositing from PNGs in a logo directory

The patches are applied **once per process** via a module-level guard.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext, layout, pymupdf
from ezdxf.addons.drawing.config import (
    BackgroundPolicy,
    ColorPolicy,
    Configuration,
    LineweightPolicy,
)
from ezdxf.fonts import fonts


# ---------------------------------------------------------------------------
# Configuration constants — match testing/test_dxf_pdf.py defaults
# ---------------------------------------------------------------------------

#: Hybrid font substitution table. Keys checked verbatim, then lowercased.
FONT_SUBSTITUTES: Dict[str, str] = {
    # SHX missing -> closest available stroke font
    "simplex.shx":  "romans.shx",
    "simplex":      "romans.shx",
    # TTF missing -> closest available TTF
    "bgothm.ttf":   "arial.ttf",
    "bgothm":       "arial.ttf",
    "isocpeur.ttf": "arial.ttf",
    "isocpeur":     "arial.ttf",
    # bare AutoCAD family name -> arial.ttf
    "arial":        "arial.ttf",
    "Arial":        "arial.ttf",
}

#: MTEXT inline `\f<Family>;` rewrites (family-name level, GDI lookup path).
MTEXT_INLINE_FONT_REWRITES: Dict[str, str] = {
    "Arial": "Arial Narrow",
}

#: ``acad.fmp`` emulation for "incomplete" styles (bare-name + no XData).
INCOMPLETE_FONT_OVERRIDES: Dict[str, str] = {
    # e.g. "times": "TIMESBD.TTF"
}

#: File-level force-overrides applied even when the source file IS available.
FORCE_FONT_OVERRIDES: Dict[str, str] = {
    "romans.shx": "romand.ttf",
}

#: Faux-bold via 9-direction offset fills. Only applies to text in these
#: DXF style names. Empty set disables.
FAUX_BOLD_STYLES: Set[str] = {"ISO 30", "ISO-30", "romans"}
FAUX_BOLD_STROKE = 0.01

#: Lineweight tuning.
LINEWEIGHT_SCALING = 0.7
MIN_LINEWEIGHT = 2.0  # 1/300 inch units; ~0.17mm floor

#: Replace "_dotsmall"-style polyline-with-bulge dots with a clean filled
#: CIRCLE + HATCH so dimension dots don't show a junction line through them.
DOT_BLOCKS_TO_FILL: Set[str] = {"_dotsmall", "_dot"}

#: Color preservation — keep original colour for entities matching either
#: a layer in :data:`PRESERVE_COLOR_LAYERS` or (when enabled) any entity
#: whose ``true_color`` is "bluish" (B dominant).
PRESERVE_COLOR = True
PRESERVE_COLOR_LAYERS: Set[str] = {"PARAF"}
PRESERVE_BLUE_TRUE_COLOR = True
PRESERVE_BLUE_MARGIN = 30


# ---------------------------------------------------------------------------
# One-off ezdxf patches
# ---------------------------------------------------------------------------

_PATCHES_APPLIED = False


def _apply_ezdxf_patches() -> None:
    """Apply ezdxf 1.3 bug fixes once per process.

    Idempotent — safe to call from every render. The actual patches are
    monkey-patches on ezdxf internals; doing them once at module load avoids
    repeatedly wrapping already-wrapped functions.
    """
    global _PATCHES_APPLIED
    if _PATCHES_APPLIED:
        return
    _patch_find_best_match()
    _patch_text_oblique_and_width()
    if FAUX_BOLD_STYLES:
        _patch_faux_bold()
    _PATCHES_APPLIED = True


def _patch_find_best_match() -> None:
    """Make ``fonts.find_best_match`` weight-aware.

    ezdxf's drawing addon calls ``find_best_match(family=..., weight=700,
    italic=...)`` for MTEXT inline ``\\f...|b1;`` codes but leaves the
    ``style`` parameter at its "Regular" default. The matcher's distance
    function weights ``style`` more than ``weight``, so a request for
    weight=700 + style=Regular returns the Regular file (e.g. ARIALN.TTF)
    instead of the Bold variant (ARIALNB.TTF).

    Wrap so that when style is "Regular" but weight ≥ 600 (or italic=True),
    we promote the style string accordingly.
    """
    from ezdxf.fonts import fonts as _f
    _orig = _f.find_best_match

    def _patched(*, family="sans-serif", style="Regular", weight=400,
                 width=5, italic=False):
        if style == "Regular":
            if weight >= 600 and italic:
                style = "Bold Italic"
            elif weight >= 600:
                style = "Bold"
            elif italic:
                style = "Italic"
        return _orig(family=family, style=style, weight=weight,
                     width=width, italic=italic)

    _f.find_best_match = _patched
    try:
        from ezdxf.addons.drawing import properties as _props
        _props.fonts.find_best_match = _patched
    except Exception:
        pass


def _patch_text_oblique_and_width() -> None:
    """Apply the ``oblique`` slant + ``width_factor`` scaling that ezdxf 1.3
    misses when rendering TEXT/MTEXT.

    Resolution: entity overrides style for TEXT; MTEXT pulls straight from
    the style record. We left-multiply each yielded WCS transform by a
    horizontal-shear (×scale-X) matrix in entity-local space.
    """
    from math import tan, radians
    from ezdxf.math import Matrix44
    from ezdxf.entities import Text
    from ezdxf.addons.drawing import text as _t
    from ezdxf.addons.drawing import frontend as _fe

    _orig = _t.simplified_text_chunks

    def _resolve(text):
        ent_oblique = float(getattr(text.dxf, "oblique", 0.0) or 0.0)
        ent_width = float(getattr(text.dxf, "width", 0.0) or 0.0)
        style_obj = None
        try:
            style_obj = text.doc.styles.get(text.dxf.style)
        except Exception:
            pass
        style_oblique = (
            float(getattr(style_obj.dxf, "oblique", 0.0) or 0.0)
            if style_obj is not None else 0.0
        )
        style_width = (
            float(getattr(style_obj.dxf, "width", 1.0) or 1.0)
            if style_obj is not None else 1.0
        )
        if isinstance(text, Text):
            oblique = ent_oblique if abs(ent_oblique) > 1e-9 else style_oblique
            width = ent_width if ent_width > 1e-9 else style_width
        else:
            oblique = style_oblique
            width = style_width
        if oblique > 180:
            oblique -= 360.0
        return oblique, width

    def _patched(text, render_engine, *, font_face):
        oblique, width = _resolve(text)
        ent_width_set = (isinstance(text, Text)
                         and float(getattr(text.dxf, "width", 0) or 0) > 1e-9)
        apply_width_extra = (abs(width - 1.0) > 1e-3
                             and (not ent_width_set or not isinstance(text, Text)))
        if abs(oblique) < 1e-3 and not apply_width_extra:
            yield from _orig(text, render_engine, font_face=font_face)
            return
        sx = tan(radians(oblique)) if abs(oblique) > 1e-3 else 0.0
        wx = width if apply_width_extra else 1.0
        m = Matrix44(
            (wx,  0.0, 0.0, 0.0,
             sx,  1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0,
             0.0, 0.0, 0.0, 1.0)
        )
        for line, transform, cap_height in _orig(
                text, render_engine, font_face=font_face):
            yield line, m @ transform, cap_height

    _t.simplified_text_chunks = _patched
    _fe.simplified_text_chunks = _patched


def _patch_faux_bold() -> None:
    """Render text in ``FAUX_BOLD_STYLES`` with overlapping translated copies
    of the same fill so thin TTF strokes (e.g. romans.ttf at 2 mm) read as
    solid black instead of anti-aliased grey.
    """
    from math import cos, sin, radians
    from ezdxf.math import Matrix44
    from ezdxf.addons.drawing import pipeline as _p

    Pipeline = _p.RenderPipeline2d
    _orig_enter = Pipeline.enter_entity
    _orig_exit = Pipeline.exit_entity
    _orig_draw_text = Pipeline.draw_text
    state = {"bold": False}

    def _enter(self, entity, properties):
        try:
            style = getattr(entity.dxf, "style", "") or ""
            state["bold"] = style in FAUX_BOLD_STYLES
        except Exception:
            state["bold"] = False
        return _orig_enter(self, entity, properties)

    def _exit(self, entity):
        state["bold"] = False
        return _orig_exit(self, entity)

    def _draw_text(self, text, transform, properties, cap_height, dxftype="MTEXT"):
        _orig_draw_text(self, text, transform, properties, cap_height, dxftype)
        if not state["bold"]:
            return
        r = FAUX_BOLD_STROKE * cap_height
        offsets = [(r * cos(radians(a)), r * sin(radians(a)))
                   for a in (0, 45, 90, 135, 180, 225, 270, 315)]
        for dx, dy in offsets:
            shifted = Matrix44.translate(dx, dy, 0) @ transform
            _orig_draw_text(self, text, shifted, properties, cap_height, dxftype)

    Pipeline.enter_entity = _enter
    Pipeline.exit_entity = _exit
    Pipeline.draw_text = _draw_text


# ---------------------------------------------------------------------------
# Font support directory registration
# ---------------------------------------------------------------------------

_FONTS_REGISTERED_DIRS: Optional[List[str]] = None


def _windows_fonts_dirs() -> List[str]:
    """Locate a Windows Fonts directory whether running on native Windows or WSL."""
    candidates = [
        Path("C:/Windows/Fonts"),
        Path("/mnt/c/Windows/Fonts"),
    ]
    user_fonts = Path.home() / "AppData/Local/Microsoft/Windows/Fonts"
    candidates.append(user_fonts)
    return [str(p) for p in candidates if p.is_dir()]


def configure_ezdxf_fonts(font_dir: Path) -> None:
    """Register ``font_dir`` (and Windows Fonts when available) as ezdxf
    support_dirs and rebuild the font-manager cache. Adds the
    :data:`FONT_SUBSTITUTES` entries as synonyms for missing fonts.

    Cached: re-registering the same directory list on subsequent calls is a
    no-op so we don't pay the cache rebuild on every render.
    """
    global _FONTS_REGISTERED_DIRS
    support_dirs = [str(font_dir), *_windows_fonts_dirs()]
    if _FONTS_REGISTERED_DIRS == support_dirs:
        return
    ezdxf.options.support_dirs = support_dirs
    fonts.build_font_manager_cache(path=fonts._get_font_manager_path())
    fonts.font_manager.build(folders=support_dirs, support_dirs=True)

    synonyms: Dict[str, str] = {}
    for src, dst in FONT_SUBSTITUTES.items():
        if fonts.font_manager.has_font(src):
            continue
        if fonts.font_manager.has_font(dst):
            synonyms[src] = dst
    if synonyms:
        fonts.font_manager.add_synonyms(synonyms, reverse=False)

    _FONTS_REGISTERED_DIRS = support_dirs


# ---------------------------------------------------------------------------
# Document-level pre-render patches
# ---------------------------------------------------------------------------

def _style_long_name(style) -> str:
    """Return the TrueType long-name from a STYLE record's XData (group 1000)."""
    if style.xdata is None:
        return ""
    for tags in style.xdata.data.values():
        for tag in tags:
            if tag[0] == 1000:
                return tag[1]
    return ""


def patch_styles(doc) -> None:
    """Three-pass STYLE-table rewrite:

    0. **Force overrides** — applied even when the source font exists, so
       e.g. every ``romans.shx``-referencing style switches to ``romand.ttf``.
    1. **acad.fmp emulation** — bare-name + no XData → use
       :data:`INCOMPLETE_FONT_OVERRIDES` lookup.
    2. **Missing-font fallback** — swap to closest available substitute of
       the same kind via :data:`FONT_SUBSTITUTES`.
    """
    for style in doc.styles:
        font = (style.dxf.font or "").strip()
        if not font:
            continue

        forced = (FORCE_FONT_OVERRIDES.get(font)
                  or FORCE_FONT_OVERRIDES.get(font.lower()))
        if forced and fonts.font_manager.has_font(forced):
            if font != forced:
                style.dxf.font = forced
            font = forced

        bare_name = "." not in font
        long_name = _style_long_name(style)

        if bare_name and not long_name:
            override = INCOMPLETE_FONT_OVERRIDES.get(font.lower())
            if override and fonts.font_manager.has_font(override):
                style.dxf.font = override
                continue

        if fonts.font_manager.has_font(font):
            continue
        replacement = (FONT_SUBSTITUTES.get(font)
                       or FONT_SUBSTITUTES.get(font.lower()))
        if not replacement or not fonts.font_manager.has_font(replacement):
            continue
        style.dxf.font = replacement


_MTEXT_FONT_CODE_RE = re.compile(r"\\f([^|;]+)([^;]*);")


def rewrite_mtext_inline_fonts(doc) -> None:
    """Apply :data:`MTEXT_INLINE_FONT_REWRITES` to every MTEXT raw text."""
    if not MTEXT_INLINE_FONT_REWRITES:
        return

    def _swap(match: re.Match) -> str:
        family = match.group(1)
        flags = match.group(2)
        new = MTEXT_INLINE_FONT_REWRITES.get(family, family)
        return f"\\f{new}{flags};"

    containers = [doc.modelspace(),
                  *(doc.layout(name) for name in doc.layout_names()
                    if name != "Model")]
    for container in containers:
        for e in container:
            if e.dxftype() != "MTEXT":
                continue
            new_text = _MTEXT_FONT_CODE_RE.sub(_swap, e.text)
            if new_text != e.text:
                e.text = new_text
    for blk in doc.blocks:
        for e in blk:
            if e.dxftype() != "MTEXT":
                continue
            new_text = _MTEXT_FONT_CODE_RE.sub(_swap, e.text)
            if new_text != e.text:
                e.text = new_text


def replace_dot_blocks(doc) -> None:
    """Replace LWPOLYLINE-with-bulge "dots" inside each
    :data:`DOT_BLOCKS_TO_FILL` block with a CIRCLE + solid HATCH so the
    rendered dot is a clean filled disc (no 2-arc junction line).
    """
    for block in doc.blocks:
        if block.name.lower() not in DOT_BLOCKS_TO_FILL:
            continue
        polyline = next(
            (e for e in block
             if e.dxftype() == "LWPOLYLINE"
             and len(list(e.get_points())) == 2),
            None,
        )
        if polyline is None:
            continue
        pts = list(polyline.get_points())
        x0, y0 = pts[0][:2]
        x1, y1 = pts[1][:2]
        seg_len = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        const_width = float(getattr(polyline.dxf, "const_width", 0.0) or 0.0)
        radius = seg_len / 2.0 + const_width / 2.0
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        layer_name = polyline.dxf.layer
        color = polyline.dxf.color

        block.delete_entity(polyline)
        block.add_circle((cx, cy), radius,
                         dxfattribs=dict(layer=layer_name, color=color))
        hatch = block.add_hatch(color=color, dxfattribs=dict(layer=layer_name))
        edge = hatch.paths.add_edge_path()
        edge.add_arc(center=(cx, cy), radius=radius,
                     start_angle=0, end_angle=360, ccw=True)


def _is_bluish(true_color: int, margin: int = PRESERVE_BLUE_MARGIN) -> bool:
    r = (true_color >> 16) & 0xFF
    g = (true_color >> 8) & 0xFF
    b = true_color & 0xFF
    return b > r + margin and b > g + margin


def _should_preserve(entity) -> bool:
    if entity.dxf.layer in PRESERVE_COLOR_LAYERS:
        return True
    if PRESERVE_BLUE_TRUE_COLOR:
        tc = entity.dxf.get("true_color")
        if tc is not None and _is_bluish(tc):
            return True
    return False


def force_monochrome(doc) -> None:
    """Set ACI 7 (auto B/W) on every entity that doesn't match the preserve
    rules. Combined with ``ColorPolicy.COLOR``, this gives a monochrome plot
    with selected entities (signatures) keeping their original colour.
    """
    if not PRESERVE_COLOR:
        return
    containers = [doc.modelspace(),
                  *(doc.layout(name) for name in doc.layout_names()
                    if name != "Model")]
    for container in containers:
        for e in container:
            if _should_preserve(e):
                continue
            if e.dxf.hasattr("true_color"):
                e.dxf.discard("true_color")
            e.dxf.color = 7
    for blk in doc.blocks:
        for e in blk:
            if _should_preserve(e):
                continue
            if e.dxf.hasattr("true_color"):
                e.dxf.discard("true_color")
            e.dxf.color = 7


# ---------------------------------------------------------------------------
# OLE2FRAME overlay
# ---------------------------------------------------------------------------

def collect_ole_frames(doc) -> List[Dict]:
    """Return list of OLE2FRAME bounding boxes in modelspace, sorted top-down
    (highest mm-Y first) and re-indexed 1..N. Each entry: ``{idx, handle,
    descr, x1, x2, y1, y2}`` (mm)."""
    rows: List[Dict] = []
    for e in doc.modelspace().query("OLE2FRAME"):
        descr, ul, lr = "", None, None
        for tag in e.acdb_ole2frame:
            if tag.code == 3:
                descr = tag.value
            elif tag.code == 10:
                ul = tag.value
            elif tag.code == 11:
                lr = tag.value
        if ul and lr:
            rows.append(dict(handle=e.dxf.handle, descr=descr,
                             x1=min(ul[0], lr[0]), x2=max(ul[0], lr[0]),
                             y1=min(ul[1], lr[1]), y2=max(ul[1], lr[1])))
    rows.sort(key=lambda f: -f["y2"])
    for i, row in enumerate(rows, start=1):
        row["idx"] = i
    return rows


def composite_ole_overlays_inplace_bytes(pdf_bytes: bytes,
                                         frames: List[Dict],
                                         overlays: Mapping[int, str],
                                         page_height_mm: float) -> bytes:
    """Stamp each PNG over its OLE2FRAME bounding box and return the new PDF
    bytes. Pure in-memory: no disk I/O, no `saveIncr` rewrite of the entire
    file. ~10× faster than the disk-based path on the OLE step alone.
    """
    if not frames or not overlays:
        return pdf_bytes
    import pymupdf as _pm

    mm_to_pt = 72.0 / 25.4
    pdf = _pm.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = pdf[0]
        h_pt = page_height_mm * mm_to_pt

        n_inserted = 0
        for f in frames:
            png = overlays.get(f["idx"])
            if not png:
                continue
            png_path = Path(png)
            if not png_path.exists():
                continue
            x0 = f["x1"] * mm_to_pt
            x1 = f["x2"] * mm_to_pt
            y0 = h_pt - f["y2"] * mm_to_pt
            y1 = h_pt - f["y1"] * mm_to_pt
            rect = _pm.Rect(x0, y0, x1, y1)
            page.insert_image(rect, filename=str(png_path),
                              keep_proportion=True)
            n_inserted += 1
        if n_inserted == 0:
            return pdf_bytes
        # `tobytes()` returns a fully-rewritten PDF; pass `garbage` and
        # `deflate` so the result is comparable to a fresh save.
        return pdf.tobytes(garbage=3, deflate=True)
    finally:
        pdf.close()


def _resolve_ole_overlays(logo_dir: Optional[Path],
                          frames: List[Dict]) -> Dict[int, str]:
    """Look for ``logo_dir/drawing<N>.png`` for each OLE index. Returns a
    ``{idx: absolute_path_string}`` mapping; entries missing on disk are
    omitted."""
    if not logo_dir or not logo_dir.is_dir():
        return {}
    out: Dict[int, str] = {}
    for f in frames:
        candidate = logo_dir / f"drawing{f['idx']}.png"
        if candidate.exists():
            out[f["idx"]] = str(candidate)
    return out


# ---------------------------------------------------------------------------
# Render entry points
# ---------------------------------------------------------------------------

def _select_render_layout(doc, layout_name: str = "SR"):
    """Pick the layout to render. ezdxf can't render through paperspace
    viewports, so when the named paperspace contains only viewports we fall
    back to modelspace (which is authored at paper scale in this template)."""
    available = doc.layout_names()
    if layout_name not in available:
        return doc.modelspace(), None
    paperspace = doc.layout(layout_name)
    non_vp = [e for e in paperspace if e.dxftype() != "VIEWPORT"]
    vp_count = sum(1 for e in paperspace if e.dxftype() == "VIEWPORT")
    if not non_vp and vp_count >= 1:
        return doc.modelspace(), paperspace
    return paperspace, paperspace


def _build_pdf_bytes(doc, layout_name: str,
                     filter_func=None) -> tuple[bytes, layout.Page]:
    from app.services.dxf_to_svg import fix_mtext_for_ezdxf_render
    fix_mtext_for_ezdxf_render(doc)
    target, paperspace = _select_render_layout(doc, layout_name)

    ctx = RenderContext(doc)
    if paperspace is not None:
        ctx.set_current_layout(paperspace)

    color_policy = ColorPolicy.COLOR if PRESERVE_COLOR else ColorPolicy.BLACK
    cfg = Configuration(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=color_policy,
        lineweight_policy=LineweightPolicy.ABSOLUTE,
        lineweight_scaling=LINEWEIGHT_SCALING,
        min_lineweight=MIN_LINEWEIGHT,
    )

    backend = pymupdf.PyMuPdfBackend()
    Frontend(ctx, backend, config=cfg).draw_layout(
        target, finalize=True, filter_func=filter_func
    )

    if paperspace is not None:
        page = layout.Page.from_dxf_layout(paperspace)
    else:
        # No paperspace — fall back to A3 landscape.
        page = layout.Page(420, 297, layout.Units.mm,
                           margins=layout.Margins.all(0))

    settings = layout.Settings(fit_page=True, scale=1)
    pdf_bytes = backend.get_pdf_bytes(page, settings=settings)
    return pdf_bytes, page


def _single_pass_entity_pipeline(doc) -> None:
    """One iteration over modelspace + blocks + non-Model layouts that
    applies the three per-entity transforms together:

    * MTEXT inline ``\\f<Family>;`` rewrites
    * ACI 7 demotion for entities outside the preserve rules
    * (true_color stripping when demoted)

    The `_DotSmall` block rewrite stays separate because it MUTATES the
    block's child collection (delete + add). Doing it inside a child loop
    would invalidate the iterator.
    """
    swap_table = MTEXT_INLINE_FONT_REWRITES if MTEXT_INLINE_FONT_REWRITES else None
    do_mono = bool(PRESERVE_COLOR)

    if not (swap_table or do_mono):
        return

    def _swap(match: re.Match) -> str:
        family = match.group(1)
        flags = match.group(2)
        new = swap_table.get(family, family) if swap_table else family
        return f"\\f{new}{flags};"

    def _process(entity):
        if swap_table is not None and entity.dxftype() == "MTEXT":
            new_text = _MTEXT_FONT_CODE_RE.sub(_swap, entity.text)
            if new_text != entity.text:
                entity.text = new_text
        if do_mono and not _should_preserve(entity):
            if entity.dxf.hasattr("true_color"):
                entity.dxf.discard("true_color")
            entity.dxf.color = 7

    containers = [doc.modelspace(),
                  *(doc.layout(name) for name in doc.layout_names()
                    if name != "Model")]
    for container in containers:
        for e in container:
            _process(e)
    for blk in doc.blocks:
        for e in blk:
            _process(e)


def _prepare_doc(doc, font_dir: Path) -> None:
    _apply_ezdxf_patches()
    configure_ezdxf_fonts(font_dir)
    patch_styles(doc)
    replace_dot_blocks(doc)            # mutates block children — runs alone
    _single_pass_entity_pipeline(doc)  # MTEXT rewrite + monochrome in one walk


def render_doc_to_pdf(doc, output_path: Path,
                      *,
                      font_dir: Path,
                      logo_dir: Optional[Path] = None,
                      layout_name: str = "SR") -> Path:
    """Render ``doc`` to a PDF at ``output_path``. Returns the output path.

    :param doc: open ezdxf Drawing, modified in-place (style patches, color
        forcing, etc.).
    :param output_path: file path to write.
    :param font_dir: directory containing autocad SHX/TTF fonts; this becomes
        an ezdxf support_dir.
    :param logo_dir: optional directory containing PNG overlays named
        ``drawing1.png``, ``drawing2.png``, … to composite over OLE2FRAME
        placeholders (1 = topmost on the sheet).
    :param layout_name: paperspace layout name to take page-setup from
        (default "SR"). Modelspace is rendered when this layout contains
        only viewports.
    """
    _prepare_doc(doc, font_dir)
    pdf_bytes, page = _build_pdf_bytes(doc, layout_name)

    frames = collect_ole_frames(doc)
    if frames:
        overlays = _resolve_ole_overlays(logo_dir, frames)
        if overlays:
            pdf_bytes = composite_ole_overlays_inplace_bytes(
                pdf_bytes, frames, overlays,
                page_height_mm=float(page.height),
            )
    output_path.write_bytes(pdf_bytes)
    return output_path


def render_doc_to_pdf_bytes(doc,
                            *,
                            font_dir: Path,
                            logo_dir: Optional[Path] = None,
                            layout_name: str = "SR",
                            filter_func=None) -> bytes:
    """In-memory variant: returns the PDF bytes. Used by preview endpoints
    AND the new fast-path PDF download — fully avoids disk I/O.

    `filter_func` (optional) — passed through to ezdxf Frontend.draw_layout.
    Used by the skeleton-cache path to render the PDF without placeholder
    text entities (overlaid per-customer via PyMuPDF afterwards).
    """
    _prepare_doc(doc, font_dir)
    pdf_bytes, page = _build_pdf_bytes(doc, layout_name, filter_func=filter_func)

    frames = collect_ole_frames(doc)
    if frames:
        overlays = _resolve_ole_overlays(logo_dir, frames)
        if overlays:
            pdf_bytes = composite_ole_overlays_inplace_bytes(
                pdf_bytes, frames, overlays,
                page_height_mm=float(page.height),
            )
    return pdf_bytes


def get_page_height_mm(doc, layout_name: str = "SR") -> float:
    """Return the rendering page height in mm, matching what
    `render_doc_to_pdf_bytes` uses. Needed by the skeleton-cache overlay so
    it can flip Y coordinates correctly."""
    target, paperspace = _select_render_layout(doc, layout_name)
    if paperspace is not None:
        page = layout.Page.from_dxf_layout(paperspace)
    else:
        page = layout.Page(420, 297, layout.Units.mm,
                           margins=layout.Margins.all(0))
    return float(page.height)
