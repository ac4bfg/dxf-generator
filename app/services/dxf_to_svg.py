"""DXF → SVG converter menggunakan ezdxf SVG backend.
Output di-normalize ke DXF native coord space (paper mm) supaya
pipeline drawing (yang pakai DXF coords) bisa overlay dengan benar."""
import re
import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.addons.drawing import layout
from ezdxf.addons.drawing.config import Configuration, LineweightPolicy


# A3 paper size mm (SR template default)
PAPER_WIDTH_MM = 420.0
PAPER_HEIGHT_MM = 297.0

# Final stroke scale: dibagi berapa nilai stroke-width SVG yg dikeluarkan ezdxf.
# Ini post-process regex — paling reliable untuk ngontrol ketebalan garis preview
# karena `lineweight_scaling`/`fixed_stroke_width` di ezdxf sering saling override.
# Semakin BESAR nilainya, semakin TIPIS garis. 1.0 = apa adanya dari ezdxf.
STROKE_SHRINK_FACTOR = 0.5


def render_dxf_to_svg(doc, paper_width_mm: float = PAPER_WIDTH_MM,
                      paper_height_mm: float = PAPER_HEIGHT_MM) -> str:
    """Render modelspace of doc ke SVG string, viewBox dalam paper mm coords.

    Output SVG's viewBox = "0 0 paper_width paper_height".
    Content di-wrap dalam <g transform="scale(...)"> supaya ezdxf's 1M coord space
    di-convert ke paper mm.
    """
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

    # Find </defs> or first actual content to wrap
    # Strategy: replace opening <svg> & wrap inner content in <g transform="scale...">
    svg_open_match = re.match(r'^(<svg[^>]*>)([\s\S]*)(</svg>)\s*$', svg_raw)
    if not svg_open_match:
        return svg_raw
    inner = svg_open_match.group(2)

    # Flip Y supaya match our convention (math Y up, screen Y down via viewBox negative range)
    new_vb = f'0 {-paper_height_mm} {paper_width_mm} {paper_height_mm}'
    new_svg_open = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{new_vb}" '
        f'style="background:#1f2937">'
    )
    # Wrap inner: scale down + flip Y. Transform sequence: scale(sx, -sy) translate(0, -vb_h)
    # Actually: first translate to flip, then scale. Or combine.
    # ezdxf outputs in screen-coord with Y going DOWN from 0 to vb_h (top-left origin).
    # We want output where Y=0 at bottom (or natural math). Since our svgViewBox uses
    # negative Y (screen conv), keep Y going down. Just scale.
    # Actually just use positive Y viewBox matching ezdxf convention:
    new_vb = f'0 0 {paper_width_mm} {paper_height_mm}'
    new_svg_open = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{new_vb}" '
        f'style="background:#1f2937">'
    )
    wrapped_inner = f'<g transform="scale({sx:.6f} {sy:.6f})">{inner}</g>'
    return new_svg_open + wrapped_inner + '</svg>'