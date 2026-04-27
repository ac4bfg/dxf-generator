"""Service layer for isometric API endpoints."""
import datetime
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.isometric_engine import IsometricEngine, VARIANT_DIRECTIONS, VARIANT_SCALE


class IsometricService:
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
        import logging
        log = logging.getLogger(__name__)
        try:
            from ezdxf.addons.drawing import Frontend, RenderContext, pymupdf, layout
            from ezdxf.addons.drawing import config as draw_cfg

            # Fix IMAGE_DEF paths so logo & embedded images resolve correctly
            self._fix_image_paths(doc)

            # Use paperspace layout (A3 already configured in template)
            target_layout = doc.modelspace()
            for name in doc.layout_names_in_taborder():
                if name != "Model":
                    candidate = doc.layouts.get(name)
                    if candidate is not None:
                        target_layout = candidate
                        break

            context = RenderContext(doc)
            backend = pymupdf.PyMuPdfBackend()
            cfg = draw_cfg.Configuration(
                background_policy=draw_cfg.BackgroundPolicy.WHITE,
                color_policy=draw_cfg.ColorPolicy.MONOCHROME,
                lineweight_policy=draw_cfg.LineweightPolicy.ABSOLUTE,
            )
            Frontend(context, backend, config=cfg).draw_layout(target_layout, finalize=True)

            # A3 landscape: 420 × 297 mm, no margins (template already has title block)
            page = layout.Page(
                420, 297, layout.Units.mm,
                margins=layout.Margins(top=0, right=0, bottom=0, left=0),
            )
            pdf_bytes = backend.get_pdf_bytes(page)

            output_path.write_bytes(pdf_bytes)
            return True
        except Exception as e:
            log.error("PDF generation error: %s", e)
            return False

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
                "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0",
            }
        dxf_svc.process_modelspace(doc.modelspace(), replacements)
        dxf_svc.process_blocks(doc, replacements)

    def list_blocks(self) -> List[Dict[str, Any]]:
        blocks = self.engine.list_blocks()
        for b in blocks:
            # "start" is a virtual block, use start-BR as its default thumbnail
            lookup_name = "start-BR" if b["name"] == "start" else b["name"]
            thumb = self._thumbnail_path(lookup_name)
            b["thumbnail_url"] = f"/api/isometric/thumbnail/{lookup_name}" if thumb else None
        return blocks

    def get_thumbnail_path(self, block_name: str) -> Optional[Path]:
        return self._thumbnail_path(block_name)

    def get_variants_info(self) -> Dict[str, Any]:
        return {
            "variants": {k: dict(v) for k, v in VARIANT_DIRECTIONS.items()},
            "variant_scales": {k: list(v) for k, v in VARIANT_SCALE.items()},
        }

    def _thumbnail_path(self, block_name: str) -> Optional[Path]:
        if not self.thumbnails_dir or not self.thumbnails_dir.exists():
            return None
        for ext in (".jpg", ".jpeg", ".png"):
            p = self.thumbnails_dir / f"{block_name}{ext}"
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
