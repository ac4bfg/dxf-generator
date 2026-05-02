"""Service layer for isometric API endpoints."""
import datetime
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.isometric_engine import IsometricEngine, VARIANT_DIRECTIONS, VARIANT_SCALE


class IsometricService:
    # Pre-rendered crossing block PDFs — keyed by "{template_path}::{start_block}".
    # Only 4 entries max (one per SR start_block variant), each ~20-50 KB.
    # None means "already tried and block not found / render failed".
    _CROSSING_OVERLAY_CACHE: Dict[str, Optional[bytes]] = {}

    def __init__(self, template_path: str, output_dir: str,
                 thumbnails_dir: Optional[str] = None,
                 oda_path: Optional[str] = None,
                 dwg_version: str = "ACAD2018"):
        self.template_path = Path(template_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir = Path(thumbnails_dir) if thumbnails_dir else None
        self.oda_path = oda_path
        self.dwg_version = dwg_version
        self.engine = IsometricEngine(str(self.template_path))

    def generate(self, request: Dict[str, Any]) -> Tuple[bool, str, Optional[Path]]:
        fmt = request.get("output_format", "dxf").lower()
        customer_data = request.get("customer_data")
        base_name = request.get("file_name") or f"isometric_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        dxf_path = self.output_dir / f"{base_name}.dxf"

        # Generate in-memory so we can apply text replacement before saving
        success, msg, doc = self.engine.generate(request, None)
        if not success:
            return False, msg, None

        self._apply_text_replacement(doc, customer_data)

        if fmt == "pdf":
            pdf_path = self.output_dir / f"{base_name}.pdf"
            ok = self._generate_pdf(doc, pdf_path)
            if not ok:
                return False, "PDF generation failed", None
            return True, "Generated successfully", pdf_path

        doc.saveas(str(dxf_path))

        if fmt == "dwg":
            if not self._oda_available():
                return False, "ODA converter not available for DWG output", dxf_path
            dwg_path = self.output_dir / f"{base_name}.dwg"
            ok = self._convert_to_dwg(dxf_path, dwg_path)
            if not ok:
                return False, "DWG conversion failed", dxf_path
            try:
                dxf_path.unlink(missing_ok=True)
            except Exception:
                pass
            return True, "Generated successfully", dwg_path

        return True, msg, dxf_path

    def _generate_pdf(self, doc, output_path: Path) -> bool:
        """Render the in-memory Drawing to PDF using the production renderer
        (font config, force-overrides, faux-bold, color preservation, OLE
        overlay compositing — see app/services/pdf_renderer.py)."""
        import logging
        log = logging.getLogger(__name__)
        try:
            self._fix_image_paths(doc)
            from app.services.pdf_renderer import render_doc_to_pdf
            render_doc_to_pdf(
                doc, output_path,
                font_dir=self._pdf_font_dir(),
                logo_dir=self._pdf_logo_dir(),
            )
            return True
        except Exception as e:
            log.error("PDF generation error: %s", e, exc_info=True)
            return False

    def render_pdf_bytes(self, doc) -> bytes:
        """In-memory PDF rendering for the preview endpoint."""
        self._fix_image_paths(doc)
        from app.services.pdf_renderer import render_doc_to_pdf_bytes
        return render_doc_to_pdf_bytes(
            doc,
            font_dir=self._pdf_font_dir(),
            logo_dir=self._pdf_logo_dir(),
        )

    # ------------------------------------------------------------------
    # Skeleton-cache fast path
    # ------------------------------------------------------------------

    def render_pdf_bytes_cached(self, request: Dict[str, Any],
                                customer_data: Optional[Dict] = None) -> bytes:
        """Fast-path PDF render using the per-(template, structure) skeleton
        cache. Geometry is rendered once per cache key; per-customer text
        is overlaid via PyMuPDF.

        Falls back transparently to the full ezdxf renderer if anything in
        the fast-path fails or if the request has no placeholders.
        """
        from app.services.pdf_template_cache import (
            request_cache_key, load_cache, save_cache,
            extract_placeholder_entities, compose_customer_pdf,
        )
        from app.services.pdf_renderer import (
            render_doc_to_pdf_bytes, get_page_height_mm,
        )
        from app.services.dxf_service import DxfService

        template_path = self.template_path
        cache_dir = self.output_dir / "pdf_cache"
        key = request_cache_key(
            template_path,
            request.get("start_block", "start-BR"),
            request.get("segments", []),
            request.get("combined_dims", []),
        )

        # Resolve customer text replacements (always — placeholders need
        # values whether the doc is freshly built or pulled from cache).
        dxf_svc = DxfService(
            template_path=str(template_path),
            output_path=str(self.output_dir),
            oda_path=self.oda_path or "",
            dwg_version=self.dwg_version,
        )
        if customer_data:
            replacements = dxf_svc.prepare_data(customer_data)
        else:
            replacements = {
                "[TANGGAL]": dxf_svc.generate_tanggal_indonesia(),
                "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
                "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[SEKTOR]": "-",
                "[NO_MGRT]": "-", "[SN_AWAL]": "-", "[KOORDINAT_TAPPING]": "-",
                "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0", "[21]": "0",
                "[NO_SK]": "-",
                "[1]": "0", "[2]": "0", "[3]": "0", "[6]": "0",
            }

        # Crossing is never baked into the skeleton (stripped by filter).
        # Apply overlay when customer has casing OR segments had a crossing
        # type entry (custom drawing) — both cases use the same 4 cached bytes.
        start_block = request.get("start_block", "start-BR")
        need_crossing = self._customer_has_casing(customer_data) or any(
            s.get("type") == "crossing" for s in request.get("segments", [])
        )
        crossing_bytes = self._get_crossing_overlay(start_block) if need_crossing else None

        # Try cache hit first.
        cached = load_cache(cache_dir, key)
        if cached is not None:
            skeleton_bytes, placeholders = cached
            page_h_mm = self._cached_page_height_mm(template_path)
            return compose_customer_pdf(
                skeleton_bytes, placeholders, replacements,
                page_height_mm=page_h_mm,
                font_dir=self._pdf_font_dir(),
                crossing_overlay_bytes=crossing_bytes,
            )

        # Miss → build skeleton (geometry only, placeholders filtered out)
        # and the placeholder metadata, save to cache, then overlay.
        success, msg, doc = self.engine.generate(request, None)
        if not success:
            raise RuntimeError(f"engine.generate failed: {msg}")

        self._fix_image_paths(doc)
        placeholders = extract_placeholder_entities(doc)
        page_h_mm = get_page_height_mm(doc, layout_name="SR")

        from app.services.pdf_template_cache import _skip_placeholders_and_crossing
        skeleton_bytes = render_doc_to_pdf_bytes(
            doc,
            font_dir=self._pdf_font_dir(),
            logo_dir=self._pdf_logo_dir(),
            filter_func=_skip_placeholders_and_crossing,
        )
        save_cache(cache_dir, key, skeleton_bytes, placeholders)
        self._cache_page_height(template_path, page_h_mm)

        return compose_customer_pdf(
            skeleton_bytes, placeholders, replacements,
            page_height_mm=page_h_mm,
            font_dir=self._pdf_font_dir(),
            crossing_overlay_bytes=crossing_bytes,
        )

    # Per-process small caches (cheap to recompute, but avoids repeated DXF
    # re-read just to get the page height on cache hits).
    _PAGE_HEIGHT_CACHE: Dict[str, float] = {}

    def _cached_page_height_mm(self, template_path: Path) -> float:
        key = str(template_path)
        if key in self._PAGE_HEIGHT_CACHE:
            return self._PAGE_HEIGHT_CACHE[key]
        # No cached value yet — read template once to determine it.
        import ezdxf
        from app.services.pdf_renderer import get_page_height_mm
        doc = ezdxf.readfile(str(template_path))
        h = get_page_height_mm(doc, layout_name="SR")
        self._PAGE_HEIGHT_CACHE[key] = h
        return h

    def _cache_page_height(self, template_path: Path, h: float) -> None:
        self._PAGE_HEIGHT_CACHE[str(template_path)] = h

    def _pdf_font_dir(self) -> Path:
        from app.config import get_settings
        s = get_settings()
        configured = Path(getattr(s, "pdf_fonts_dir", "") or "")
        if configured and configured.is_dir():
            return configured
        # fall back to repo testing directory if it exists
        fallback = Path("testing/autocad_fonts")
        return fallback if fallback.is_dir() else configured

    def _pdf_logo_dir(self) -> Optional[Path]:
        from app.config import get_settings
        s = get_settings()
        configured = Path(getattr(s, "pdf_logo_dir", "") or "")
        if configured and configured.is_dir():
            return configured
        fallback = Path("testing/logo")
        return fallback if fallback.is_dir() else None

    def _fix_crossing_mtext_direction(self, doc, start_block: str) -> None:
        """Prepare crossing block MTEXT for ezdxf PDF rendering.

        1. Convert text_direction vectors to explicit rotation angles — ezdxf's PDF
           renderer ignores text_direction and renders at 0° without this fix.
        2. Correct the insert position for MTEXT without \\A1;: ezdxf renders
           top-aligned MTEXT with a systematic centroid offset from the declared
           insert. Pre-measured offsets are applied so the visual center matches
           the DXF insert (= AutoCAD MiddleCenter position).
        """
        import math as _math
        from app.services.isometric_engine import CROSSING_BLOCK_MAP
        from app.services.dxf_to_svg import _BLOCK_MTEXT_ANGLE_CORRECTION_PDF as _BLOCK_MTEXT_ANGLE_CORRECTION
        crossing_name = CROSSING_BLOCK_MAP.get(start_block, "")
        if not crossing_name or crossing_name not in doc.blocks:
            return
        try:
            for entity in doc.blocks[crossing_name]:
                if entity.dxftype() != "MTEXT":
                    continue
                # Convert text_direction to explicit rotation
                td = entity.dxf.get("text_direction", None)
                if td is not None:
                    angle_deg = _math.degrees(_math.atan2(td.y, td.x))
                    entity.dxf.rotation = angle_deg
                    entity.dxf.discard("text_direction")
                else:
                    angle_deg = float(entity.dxf.get("rotation", 0) or 0)
                # Apply centering correction for Middle attachment MTEXT without \A1;
                if entity.dxf.get("attachment_point", 1) in (4, 5, 6):
                    if "\\A1;" not in (entity.text or ""):
                        correction = _BLOCK_MTEXT_ANGLE_CORRECTION.get(round(angle_deg))
                        if correction is not None:
                            dx, dy = correction
                            tc = entity.dxf.insert
                            entity.dxf.insert = (
                                float(tc.x) + dx,
                                float(tc.y) + dy,
                                float(getattr(tc, "z", 0)),
                            )
        except Exception:
            pass

    def _fix_image_paths(self, doc) -> None:
        """Update IMAGE_DEF file references to resolve relative to the template directory."""
        images_dir = self.template_path.parent
        try:
            for imagedef in doc.objects.query("IMAGEDEF"):
                original = Path(imagedef.dxf.filename)
                # Try filename-only lookup in the template dir and a sibling images/ folder
                for search_dir in (images_dir, images_dir / "images"):
                    candidate = search_dir / original.name
                    if candidate.exists():
                        imagedef.dxf.filename = str(candidate)
                        break
        except Exception:
            pass

    def _apply_text_replacement(self, doc, customer_data: Optional[Dict] = None):
        from app.services.dxf_service import DxfService
        dxf_svc = DxfService(
            template_path=str(self.template_path),
            output_path=str(self.output_dir),
            oda_path=self.oda_path or "",
            dwg_version=self.dwg_version,
        )
        if customer_data:
            replacements = dxf_svc.prepare_data(customer_data)
        else:
            replacements = {
                "[TANGGAL]": dxf_svc.generate_tanggal_indonesia(),
                "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
                "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[SEKTOR]": "-",
                "[NO_MGRT]": "-", "[SN_AWAL]": "-", "[KOORDINAT_TAPPING]": "-",
                "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0", "[21]": "0",
            }
        dxf_svc.process_modelspace(doc.modelspace(), replacements)
        dxf_svc.process_blocks(doc, replacements)

    # ------------------------------------------------------------------
    # Crossing overlay helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _customer_has_casing(customer_data: Optional[Dict]) -> bool:
        """Return True when customer_data indicates casing material > 0."""
        if not customer_data:
            return False
        mats = customer_data.get("materials") or {}
        raw = mats.get("casing") or customer_data.get("casing") or customer_data.get("21", "0")
        try:
            return float(str(raw).replace(",", ".")) > 0
        except (ValueError, TypeError):
            return False

    def _get_crossing_overlay(self, start_block: str) -> Optional[bytes]:
        """Return pre-rendered PDF bytes for the crossing block of start_block.

        Storage hierarchy:
          1. In-memory dict  (_CROSSING_OVERLAY_CACHE) — fastest, per-process
          2. Disk cache      (output/pdf_cache/crossing_{start_block}.pdf)
          3. Render from DXF — slowest, result saved to both layers

        Max 4 entries (one per start_block variant), ~50–150 KB each.
        """
        from app.services.isometric_engine import CROSSING_BLOCK_MAP
        mem_key = f"{self.template_path}::{start_block}"

        # 1. In-memory hit
        if mem_key in IsometricService._CROSSING_OVERLAY_CACHE:
            return IsometricService._CROSSING_OVERLAY_CACHE[mem_key]

        crossing_name = CROSSING_BLOCK_MAP.get(start_block)
        if not crossing_name:
            IsometricService._CROSSING_OVERLAY_CACHE[mem_key] = None
            return None

        # 2. Disk hit
        disk_path = self.output_dir / "pdf_cache" / f"crossing_{start_block}.pdf"
        if disk_path.is_file():
            data = disk_path.read_bytes()
            IsometricService._CROSSING_OVERLAY_CACHE[mem_key] = data
            return data

        # 3. Render from DXF and save to both layers
        try:
            from app.services.pdf_renderer import render_doc_to_pdf_bytes
            from app.services.pdf_template_cache import _skip_placeholders

            minimal_req = {
                "module": "SR",
                "start_block": start_block,
                "start_rotation": 0,
                "output_format": "pdf",
                "segments": [{"type": "crossing"}],
                "combined_dims": [],
            }
            success, msg, doc = self.engine.generate(minimal_req, None)
            if not success:
                raise RuntimeError(msg)

            self._fix_image_paths(doc)
            # ezdxf renders MTEXT `rotation` correctly but ignores `text_direction`
            # vectors in PDF output. The crossing block CSG label uses text_direction
            # (AutoCAD-native) instead of rotation. Convert before rendering so the
            # CSG label appears at the correct isometric angle (same as dim text).
            self._fix_crossing_mtext_direction(doc, start_block)
            overlay = render_doc_to_pdf_bytes(
                doc,
                font_dir=self._pdf_font_dir(),
                logo_dir=self._pdf_logo_dir(),
                filter_func=_skip_placeholders,
            )

            # Persist to disk
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_bytes(overlay)

            IsometricService._CROSSING_OVERLAY_CACHE[mem_key] = overlay
            return overlay
        except Exception as e:
            print(f"[WARNING] Could not render crossing overlay for {start_block}: {e}")
            IsometricService._CROSSING_OVERLAY_CACHE[mem_key] = None
            return None

    def apply_crossing_overlay(self, pdf_bytes: bytes,
                               start_block: str,
                               customer_data: Optional[Dict]) -> bytes:
        """Overlay the crossing block onto pdf_bytes when customer has casing > 0.
        Used by the full-render (non-cached) fallback path."""
        if not self._customer_has_casing(customer_data):
            return pdf_bytes
        crossing_bytes = self._get_crossing_overlay(start_block)
        if not crossing_bytes:
            return pdf_bytes
        try:
            import pymupdf as _pm
            base = _pm.open(stream=pdf_bytes, filetype="pdf")
            cross = _pm.open(stream=crossing_bytes, filetype="pdf")
            base[0].show_pdf_page(base[0].rect, cross, 0)
            result = base.tobytes(garbage=3, deflate=True)
            base.close()
            cross.close()
            return result
        except Exception as e:
            print(f"[WARNING] Crossing overlay failed: {e}")
            return pdf_bytes

    def list_blocks(self) -> List[Dict[str, Any]]:
        blocks = self.engine.list_blocks()
        for b in blocks:
            thumb = self._thumbnail_path(b["name"])
            b["thumbnail_url"] = f"/api/isometric/thumbnail/{b['name']}" if thumb else None
        return blocks

    def get_thumbnail_path(self, block_name: str) -> Optional[Path]:
        return self._thumbnail_path(block_name)

    def get_variants_info(self) -> Dict[str, Any]:
        return {
            "variants": {k: dict(v) for k, v in VARIANT_DIRECTIONS.items()},
            "variant_scales": {k: list(v) for k, v in VARIANT_SCALE.items()},
        }

    THUMBNAIL_ALIASES = {
        "REGULATOR-2": "regulator",
        "valve": "ball_valve_1-2",
    }

    def _thumbnail_path(self, block_name: str) -> Optional[Path]:
        if not self.thumbnails_dir or not self.thumbnails_dir.exists():
            return None
        lookup = self.THUMBNAIL_ALIASES.get(block_name, block_name)
        for ext in (".jpg", ".jpeg", ".png"):
            p = self.thumbnails_dir / f"{lookup}{ext}"
            if p.exists():
                return p
        return None

    def _oda_available(self) -> bool:
        if not self.oda_path:
            return False
        return os.path.exists(self.oda_path)

    def _convert_to_dwg(self, dxf_path: Path, dwg_path: Path) -> bool:
        try:
            in_dir = str(dxf_path.parent)
            out_dir = str(dwg_path.parent)
            cmd = [self.oda_path, in_dir, out_dir, self.dwg_version, "DWG", "0", "1", dxf_path.name]
            env = os.environ.copy()
            env.setdefault("DISPLAY", ":99")
            result = subprocess.run(cmd, capture_output=True, timeout=60, env=env)
            return result.returncode == 0 and dwg_path.exists()
        except Exception:
            return False
