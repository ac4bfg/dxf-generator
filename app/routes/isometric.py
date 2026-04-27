"""Isometric drawing API endpoints. New dynamic engine, separate from legacy /api/dxf."""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import FileResponse

from app.config import get_settings
from app.schemas.isometric_schema import (
    BlockListResponse,
    IsometricGenerateRequest,
    IsometricGenerateResponse,
    VariantDirectionsResponse,
)
from app.services.dxf_service import DxfService
from app.services.dxf_to_svg import render_dxf_to_svg
from app.services.isometric_service import IsometricService
from fastapi import Body
from fastapi.responses import Response
import ezdxf


router = APIRouter(prefix="/api/isometric", tags=["Isometric Engine"])


def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    settings = get_settings()
    if settings.api_key:
        if not x_api_key or x_api_key != settings.api_key:
            raise HTTPException(status_code=401, detail="Invalid API Key")
    return True


def get_isometric_service(module: str = "SR") -> IsometricService:
    settings = get_settings()
    if module == "SK":
        template = getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
    else:
        template = getattr(settings, "isometric_template_path", None) or settings.template_path
    thumbnails = getattr(settings, "thumbnails_path", "thumbnails")
    return IsometricService(
        template_path=template,
        output_dir=settings.output_path,
        thumbnails_dir=thumbnails,
        oda_path=settings.oda_path,
        dwg_version=settings.dwg_version,
    )


@router.post("/generate", response_model=IsometricGenerateResponse)
async def generate_isometric(
    request: IsometricGenerateRequest,
    x_api_key: Optional[str] = Header(None),
):
    verify_api_key(x_api_key)
    service = get_isometric_service(module=request.module)
    success, message, file_path = service.generate(request.model_dump())
    if not success:
        raise HTTPException(status_code=500, detail=message)
    return IsometricGenerateResponse(
        success=True,
        message=message,
        file_path=str(file_path) if file_path else None,
        file_name=file_path.name if file_path else None,
    )


@router.get("/download/{filename}")
async def download_isometric(
    filename: str,
    x_api_key: Optional[str] = Header(None),
):
    verify_api_key(x_api_key)
    from urllib.parse import unquote
    decoded = unquote(filename)
    settings = get_settings()
    file_path = Path(settings.output_path) / decoded
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {decoded}")
    return FileResponse(
        path=str(file_path),
        filename=decoded,
        media_type="application/octet-stream",
    )


@router.get("/blocks", response_model=BlockListResponse)
async def list_blocks(module: str = "SR", x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    service = get_isometric_service(module=module)
    return BlockListResponse(blocks=service.list_blocks())


@router.get("/thumbnail/{block_name}")
async def get_thumbnail(block_name: str, module: str = "SR", x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    service = get_isometric_service(module=module)
    path = service.get_thumbnail_path(block_name)
    if not path:
        raise HTTPException(status_code=404, detail=f"Thumbnail not found for: {block_name}")
    return FileResponse(path=str(path), media_type="image/jpeg")


@router.get("/variants", response_model=VariantDirectionsResponse)
async def get_variants(x_api_key: Optional[str] = Header(None)):
    """Returns variant directions mapping and scale presets."""
    verify_api_key(x_api_key)
    service = get_isometric_service()
    return service.get_variants_info()


@router.post("/preview-svg")
async def preview_svg(
    data: dict = Body(..., description="Customer data for text replacement"),
    x_api_key: Optional[str] = Header(None),
):
    """Render template dengan text replacement customer data → SVG string. Supports module=SR|SK in body."""
    verify_api_key(x_api_key)
    module = data.pop("module", "SR")
    settings = get_settings()
    if module == "SK":
        template_path = getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
    else:
        template_path = getattr(settings, "isometric_template_path", None) or settings.template_path

    try:
        dxf_svc = DxfService(
            template_path=template_path,
            output_path=settings.output_path,
            oda_path=settings.oda_path,
            dwg_version=settings.dwg_version,
        )
        replacements = dxf_svc.prepare_data(data)

        doc = ezdxf.readfile(str(template_path))
        dxf_svc.process_modelspace(doc.modelspace(), replacements)
        dxf_svc.process_blocks(doc, replacements)

        svg = render_dxf_to_svg(doc)
        return Response(content=svg, media_type="image/svg+xml")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview error: {str(e)}")


@router.post("/preview-drawing-svg")
async def preview_drawing_svg(
    payload: dict = Body(..., description="Drawing config + optional customer_data"),
    x_api_key: Optional[str] = Header(None),
):
    """Live preview: generate drawing in-memory + text replace + render SVG.
    Payload: {start_block, segments, combined_dims, start_insert?, start_rotation?, customer_data?, module?}
    """
    verify_api_key(x_api_key)
    module = payload.get("module", "SR")
    service = get_isometric_service(module=module)
    customer_data = payload.pop("customer_data", None)
    success, result = service.engine.generate_svg_preview(payload, customer_data)
    if not success:
        raise HTTPException(status_code=500, detail=result)
    return Response(content=result, media_type="image/svg+xml")


@router.get("/preview-blank-svg")
async def preview_blank_svg(
    module: str = "SR",
    x_api_key: Optional[str] = Header(None),
):
    """Render template kosong (placeholder replaced dengan dash/0) — untuk initial view. Supports module=SR|SK."""
    verify_api_key(x_api_key)
    settings = get_settings()
    if module == "SK":
        template_path = getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[SEKTOR]": "-",
            "[NO_SK]": "-",
            "[1]": "0", "[2]": "0", "[3]": "0", "[6]": "0", "[7]": "0",
        }
    else:
        template_path = getattr(settings, "isometric_template_path", None) or settings.template_path
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[SEKTOR]": "-",
            "[NO_MGRT]": "-", "[SN_AWAL]": "-", "[KOORDINAT_TAPPING]": "-",
            "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0",
        }

    try:
        dxf_svc = DxfService(
            template_path=template_path,
            output_path=settings.output_path,
            oda_path=settings.oda_path,
            dwg_version=settings.dwg_version,
        )
        doc = ezdxf.readfile(str(template_path))
        dxf_svc.process_modelspace(doc.modelspace(), blank_replacements)
        dxf_svc.process_blocks(doc, blank_replacements)

        svg = render_dxf_to_svg(doc)
        return Response(content=svg, media_type="image/svg+xml")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview error: {str(e)}")
