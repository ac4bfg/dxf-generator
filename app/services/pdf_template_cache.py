"""Cached PDF skeleton + customer-text overlay.

When a customer drawing uses the *default* segments seed (no edits), the
geometry portion of the rendered PDF is identical for every customer of the
same start variant. Only the title-block placeholder text (NAMA, REFF_ID,
ALAMAT, materials, etc.) varies per customer. We exploit that to skip the
expensive ezdxf full re-render on each request:

1. **Skeleton cache** — once per `(template, start_block, segments_hash)`,
   render a PDF *without* the placeholder TEXT/MTEXT entities (filtered out
   via ezdxf Frontend filter_func). Save to disk as ``<key>.pdf``.
2. **Placeholder metadata** — alongside the skeleton, save the original
   position/height/style of every placeholder entity to ``<key>.meta.json``.
3. **Per-customer overlay** — open the cached skeleton, stamp the customer's
   text values at the recorded placeholder positions via PyMuPDF
   ``insert_textbox``. Output is bytes.

Customer text rendering goes through PyMuPDF, not ezdxf, so the visual is
slightly different from a full ezdxf render (most notably: no faux-bold,
no oblique support). For plain title-block fields (single-line Arial
Narrow) the difference is acceptable. For customers that customise
segments → fall through to the full renderer.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PLACEHOLDER_RE = re.compile(r"\[[A-Z0-9_]+\]")

# Strips DXF MTEXT inline format codes (e.g. \pxsm1; \fArial; \A1; \P {})
# so PyMuPDF receives clean plain text for rendering.
_MTEXT_FMT_RE = re.compile(r'\\[A-Za-z~][^;]*;|\\P|\{|\}')
MM_TO_PT = 72.0 / 25.4

# Cache template content hashes so we don't re-read+md5 a file on every
# request. Keyed by (path, mtime) — NOT path alone — so a file that gets
# REPLACED at the same path (admin uploads a new kop DXF, e.g.
# SK_POLOS_p1.dxf, without a server restart) is detected via its changed
# mtime and re-hashed, instead of silently reusing the OLD file's hash
# forever for the life of the process. Old (path, mtime) entries are never
# evicted, but each is tiny (12-char string) and distinct file replacements
# are rare, so unbounded growth is not a practical concern.
_TEMPLATE_CONTENT_HASH: Dict[tuple, str] = {}


def _get_template_hash(template_path: Path) -> str:
    mtime = template_path.stat().st_mtime_ns
    key = (str(template_path), mtime)
    if key not in _TEMPLATE_CONTENT_HASH:
        _TEMPLATE_CONTENT_HASH[key] = hashlib.md5(
            template_path.read_bytes()
        ).hexdigest()[:12]
    return _TEMPLATE_CONTENT_HASH[key]

# DXF TEXT halign/valign codes
# halign: 0=Left, 1=Center, 2=Right, 3=Aligned, 4=Middle, 5=Fit
# valign: 0=Baseline, 1=Bottom, 2=Middle, 3=Top
_HA_TO_PYMUPDF = {0: 0, 1: 1, 2: 2, 4: 1, 3: 0, 5: 0}  # 0=left, 1=center, 2=right
# MTEXT attachment_point: 1=TopLeft, 2=TopCenter, 3=TopRight,
#                         4=MiddleLeft, 5=MiddleCenter, 6=MiddleRight,
#                         7=BottomLeft, 8=BottomCenter, 9=BottomRight
_MTEXT_AP_HALIGN = {1: 0, 2: 1, 3: 2, 4: 0, 5: 1, 6: 2, 7: 0, 8: 1, 9: 2}


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _normalize_nums(v: Any) -> Any:
    """Recursively normalize numeric types so that int 4 and float 4.0
    produce the same JSON representation.

    PHP sends ``4.0`` as a JSON float; JavaScript's JSON.stringify and
    MySQL JSON round-trip both produce integer ``4`` for the same value.
    Without normalization these hash differently despite identical semantics.
    """
    if isinstance(v, float) and v.is_integer() and not math.isinf(v) and not math.isnan(v):
        return int(v)
    if isinstance(v, dict):
        return {k: _normalize_nums(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_normalize_nums(item) for item in v]
    return v


def request_cache_key(template_path: Path, start_block: str,
                      segments: List[Dict], combined_dims: List[Dict]) -> str:
    """Stable cache key for a (template, render-signature) tuple. The key
    captures only fields that actually affect the rendered geometry, so two
    customers whose segments differ only in cosmetically-irrelevant fields
    (e.g. ``length_mm`` on a pipe whose visual length is fixed by a
    ``breakline.visual_length_mm`` and whose ``dimension`` is off) hash to
    the same key.

    Crossing segments are intentionally excluded from the key: the crossing
    block is never baked into the skeleton PDF (it is always applied as a
    per-customer PyMuPDF overlay), so two requests that differ only in the
    presence of a crossing segment produce the same skeleton and should share
    the same cache entry.
    """
    payload = {
        "template": template_path.name,
        "template_hash": _get_template_hash(template_path),
        "start_block": start_block,
        "segments": [
            _segment_signature(s) for s in (segments or [])
            if s.get("type") != "crossing"
        ],
        "combined_dims": [_combined_dim_signature(c) for c in (combined_dims or [])],
    }
    digest = hashlib.sha256(
        json.dumps(_normalize_nums(payload), sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return f"{template_path.stem}_{start_block}_{digest}"


def _segment_signature(seg: Dict) -> Dict:
    """Return only fields that influence the rendered output for `seg`.

    Notably for PIPE: when a segment has both ``breakline.visual_length_mm``
    set AND ``dimension`` is False, the actual ``length_mm`` does NOT
    affect the PDF (visual is clamped, no dim text drawn). We omit it from
    the signature so customer-specific length_mm noise doesn't bust the
    cache.
    """
    s = seg or {}
    t = s.get("type", "pipe")
    if t == "pipe":
        sig = {
            "type": "pipe",
            "direction": s.get("direction"),
            "direction_by_variant": s.get("direction_by_variant"),
            "angle": s.get("angle"),
            "angle_by_variant": s.get("angle_by_variant"),
            "no_transform": s.get("no_transform"),
            "dimension": bool(s.get("dimension")),
            "bend_side": s.get("bend_side"),
            "bend_side_by_variant": s.get("bend_side_by_variant"),
        }
        if s.get("dimension"):
            sig["dimension_side"] = s.get("dimension_side")
        bl = s.get("breakline") or None
        if bl:
            sig["bl_style"] = (bl.get("style") or "zigzag")
            sig["bl_visual"] = bl.get("visual_length_mm")
            if s.get("dimension"):
                # Real length only contributes to the dim text when shown
                sig["bl_real"] = bl.get("real_length_mm") or s.get("length_mm")
        else:
            # Without a breakline, length_mm directly drives the rendered
            # length (and the dim text when dim is on).
            sig["length_mm"] = s.get("length_mm")
        # Overlays affect rendered geometry (an extra block at a position
        # along the pipe), so they must be part of the cache key.
        ovs = s.get("overlays") or []
        if ovs:
            sig["overlays"] = [
                {
                    "block": o.get("block"),
                    "block_by_variant": o.get("block_by_variant"),
                    "position": o.get("position", 0.5),
                    "scale": o.get("scale"),
                    "rotation_offset": o.get("rotation_offset", 0.0),
                    "rotation_offset_by_variant": o.get("rotation_offset_by_variant"),
                }
                for o in ovs
            ]
        return sig
    if t == "component":
        return {
            "type": "component",
            "block": s.get("block"),
            "block_by_variant": s.get("block_by_variant"),
            "gap": s.get("gap"),
            "scale": s.get("scale"),
            "scale_by_variant": s.get("scale_by_variant"),
            "rotation": s.get("rotation"),
            "rotation_by_variant": s.get("rotation_by_variant"),
            "auto_mirror": s.get("auto_mirror"),
            "color": s.get("color"),
            "direction": s.get("direction"),
            "direction_by_variant": s.get("direction_by_variant"),
            "canonical_direction_angle": s.get("canonical_direction_angle"),
            "insert_offset_by_variant": s.get("insert_offset_by_variant"),
            "bend_side": s.get("bend_side"),
            "bend_side_by_variant": s.get("bend_side_by_variant"),
            "dimension": bool(s.get("dimension")),
            "dimension_side": s.get("dimension_side") if s.get("dimension") else None,
        }
    # Unknown type → keep as-is.
    return {k: s[k] for k in sorted(s.keys())}


def _combined_dim_signature(cd: Dict) -> Dict:
    side = cd.get("side")
    # Normalize: None and "default" are semantically identical.
    # The Drawing Editor saves "default" explicitly; sync payloads omit it.
    # Without normalization they hash differently despite identical rendering.
    if side == "default":
        side = None
    return {
        "from_seg": cd.get("from_seg"),
        "to_seg": cd.get("to_seg"),
        # text_mm is computed from segment lengths if not explicit; both
        # paths matter for the rendered dim string.
        "text_mm": cd.get("text_mm"),
        "side": side,
    }


# ---------------------------------------------------------------------------
# Placeholder extraction
# ---------------------------------------------------------------------------

#  [REFF_ID] appears TWICE per SK/SR template — once in the title block
#  (Y ~227-254mm, near the top of an A3 sheet) and once as the drawing
#  number bottom-right (Y ~5-9mm). Same literal key, so a single row in the
#  admin "Posisi PDF" panel (offset/font_scale keyed by substring match on
#  ph["text"]) would otherwise nudge BOTH at once. _REFF_ID_BAWAH_Y_MAX is
#  a cutoff well below the title block's Y and well above the bottom
#  drawing-number's Y for every template surveyed (SK default/Kendal, SR
#  default/2/3/backup) — see extract_placeholder_entities().
_REFF_ID_BAWAH_Y_MAX = 50.0


def extract_placeholder_entities(doc) -> List[Dict[str, Any]]:
    """Walk the modelspace and return a list of placeholder TEXT/MTEXT
    descriptions. We deliberately skip the synthetic ``*Model_Space`` block
    (which is just an internal copy of modelspace)."""
    items: List[Dict[str, Any]] = []
    for e in doc.modelspace():
        if e.dxftype() == "TEXT" and PLACEHOLDER_RE.search(e.dxf.text):
            d = e.dxf
            ap = d.get("align_point")
            y = float(d.insert.y)
            items.append({
                "kind": "TEXT",
                "text": d.text,
                # offset_key — used ONLY to look up custom_offsets/
                # custom_font_scale (never for value substitution, which
                # always uses "text"). None means "use text itself", same
                # as before this field existed.
                "offset_key": "[REFF_ID_BAWAH]" if ("[REFF_ID]" in d.text and y < _REFF_ID_BAWAH_Y_MAX) else None,
                "x": float(d.insert.x), "y": y,
                "ax": float(ap.x) if ap else float(d.insert.x),
                "ay": float(ap.y) if ap else float(d.insert.y),
                "height": float(d.height),
                "rotation": float(d.rotation or 0),
                "halign": int(d.halign or 0),
                "valign": int(d.valign or 0),
                "style": d.style,
            })
        elif e.dxftype() == "MTEXT" and PLACEHOLDER_RE.search(e.text):
            d = e.dxf
            # Strip DXF MTEXT format codes so the stored template text
            # contains only plain text + placeholder tokens. PyMuPDF does
            # not understand codes like \pxsm1; and would render them as
            # literal characters if left in.
            plain_text = _MTEXT_FMT_RE.sub('', e.text).strip()
            if not PLACEHOLDER_RE.search(plain_text):
                plain_text = e.text  # fallback: keep original if strip removed placeholder
            y = float(d.insert.y)
            items.append({
                "kind": "MTEXT",
                "text": plain_text,
                "offset_key": "[REFF_ID_BAWAH]" if ("[REFF_ID]" in plain_text and y < _REFF_ID_BAWAH_Y_MAX) else None,
                "x": float(d.insert.x), "y": y,
                "height": float(d.char_height),
                "width": float(d.width or 0),
                "rotation": float(d.rotation or 0),
                "attachment_point": int(d.attachment_point or 1),
                "style": d.style,
            })
    return items


# ---------------------------------------------------------------------------
# Skeleton render
# ---------------------------------------------------------------------------

def render_skeleton_bytes(doc, *,
                          renderer: Callable[..., bytes],
                          font_dir: Path,
                          logo_dir: Optional[Path],
                          layout_name: str = "SR") -> bytes:
    """Render the doc to PDF bytes, but skip every TEXT/MTEXT whose content
    contains a ``[PLACEHOLDER]`` token. The supplied ``renderer`` callable
    must accept ``(doc, font_dir, logo_dir, layout_name, filter_func)`` and
    return raw PDF bytes."""
    return renderer(doc=doc,
                    font_dir=font_dir,
                    logo_dir=logo_dir,
                    layout_name=layout_name,
                    filter_func=_skip_placeholders)


def _skip_placeholders(entity) -> bool:
    """ezdxf Frontend filter_func: True keeps the entity, False removes it."""
    t = entity.dxftype()
    if t == "TEXT":
        return not PLACEHOLDER_RE.search(entity.dxf.text or "")
    if t == "MTEXT":
        return not PLACEHOLDER_RE.search(entity.text or "")
    return True


def _skip_placeholders_and_crossing(entity) -> bool:
    """Like _skip_placeholders but also removes crossing block references.

    Crossing is never baked into the skeleton PDF — it is always applied as a
    per-customer PyMuPDF overlay so that casing and non-casing customers with
    the same pipe configuration can share a single skeleton cache entry.
    """
    if entity.dxftype() == "INSERT":
        name = (entity.dxf.name or "").lower()
        if name.startswith("crossing-"):
            return False
    return _skip_placeholders(entity)


# ---------------------------------------------------------------------------
# Customer overlay (PyMuPDF)
# ---------------------------------------------------------------------------

def _resolve_text(template_text: str, replacements: Dict[str, str]) -> str:
    """Replace every `[KEY]` token in template_text with replacements[`[KEY]`].
    Strips MTEXT inline formatting prefixes like `\\pxqr;` / `\\pxsm1;`."""
    out = template_text
    # strip leading paragraph-format codes (e.g. \pxqr; or \pxsm1;)
    out = re.sub(r"^\\px[a-z0-9,.\-]*;", "", out)
    # also strip any leftover formatting codes inside (rare for plain placeholders)
    out = re.sub(r"\\[A-Za-z]\\?[^;]*;", "", out)
    for key, value in replacements.items():
        out = out.replace(key, str(value))
    return out


def _font_path_or_default(font_dir: Optional[Path], fontfile: str) -> Optional[str]:
    if not font_dir:
        return None
    candidate = font_dir / fontfile
    return str(candidate) if candidate.is_file() else None


# In-process cache of pymupdf.Font objects, keyed by font file path — avoids
# re-reading/re-parsing the TTF on every _stamp_text()/_word_wrap_lines()
# call within a single render (there can be dozens of placeholders per page).
_FONT_OBJ_CACHE: Dict[str, Any] = {}


def _get_font_obj(font_path: Optional[str]):
    """Return a pymupdf.Font for `font_path`, or None if unavailable.

    IMPORTANT: page.get_text_length() does NOT exist in PyMuPDF 1.28 (the
    version pinned here) — calling it always raises AttributeError, which
    every caller silently caught and replaced with a crude `len(text) *
    fontsize * 0.5` estimate. That estimate is self-consistent for the
    anchor-position MATH (anchor_x = x_pt - text_width always makes
    anchor_x + text_width == x_pt), but PyMuPDF's actual glyph rendering in
    insert_text() uses the REAL proportional glyph widths — which differ
    from the flat per-character estimate by an amount that depends on the
    exact characters in the string. For right/center-aligned text this
    showed up as visible drift (e.g. right-aligned "EA" after a material
    quantity landing a few points off depending on how many digits the
    quantity had) even though the anchor math itself was correct — the
    width FED INTO that math was wrong. pymupdf.Font(fontfile=...).
    text_length() is the correct API for a custom/embedded font in this
    PyMuPDF version and returns the true glyph-based width.
    """
    if not font_path:
        return None
    if font_path not in _FONT_OBJ_CACHE:
        try:
            import pymupdf as _pm
            _FONT_OBJ_CACHE[font_path] = _pm.Font(fontfile=font_path)
        except Exception:
            _FONT_OBJ_CACHE[font_path] = None
    return _FONT_OBJ_CACHE[font_path]


def _text_width(text: str, fontsize: float, fontname: str,
                font_obj=None) -> float:
    """Accurate text width via pymupdf.Font.text_length() when `font_obj`
    is available; falls back to the flat per-character estimate (used to be
    the ONLY path — see _get_font_obj() docstring) only when the font
    couldn't be loaded at all."""
    if font_obj is not None:
        try:
            return font_obj.text_length(text, fontsize=fontsize)
        except Exception:
            pass
    return len(text) * fontsize * 0.5


def _placeholder_offset(template_text: str, custom: Optional[Dict[str, Tuple[float, float]]] = None) -> Tuple[float, float]:
    """Return the (x, y) nudge (in fractions of font size) for a placeholder
    by scanning its template text for any key in :data:`PLACEHOLDER_OFFSETS`.

    ``custom`` — per-region overrides (AsbuiltPdfPlaceholderOffset, resolved
    Laravel-side and passed through customer_data['pdf_offsets']) MERGED on
    top of the hardcoded defaults, so a region overriding ONE key doesn't
    lose the calibrated defaults for every other key. Falls back to (0, 0)
    when no key matches either source.
    """
    merged = {**PLACEHOLDER_OFFSETS, **(custom or {})}
    for key, offset in merged.items():
        if key in template_text:
            return offset
    return (0.0, 0.0)


def _placeholder_x_offset(template_text: str) -> float:
    """Backwards-compat shim — returns just the X component."""
    return _placeholder_offset(template_text)[0]


def _font_scale(template_text: str, custom: Optional[Dict[str, float]] = None) -> float:
    """Return the font_size_pt multiplier for a placeholder by scanning its
    template text for any key in ``custom`` (AsbuiltPdfPlaceholderOffset.
    font_scale, resolved Laravel-side). Unlike _placeholder_offset() there is
    NO hardcoded default layer — a key absent from ``custom`` means 1.0
    (unchanged, original DXF cap-height).
    """
    if not custom:
        return 1.0
    for key, scale in custom.items():
        if key in template_text:
            return scale
    return 1.0


# DXF font filename → PyMuPDF font alias + filename. Order in this list
# determines the search priority: the first file that exists in font_dir
# becomes the page's main customer-text font.
_OVERLAY_FONT_CANDIDATES = [
    ("arial",  "arial.ttf"),    # Arial Regular — matches DXF style 'Standard'
                                 # (font='arial.ttf') and 'ARIAL' (font='Arial'
                                 # → arial.ttf via FONT_SUBSTITUTES).
    ("arialn", "ARIALN.TTF"),    # fallback: Arial Narrow if Regular missing
]

# Conversion factor from DXF cap-height (mm) to PDF em-square (pt). PDF
# font_size sets the em-square size, AutoCAD's TEXT height is the cap
# height. For Arial the cap-to-em ratio is ~0.716, so:
#   font_size_pt = cap_mm × (72/25.4) / 0.716 ≈ cap_mm × 3.95
_DXF_HEIGHT_TO_PT = (72.0 / 25.4) / 0.716


# =============================================================
# Per-placeholder fine-tune offsets — EDIT HERE to nudge any field
# =============================================================
# Maps a placeholder key (e.g. "[NAMA]") to an (x, y) offset
# expressed as a multiple of the font size at that placeholder.
#
#   x: negative = shift LEFT,  positive = shift RIGHT
#   y: negative = shift UP,    positive = shift DOWN
#                              (PDF y is down-positive)
#
# These offsets are added on top of the base alignment computed from
# the DXF entity's halign/valign or MTEXT attachment_point. Use them
# whenever a specific cell looks slightly off after rendering.
#
# Tip: 1.0 == one font-size of shift. For a 1.61 mm cap height
# (~6.3 pt), 0.25 ≈ 1.6 pt ≈ 0.55 mm.
PLACEHOLDER_OFFSETS: Dict[str, Tuple[float, float]] = {
    # ---- Title block (TEXT entities, valign=2 Middle) ----
    "[REFF_ID]":            (0.0, -0.10),
    # Bottom-right drawing number — same [REFF_ID] literal but a distinct
    # entity (see _REFF_ID_BAWAH_Y_MAX/offset_key), default preserved
    # identical to [REFF_ID] so existing renders don't shift; admin can
    # override just this one from the "Posisi PDF" panel.
    "[REFF_ID_BAWAH]":      (0.0, -0.10),
    "[NAMA]":               (-0.075, -0.10),
    "[SEKTOR]":             (0.0, -0.10),
    "[RT]":                 (0.0, -0.10),
    "[RW]":                 (0.0, -0.10),
    "[KELURAHAN]":          (0.0, -0.10),
    "[PADUKUHAN]":          (0.0, -0.10),
    "[NO_MGRT]":            (0.0, -0.10),
    "[SN_AWAL]":            (0.0, -0.10),
    "[KOORDINAT_TAPPING]":  (0.0, -0.10),
    "[TANGGAL]":            (0.0, 0.10),

    # ---- Title block (MTEXT, attachment=4 Middle Left) ----
    "[ALAMAT]":             (-0.075, 0.0),

    # ---- Material count cells SR (MTEXT, attachment=3 Top Right) ----
    "[7]":  (-0.150, 0.0),  # sealtape (SR)
    "[19]": (-0.150, 0.0),  # coupler (SR)
    "[10]": (-0.150, 0.0),  # elbow (SR)
    "[21]": (-0.350, 0.0),  # casing (SR)
    "[8]":  (-0.150, 0.0),  # pipa (SR)

    # ---- Material count cells SK (MTEXT, attachment=3 Top Right) ----
    "[2]":  (-0.150, 0.0),  # elbow SK
    "[3]":  (-0.150, 0.0),  # sockdraft SK
    "[6]":  (-0.150, 0.0),  # klem SK
    "[113]": (-0.150, 0.0),  # long elbow 3/4" male female SK
    "[114]": (-0.150, 0.0),  # ball valve 1/2" SK
    "[115]": (-0.150, 0.0),  # nipel selang 1/2" SK
    "[4]":   (-0.150, 0.0),  # elbow reduce 3/4"x1/2" SK
}


# Backwards-compat alias — keep callers that reference the old name working.
PLACEHOLDER_X_OFFSET = {k: v[0] for k, v in PLACEHOLDER_OFFSETS.items()}


def compose_customer_pdf(skeleton_bytes: bytes,
                         placeholders: List[Dict[str, Any]],
                         replacements: Dict[str, str],
                         page_height_mm: float,
                         font_dir: Optional[Path] = None,
                         crossing_overlay_bytes: Optional[bytes] = None,
                         logo_overlays: Optional[List[Dict[str, Any]]] = None,
                         custom_offsets: Optional[Dict[str, Tuple[float, float]]] = None,
                         custom_font_scale: Optional[Dict[str, float]] = None) -> bytes:
    """Open the skeleton PDF, stamp each placeholder's resolved text at the
    recorded position, return new PDF bytes. Pure in-memory, no disk I/O.

    custom_offsets — per-region PLACEHOLDER_OFFSETS override (AsbuiltPdfPlaceholderOffset,
    resolved Laravel-side, merged on top of the hardcoded defaults per-key —
    see _placeholder_offset()). None/empty = identical to current behavior.

    custom_font_scale — per-region font size multiplier keyed the same way
    (AsbuiltPdfPlaceholderOffset.font_scale, resolved Laravel-side). Unlike
    custom_offsets there is NO hardcoded default layer — a key absent here
    means 1.0 (unchanged, original DXF cap-height). None/empty = identical
    to current behavior.

    crossing_overlay_bytes — pre-rendered PDF of just the crossing block for
    this start_block variant. When supplied (customer has casing > 0), it is
    blended onto the skeleton *before* text so it sits in the geometry layer.
    Only 4 such overlays exist (one per start_block); each is ~20–50 KB and
    cached in IsometricService._CROSSING_OVERLAY_CACHE.

    logo_overlays — per-region logo PNGs from As Built settings
    (asbuilt_dxf_templates.logo_overlays), list of {png_base64, x1, y1, x2,
    y2} (mm, DXF Y-up). Stamped on the SKELETON (already-cached geometry),
    NEVER baked into the skeleton itself — this must stay outside the cache
    key/skeleton build (render_pdf_bytes_cached's logo_dir=self._pdf_logo_dir()
    stays global/region-agnostic there) or region A's logo would leak into
    region B's cached skeleton for the same geometry.
    """
    if not placeholders and crossing_overlay_bytes is None and not logo_overlays:
        return skeleton_bytes

    import pymupdf as _pm

    pdf = _pm.open(stream=skeleton_bytes, filetype="pdf")
    try:
        page = pdf[0]

        # ── Crossing overlay (geometry, rendered under customer text) ──
        # Uses get_drawings() instead of show_pdf_page so the white background
        # added by BackgroundPolicy.WHITE in the renderer is skipped — only
        # actual crossing geometry paths are replayed onto the skeleton page.
        if crossing_overlay_bytes:
            try:
                cross_doc = _pm.open(stream=crossing_overlay_bytes, filetype="pdf")
                paths = cross_doc[0].get_drawings()
                cross_doc.close()
                if paths:
                    shape = page.new_shape()
                    for path in paths:
                        fill  = path.get("fill")
                        color = path.get("color")
                        # Skip pure-white background fills (no stroke color = background rect)
                        if fill == (1.0, 1.0, 1.0) and color is None:
                            continue
                        drawn = False
                        for item in path.get("items", []):
                            k = item[0]
                            if k == "l":                     # line
                                shape.draw_line(item[1], item[2])
                                drawn = True
                            elif k == "re":                  # rect
                                shape.draw_rect(item[1])
                                drawn = True
                            elif k == "c":                   # cubic bezier
                                shape.draw_bezier(item[1], item[2], item[3], item[4])
                                drawn = True
                            elif k == "qu":                  # quad
                                shape.draw_quad(item[1])
                                drawn = True
                        if drawn:
                            draw_fill = fill if fill != (1.0, 1.0, 1.0) else None
                            shape.finish(
                                color=color,
                                fill=draw_fill,
                                width=path.get("width") or 0.5,
                                closePath=path.get("closePath", False),
                            )
                    shape.commit()
            except Exception as _e:
                print(f"[WARNING] Crossing overlay failed: {_e}")
        h_pt = page_height_mm * MM_TO_PT

        # ── Logo overlays (per-region, from As Built settings) ──
        # Same rect math as composite_ole_overlays_inplace_bytes (pdf_renderer.py):
        # mm (DXF Y-up) -> pt (PDF Y-down from top), y flipped via h_pt - y*MM_TO_PT.
        # insert_image() accepts raw bytes via stream=, no temp file needed.
        for lo in (logo_overlays or []):
            b64 = lo.get("png_base64")
            if not b64:
                continue
            try:
                import base64 as _b64
                png_bytes = _b64.b64decode(b64)
            except Exception:
                continue
            try:
                x1, x2 = float(lo["x1"]), float(lo["x2"])
                y1, y2 = float(lo["y1"]), float(lo["y2"])
            except (KeyError, TypeError, ValueError):
                continue
            if x2 <= x1 or y2 <= y1:
                continue
            x0_pt = x1 * MM_TO_PT
            x1_pt = x2 * MM_TO_PT
            y0_pt = h_pt - y2 * MM_TO_PT
            y1_pt = h_pt - y1 * MM_TO_PT
            try:
                page.insert_image(_pm.Rect(x0_pt, y0_pt, x1_pt, y1_pt), stream=png_bytes, keep_proportion=False)
            except Exception as _e:
                print(f"[WARNING] Logo overlay failed: {_e}")

        # Embed the first available customer-text font. Priority: arial.ttf
        # (matches DXF style resolution), falls back to ARIALN.TTF.
        used_alias: Optional[str] = None
        used_font_obj = None
        for alias, fname in _OVERLAY_FONT_CANDIDATES:
            font_path = _font_path_or_default(font_dir, fname)
            if not font_path:
                continue
            try:
                page.insert_font(fontname=alias, fontfile=font_path)
                used_alias = alias
                used_font_obj = _get_font_obj(font_path)
                break
            except Exception:
                continue

        for ph in placeholders:
            value = _resolve_text(ph["text"], replacements).strip()
            if not value:
                continue

            # Convert WCS (mm, Y-up) -> PDF points (Y-down from top).
            x_pt = ph["x"] * MM_TO_PT
            y_pt = h_pt - ph["y"] * MM_TO_PT

            # Map DXF cap-height (mm) to PDF font size (em-square pt) so the
            # rendered cap-height roughly matches AutoCAD's plot. Custom
            # font_scale (admin, "Posisi PDF" panel) applies BEFORE the
            # offset fractions are resolved to pt, so a nudge (e.g. -0.15em)
            # stays proportionally correct at any scale — it was calibrated
            # as a fraction of the rendered size, not the original one.
            # offset_key disambiguates entities that share the same literal
            # [KEY] (e.g. two [REFF_ID] instances — title block vs bottom-
            # right drawing number) so each can get its own custom
            # offset/scale from the admin panel. Falls back to "text" for
            # every other placeholder (unchanged behavior).
            lookup_text = ph.get("offset_key") or ph["text"]
            font_size_pt = max(ph["height"] * _DXF_HEIGHT_TO_PT, 4.0)
            font_scale = _font_scale(lookup_text, custom_font_scale)
            font_size_pt *= font_scale

            is_custom = custom_offsets and any(k in lookup_text for k in custom_offsets)
            x_offset_frac, y_offset_frac = _placeholder_offset(lookup_text, custom_offsets)
            x_offset_pt = x_offset_frac * font_size_pt
            y_offset_pt = y_offset_frac * font_size_pt

            if ph["kind"] == "TEXT":
                halign = _HA_TO_PYMUPDF.get(ph["halign"], 0)
                # Use align point when halign != 0 (CENTER/RIGHT),
                # because in DXF it lives at the alignment box origin.
                if ph["halign"] in (1, 2, 4):
                    x_pt = ph["ax"] * MM_TO_PT
                    y_pt = h_pt - ph["ay"] * MM_TO_PT
                # PLACEHOLDER_OFFSETS' DEFAULT y-fraction was calibrated BY
                # TRIAL against valign=2 (Middle) title-block text, where
                # insert.y is the box's vertical CENTER, not the text
                # baseline — the nudge compensates for that gap. A
                # re-exploded/re-saved template (seen in practice:
                # SK_POLOS_p1.dxf after AutoCAD copy/paste) can carry the
                # SAME placeholder at valign=0 (Baseline), where insert.y IS
                # ALREADY the baseline — applying the valign=2 DEFAULT there
                # shifts text into the label above it, so it's skipped for
                # valign!=2. A CUSTOM offset (admin explicitly set it via the
                # "Posisi PDF" panel, is_custom=True) is a DELIBERATE nudge
                # for THIS exact entity — it must always apply regardless of
                # valign, otherwise admin adjustments on valign=0 templates
                # (e.g. Kendal) silently do nothing (bug reported: Y offset
                # had zero effect while X worked, because X had no such gate).
                apply_y = is_custom or ph["valign"] == 2
                baseline_y = y_pt + (y_offset_pt if apply_y else 0.0)
                _stamp_text(page, x_pt + x_offset_pt, baseline_y, value,
                            font_size_pt, ph["rotation"], halign, used_alias,
                            used_font_obj)
            else:  # MTEXT
                ap = ph["attachment_point"]
                halign = _MTEXT_AP_HALIGN.get(ap, 0)
                mtext_width = ph.get("width", 0)

                if mtext_width > 0:
                    # MTEXT with non-zero DXF width → word-wrap via insert_textbox.
                    # x_offset/y_offset already baked into x_pt/y_pt via caller.
                    _stamp_mtext_wrapped(
                        page,
                        x_pt + x_offset_pt,
                        y_pt + y_offset_pt,
                        value, font_size_pt,
                        mtext_width, ap, halign, used_alias,
                        used_font_obj,
                    )
                else:
                    # MTEXT width=0 (single-line, no wrapping) — keep existing path.
                    # MTEXT vertical anchor depends on attachment_point:
                    #   1,2,3 = Top    -> baseline ≈ y_pt + 0.5*size
                    #   4,5,6 = Middle -> baseline ≈ y_pt + 0.35*size
                    #   7,8,9 = Bottom -> baseline = y_pt
                    if ap in (1, 2, 3):
                        baseline_y = y_pt + font_size_pt * 0.5
                    elif ap in (4, 5, 6):
                        baseline_y = y_pt + font_size_pt * 0.35
                    else:
                        baseline_y = y_pt
                    _stamp_text(page, x_pt + x_offset_pt, baseline_y + y_offset_pt,
                                value, font_size_pt,
                                ph["rotation"], halign, used_alias,
                                used_font_obj)

        return pdf.tobytes(garbage=3, deflate=True)
    finally:
        pdf.close()


def _stamp_text(page, x_pt: float, y_pt: float, text: str, size: float,
                rotation: float, halign: int, font_alias: Optional[str],
                font_obj=None) -> None:
    """Stamp a single line at the given PDF-point position.

    Uses `insert_text` (not insert_textbox) so long strings — like
    "JRG3-KNK-0000-PL-DG-026-<REFF_ID>" — never get clipped by a box that
    happens to be too narrow. We compute string width manually so we can
    still honor halign (CENTER / RIGHT shift the anchor by half / full
    width).

    font_obj — pymupdf.Font for `font_alias` (see _get_font_obj()), used for
    an ACCURATE text_length(). Left/top-anchored text (halign=0) never
    needed this — only center/right alignment does, since the anchor must
    shift by the text's true rendered width for the visible edge to land
    exactly on x_pt (see _get_font_obj() docstring for why the old
    page.get_text_length() fallback silently drifted here).
    """
    fontname = font_alias or "helv"
    text_width = _text_width(text, size, fontname, font_obj)

    if halign == 1:    # center
        anchor_x = x_pt - text_width / 2
    elif halign == 2:  # right
        anchor_x = x_pt - text_width
    else:              # left (and treat halign=4/middle same as left for now)
        anchor_x = x_pt

    # `y_pt` already IS the desired PDF baseline — caller chose it based
    # on the placeholder kind / attachment_point.
    baseline_y = y_pt

    kwargs = {"fontsize": size, "color": (0, 0, 0), "fontname": fontname}
    if rotation:
        rot_int = int(round(rotation)) % 360
        if rot_int in (0, 90, 180, 270):
            kwargs["rotate"] = rot_int

    page.insert_text((anchor_x, baseline_y), text, **kwargs)


def _word_wrap_lines(page, text: str, fontsize: float,
                     width_pt: float, fontname: str,
                     font_obj=None) -> List[str]:
    """Word-wrap text into a list of lines fitting within width_pt.

    font_obj — see _get_font_obj()/_text_width(); without it this falls
    back to a flat character-width estimate, which can wrap a line too
    early or too late since it doesn't know the real (proportional) glyph
    widths.
    """
    if font_obj is not None:
        words = text.split()
        if not words:
            return [""]
        space_w = _text_width(" ", fontsize, fontname, font_obj)
        lines: List[str] = []
        current: List[str] = []
        current_w = 0.0
        for word in words:
            word_w = _text_width(word, fontsize, fontname, font_obj)
            if not current:
                current.append(word)
                current_w = word_w
            elif current_w + space_w + word_w <= width_pt:
                current.append(word)
                current_w += space_w + word_w
            else:
                lines.append(" ".join(current))
                current = [word]
                current_w = word_w
        if current:
            lines.append(" ".join(current))
        return lines or [""]
    else:
        # No font_obj — estimate with fixed char width but still split on
        # word boundaries, not arbitrary character positions.
        avg_char_w = fontsize * 0.65
        max_chars  = max(1, int(width_pt / avg_char_w))
        words      = text.split()
        if not words:
            return [""]
        lines: List[str] = []
        current: List[str] = []
        current_len = 0
        for word in words:
            wlen = len(word)
            if not current:
                current.append(word)
                current_len = wlen
            elif current_len + 1 + wlen <= max_chars:
                current.append(word)
                current_len += 1 + wlen
            else:
                lines.append(" ".join(current))
                current = [word]
                current_len = wlen
        if current:
            lines.append(" ".join(current))
        return lines or [""]


def _stamp_mtext_wrapped(page, x_pt: float, y_pt: float, text: str,
                         size: float, width_mm: float,
                         attachment_point: int, halign: int,
                         font_alias: Optional[str], font_obj=None) -> None:
    """Stamp a word-wrapping MTEXT field line by line via _stamp_text.

    Renders each wrapped line with insert_text (same as single-line MTEXT)
    so vertical positioning is identical to the single-line path — no
    insert_textbox calibration drift.

    Line spacing: 1.5 × font size.
    Vertical anchor per attachment_point:
      Middle (AP 4-6): centre of all baselines at y_pt + 0.35 × size,
                       matching single-line MTEXT middle baseline.
      Top    (AP 1-3): first baseline at y_pt + 0.50 × size.
      Bottom (AP 7-9): last  baseline at y_pt.
    """
    fontname    = font_alias or "helv"
    width_pt    = width_mm * MM_TO_PT
    LINE_FACTOR = 1.25
    line_h_pt   = size * LINE_FACTOR

    lines = _word_wrap_lines(page, text, size, width_pt, fontname, font_obj)
    n = len(lines)

    # First-baseline y — mirrors the single-line MTEXT middle baseline (0.35).
    # For n lines centred at (y_pt + 0.35*size):
    #   first_y = (y_pt + 0.35*size) - (n-1)/2 * line_h_pt
    if attachment_point in (1, 2, 3):        # Top
        first_y = y_pt + size * 0.50
    elif attachment_point in (4, 5, 6):      # Middle
        first_y = (y_pt + size * 0.35) - (n - 1) * line_h_pt / 2
    else:                                     # Bottom
        first_y = y_pt - (n - 1) * line_h_pt

    for i, line in enumerate(lines):
        _stamp_text(page, x_pt, first_y + i * line_h_pt,
                    line, size, 0, halign, font_alias, font_obj)


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _cache_paths(cache_dir: Path, key: str) -> Tuple[Path, Path]:
    return cache_dir / f"{key}.pdf", cache_dir / f"{key}.meta.json"


def load_cache(cache_dir: Path, key: str) -> Optional[Tuple[bytes, List[Dict]]]:
    pdf_path, meta_path = _cache_paths(cache_dir, key)
    if not (pdf_path.is_file() and meta_path.is_file()):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return pdf_path.read_bytes(), meta
    except Exception:
        return None


def save_cache(cache_dir: Path, key: str, pdf_bytes: bytes,
               placeholders: List[Dict]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path, meta_path = _cache_paths(cache_dir, key)
    pdf_path.write_bytes(pdf_bytes)
    meta_path.write_text(json.dumps(placeholders, indent=2), encoding="utf-8")
