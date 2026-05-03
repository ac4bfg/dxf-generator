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

# Cache template content hashes so we read each file once per process.
# Using content hash (not mtime) means the cache key is stable even when
# the OS updates mtime without changing the file's actual content.
_TEMPLATE_CONTENT_HASH: Dict[str, str] = {}


def _get_template_hash(template_path: Path) -> str:
    key = str(template_path)
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

def extract_placeholder_entities(doc) -> List[Dict[str, Any]]:
    """Walk the modelspace and return a list of placeholder TEXT/MTEXT
    descriptions. We deliberately skip the synthetic ``*Model_Space`` block
    (which is just an internal copy of modelspace)."""
    items: List[Dict[str, Any]] = []
    for e in doc.modelspace():
        if e.dxftype() == "TEXT" and PLACEHOLDER_RE.search(e.dxf.text):
            d = e.dxf
            ap = d.get("align_point")
            items.append({
                "kind": "TEXT",
                "text": d.text,
                "x": float(d.insert.x), "y": float(d.insert.y),
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
            items.append({
                "kind": "MTEXT",
                "text": plain_text,
                "x": float(d.insert.x), "y": float(d.insert.y),
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


def _placeholder_offset(template_text: str) -> Tuple[float, float]:
    """Return the (x, y) nudge (in fractions of font size) for a placeholder
    by scanning its template text for any key in
    :data:`PLACEHOLDER_OFFSETS`. Falls back to (0, 0) when no key matches.
    """
    for key, offset in PLACEHOLDER_OFFSETS.items():
        if key in template_text:
            return offset
    return (0.0, 0.0)


def _placeholder_x_offset(template_text: str) -> float:
    """Backwards-compat shim — returns just the X component."""
    return _placeholder_offset(template_text)[0]


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
    "[NAMA]":               (0.0, -0.10),
    "[SEKTOR]":             (0.0, -0.10),
    "[RT]":                 (0.0, -0.10),
    "[RW]":                 (0.0, -0.10),
    "[KELURAHAN]":          (0.0, -0.10),
    "[NO_MGRT]":            (0.0, -0.10),
    "[SN_AWAL]":            (0.0, -0.10),
    "[KOORDINAT_TAPPING]":  (0.0, -0.10),
    "[TANGGAL]":            (0.0, 0.10),

    # ---- Title block (MTEXT, attachment=4 Middle Left) ----
    "[ALAMAT]":             (0.0, 0.0),

    # ---- Material count cells (MTEXT, attachment=3 Top Right) ----
    "[7]":  (-0.125, 0.0),  # sealtape
    "[19]": (-0.125, 0.0),  # coupler
    "[10]": (-0.125, 0.0),  # elbow
    "[21]": (-0.350, 0.0),  # casing
    # "[8]":  (0.0, 0.0),   # pipa (M)
}


# Backwards-compat alias — keep callers that reference the old name working.
PLACEHOLDER_X_OFFSET = {k: v[0] for k, v in PLACEHOLDER_OFFSETS.items()}


def compose_customer_pdf(skeleton_bytes: bytes,
                         placeholders: List[Dict[str, Any]],
                         replacements: Dict[str, str],
                         page_height_mm: float,
                         font_dir: Optional[Path] = None,
                         crossing_overlay_bytes: Optional[bytes] = None) -> bytes:
    """Open the skeleton PDF, stamp each placeholder's resolved text at the
    recorded position, return new PDF bytes. Pure in-memory, no disk I/O.

    crossing_overlay_bytes — pre-rendered PDF of just the crossing block for
    this start_block variant. When supplied (customer has casing > 0), it is
    blended onto the skeleton *before* text so it sits in the geometry layer.
    Only 4 such overlays exist (one per start_block); each is ~20–50 KB and
    cached in IsometricService._CROSSING_OVERLAY_CACHE.
    """
    if not placeholders and crossing_overlay_bytes is None:
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

        # Embed the first available customer-text font. Priority: arial.ttf
        # (matches DXF style resolution), falls back to ARIALN.TTF.
        used_alias: Optional[str] = None
        for alias, fname in _OVERLAY_FONT_CANDIDATES:
            font_path = _font_path_or_default(font_dir, fname)
            if not font_path:
                continue
            try:
                page.insert_font(fontname=alias, fontfile=font_path)
                used_alias = alias
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
            # rendered cap-height roughly matches AutoCAD's plot.
            font_size_pt = max(ph["height"] * _DXF_HEIGHT_TO_PT, 4.0)

            x_offset_frac, y_offset_frac = _placeholder_offset(ph["text"])
            x_offset_pt = x_offset_frac * font_size_pt
            y_offset_pt = y_offset_frac * font_size_pt

            if ph["kind"] == "TEXT":
                halign = _HA_TO_PYMUPDF.get(ph["halign"], 0)
                # Use align point when halign != 0 (CENTER/RIGHT),
                # because in DXF it lives at the alignment box origin.
                if ph["halign"] in (1, 2, 4):
                    x_pt = ph["ax"] * MM_TO_PT
                    y_pt = h_pt - ph["ay"] * MM_TO_PT
                # TEXT valign=2 (Middle) — empirically baseline ≈ y_pt aligns
                # with the ezdxf-rendered labels in the title block.
                baseline_y = y_pt + y_offset_pt
                _stamp_text(page, x_pt + x_offset_pt, baseline_y, value,
                            font_size_pt, ph["rotation"], halign, used_alias)
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
                                ph["rotation"], halign, used_alias)

        return pdf.tobytes(garbage=3, deflate=True)
    finally:
        pdf.close()


def _stamp_text(page, x_pt: float, y_pt: float, text: str, size: float,
                rotation: float, halign: int, font_alias: Optional[str]) -> None:
    """Stamp a single line at the given PDF-point position.

    Uses `insert_text` (not insert_textbox) so long strings — like
    "JRG3-KNK-0000-PL-DG-026-<REFF_ID>" — never get clipped by a box that
    happens to be too narrow. We compute string width manually so we can
    still honor halign (CENTER / RIGHT shift the anchor by half / full
    width).
    """
    import pymupdf as _pm

    fontname = font_alias or "helv"
    # Compute text width in PDF points so we can offset for non-left aligns.
    try:
        text_width = page.get_text_length(text, fontsize=size, fontname=fontname)
    except Exception:
        # get_text_length needs the font registered; for the built-in
        # "helv" it always works. Estimate as fallback.
        text_width = len(text) * size * 0.5

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


def _count_wrapped_lines(page, text: str, fontsize: float,
                         width_pt: float, fontname: str) -> int:
    """Simulate word-wrapping to count lines matching insert_textbox behaviour.

    Measures each word with get_text_length (same font metrics PyMuPDF uses
    internally) and greedily fills lines — identical algorithm to a standard
    word-wrap. More accurate than the single-line-width-ratio estimate because
    it accounts for the varying waste at each line break.
    """
    try:
        words = text.split()
        if not words:
            return 1
        space_w = page.get_text_length(" ", fontsize=fontsize, fontname=fontname)
        n = 1
        line_w = 0.0
        for word in words:
            word_w = page.get_text_length(word, fontsize=fontsize, fontname=fontname)
            if line_w == 0.0:           # first word on a fresh line
                line_w = word_w
            elif line_w + space_w + word_w <= width_pt:
                line_w += space_w + word_w
            else:                        # word doesn't fit → new line
                n += 1
                line_w = word_w
        return n
    except Exception:
        # Fallback: character-count estimate with 0.65× factor (measured for
        # uppercase Arial — wider than the old 0.50 default which was too
        # narrow and caused insert_textbox to overflow silently).
        avg_char_w = fontsize * 0.65
        total_w = len(text) * avg_char_w
        return max(1, math.ceil(total_w / width_pt))


def _stamp_mtext_wrapped(page, x_pt: float, y_pt: float, text: str,
                         size: float, width_mm: float,
                         attachment_point: int, halign: int,
                         font_alias: Optional[str]) -> None:
    """Stamp a word-wrapping MTEXT field via PyMuPDF insert_textbox.

    Called when the placeholder MTEXT has DXF width > 0, meaning the template
    expects text to wrap within that column width (e.g. the ALAMAT address
    field that may span 2–3 lines). insert_textbox respects word boundaries
    and wraps at the supplied rect width, matching AutoCAD/ezdxf SVG output.

    Coordinate convention (same as compose_customer_pdf):
      x_pt, y_pt — PDF-points anchor already including per-placeholder
                   fine-tune offsets; y_pt is the AP row anchor (Y-down).
    """
    import pymupdf as _pm

    fontname  = font_alias or "helv"
    width_pt  = width_mm * MM_TO_PT
    line_h_pt = size * 1.5

    # Simulate word-wrap to get an accurate line count — avoids the
    # over-estimation that shifts text upward when a ratio estimate is used.
    n_lines = _count_wrapped_lines(page, text, size, width_pt, fontname)

    # block_h: height of actual text content (used for centering).
    # rect_h: block + half-line padding so the last line isn't clipped.
    block_h_pt = n_lines * line_h_pt
    rect_h_pt  = block_h_pt + line_h_pt * 0.5

    # Vertical rect bounds.
    #
    # AP=4-6 (Middle): y_pt is the DXF anchor for the MIDDLE of the text block.
    #
    # Calibration anchor (n=2, empirically confirmed correct):
    #   y0_n2 = y_pt - 1.15 * size
    #
    # For each additional line beyond 2, the rect top must move UP by L_half
    # so the rendered block stays centred at y_pt:
    #   y0 = y_pt - 1.15*size - (n-2) * L_HALF * size
    #
    # L_HALF ≈ 0.60 corresponds to the natural Arial line height ≈ 1.20 * size.
    # This is between the pure-centering value (0.75, too high for n>2) and the
    # fixed value (0.00, too low for n>2), which brackets the correct behaviour.
    #
    # AP=1-3 (Top): y0 at y_pt + 0.50*size (matches _stamp_text Top calibration).
    # AP=7-9 (Bottom): rect grows upward from y_pt.
    L_HALF = 0.60  # = actual_line_height / 2 ≈ 1.20 / 2
    if attachment_point in (1, 2, 3):    # Top anchor → rect grows downward
        y0 = y_pt + size * 0.50
        y1 = y0 + rect_h_pt
    elif attachment_point in (4, 5, 6):  # Middle anchor → calibrated per n_lines
        y0 = y_pt - size * 1.15 - (n_lines - 2) * L_HALF * size
        y1 = y0 + rect_h_pt
    else:                                 # Bottom anchor → rect grows upward
        y0 = y_pt - rect_h_pt
        y1 = y_pt

    # Horizontal extent from anchor based on halign.
    if halign == 1:    # center (AP 2,5,8)
        x0 = x_pt - width_pt / 2
        x1 = x_pt + width_pt / 2
    elif halign == 2:  # right (AP 3,6,9)
        x0 = x_pt - width_pt
        x1 = x_pt
    else:              # left (AP 1,4,7)
        x0 = x_pt
        x1 = x_pt + width_pt

    # Retry with an extra line if insert_textbox reports overflow (negative return).
    # This compensates for _count_wrapped_lines underestimating when get_text_length
    # is unavailable and the fallback heuristic is too narrow.
    for _attempt in range(3):
        rect = _pm.Rect(x0, y0, x1, y1)
        rc = page.insert_textbox(
            rect, text,
            fontname=fontname,
            fontsize=size,
            color=(0, 0, 0),
            align=halign,
        )
        if rc is None or rc >= 0:
            break
        # Overflow — expand rect by one extra line
        n_lines += 1
        block_h_pt = n_lines * line_h_pt
        rect_h_pt  = block_h_pt + line_h_pt * 0.5
        if attachment_point in (1, 2, 3):
            y1 = y0 + rect_h_pt
        elif attachment_point in (4, 5, 6):
            y0 = y_pt - size * 1.15 - (n_lines - 2) * L_HALF * size
            y1 = y0 + rect_h_pt
        else:
            y0 = y_pt - rect_h_pt
            y1 = y_pt


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
