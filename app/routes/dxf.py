from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import FileResponse

from app.config import get_settings, get_template_path
from app.schemas.dxf_schema import (
    AsbuiltGenerateRequest,
    BulkGenerateRequest,
    GenerateResponse,
    HealthResponse,
)
from app.services.dxf_service import DxfService


router = APIRouter(prefix="/api/dxf", tags=["DXF Generator"])


def get_dxf_service() -> DxfService:
    settings = get_settings()
    return DxfService(
        template_path=settings.template_path,
        output_path=settings.output_path
    )


def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    settings = get_settings()
    if settings.api_key:
        if not x_api_key or x_api_key != settings.api_key:
            raise HTTPException(status_code=401, detail="Invalid API Key")
    return True


@router.get("/health", response_model=HealthResponse)
async def health_check():
    settings = get_settings()
    template_path = Path(settings.template_path)
    return HealthResponse(
        status="healthy",
        template_found=template_path.exists()
    )


@router.post("/generate", response_model=GenerateResponse)
async def generate_single(
    request: AsbuiltGenerateRequest,
    _api_key: bool = Header(None, include_in_alias=False)
):
    verify_api_key(_api_key)

    service = get_dxf_service()
    success, message, file_path = service.generate_single(request.model_dump())

    if not success:
        raise HTTPException(status_code=500, detail=message)

    return GenerateResponse(
        success=True,
        message=message,
        file_path=str(file_path) if file_path else None,
        file_name=f"ASBUILT_{request.reff_id}.dxf"
    )


@router.get("/download/{reff_id}")
async def download_file(
    reff_id: str,
    _api_key: Optional[str] = Header(None)
):
    verify_api_key(_api_key)

    # URL decode reff_id - handle + dan %20 keduanya sebagai spasi
    from urllib.parse import unquote
    decoded_reff_id = unquote(reff_id)
    decoded_reff_id = decoded_reff_id.replace('+', ' ')
    
    # Sanitasi - harus sama dengan yang di generate_single
    safe_reff_id = decoded_reff_id.replace(' ', '_').replace('/', '_').replace('\\', '_')
    
    settings = get_settings()
    file_path = Path(settings.output_path) / f"ASBUILT_{safe_reff_id}.dxf"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {safe_reff_id}")

    # Use FileResponse - lebih reliable untuk download
    # File akan dihapus setelah response selesai dikirim
    response = FileResponse(
        path=str(file_path),
        filename=f"ASBUILT_{safe_reff_id}.dxf",
        media_type="application/octet-stream"
    )
    
    # Schedule deletion after response is sent
    # This ensures file is not deleted before download completes
    response.headers["X-FastAPI-Delete"] = str(file_path)
    
    return response


@router.get("/delete-file/{safe_reff_id}")
async def delete_file(
    safe_reff_id: str,
    _api_key: Optional[str] = Header(None)
):
    """Endpoint untuk delete file setelah Laravel selesai download"""
    verify_api_key(_api_key)
    
    settings = get_settings()
    file_path = Path(settings.output_path) / f"ASBUILT_{safe_reff_id}.dxf"
    
    if file_path.exists():
        file_path.unlink()
        return {"success": True, "deleted": str(file_path)}
    
    return {"success": False, "message": "File not found"}


@router.post("/bulk-generate")
async def bulk_generate(
    request: BulkGenerateRequest,
    _api_key: Optional[str] = Header(None)
):
    verify_api_key(_api_key)

    if not request.items:
        raise HTTPException(status_code=400, detail="No items provided")

    service = get_dxf_service()

    if request.mode == "zip":
        success, message, zip_path = service.generate_bulk_zip(
            [item.model_dump() for item in request.items]
        )

        if not success:
            raise HTTPException(status_code=500, detail=message)

        return GenerateResponse(
            success=True,
            message=message,
            file_path=str(zip_path) if zip_path else None,
            file_name=zip_path.name if zip_path else None
        )

    raise HTTPException(status_code=400, detail=f"Unsupported mode: {request.mode}")


@router.get("/bulk-download/{filename}")
async def download_bulk_file(
    filename: str,
    _api_key: Optional[str] = Header(None)
):
    verify_api_key(_api_key)

    # URL decode filename
    from urllib.parse import unquote
    decoded_filename = unquote(filename)
    
    settings = get_settings()
    file_path = Path(settings.output_path) / decoded_filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {decoded_filename}")

    # Use FileResponse
    return FileResponse(
        path=str(file_path),
        filename=decoded_filename,
        media_type="application/octet-stream"
    )


@router.get("/delete-bulk/{filename}")
async def delete_bulk_file(
    filename: str,
    _api_key: Optional[str] = Header(None)
):
    """Endpoint untuk delete bulk file setelah Laravel selesai download"""
    verify_api_key(_api_key)
    
    from urllib.parse import unquote
    decoded_filename = unquote(filename)
    
    settings = get_settings()
    file_path = Path(settings.output_path) / decoded_filename
    
    if file_path.exists():
        file_path.unlink()
        return {"success": True, "deleted": str(file_path)}
    
    return {"success": False, "message": "File not found"}
