"""Isometric drawing API endpoints. New dynamic engine, separate from legacy /api/dxf."""
import asyncio
import uuid as _uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Header
from fastapi.responses import FileResponse, Response

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
from app.services.job_store import JobStore
import ezdxf


router = APIRouter(prefix="/api/isometric", tags=["Isometric Engine"])

# ---------------------------------------------------------------------------
# Per-worker lazy singletons
# ---------------------------------------------------------------------------
# Each Uvicorn worker process initialises these independently on first request.
# - _heavy_semaphore: caps concurrent CPU-heavy ops per worker. With N workers
#   and MAX_CONCURRENT_HEAVY=1 the total concurrency = N (one per CPU core).
# - _job_store: file-based store shared across all workers via the filesystem.
# ---------------------------------------------------------------------------

_heavy_semaphore: Optional[asyncio.Semaphore] = None
_job_store: Optional[JobStore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _heavy_semaphore
    if _heavy_semaphore is None:
        n = getattr(get_settings(), "max_concurrent_heavy", 1)
        _heavy_semaphore = asyncio.Semaphore(n)
    return _heavy_semaphore


def _get_job_store() -> JobStore:
    global _job_store
    if _job_store is None:
        settings = get_settings()
        jobs_dir = Path(settings.jobs_path or settings.output_path) / "jobs"
        _job_store = JobStore(jobs_dir)
    return _job_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_font_dir(settings) -> Path:
    configured = Path(getattr(settings, "pdf_fonts_dir", "") or "")
    if configured and configured.is_dir():
        return configured
    for fallback in (Path("assets/fonts"), Path("testing/autocad_fonts")):
        if fallback.is_dir():
            return fallback
    return configured


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


# ---------------------------------------------------------------------------
# Generate (single)
# ---------------------------------------------------------------------------

@router.post("/generate", response_model=IsometricGenerateResponse)
async def generate_isometric(
    request: IsometricGenerateRequest,
    x_api_key: Optional[str] = Header(None),
):
    verify_api_key(x_api_key)
    service = get_isometric_service(module=request.module)
    async with _get_semaphore():
        success, message, file_path = await asyncio.to_thread(service.generate, request.model_dump())
    if not success:
        raise HTTPException(status_code=500, detail=message)
    return IsometricGenerateResponse(
        success=True,
        message=message,
        file_path=str(file_path) if file_path else None,
        file_name=file_path.name if file_path else None,
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

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
    return FileResponse(
        path=str(path),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/variants", response_model=VariantDirectionsResponse)
async def get_variants(x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    service = get_isometric_service()
    return service.get_variants_info()


# ---------------------------------------------------------------------------
# Preview endpoints — all CPU work offloaded to thread pool
# ---------------------------------------------------------------------------

@router.post("/preview-svg")
async def preview_svg(
    data: dict = Body(..., description="Customer data for text replacement"),
    x_api_key: Optional[str] = Header(None),
):
    """Render template dengan text replacement customer data → SVG. Supports module=SR|SK in body."""
    verify_api_key(x_api_key)
    module = data.pop("module", "SR")
    settings = get_settings()
    template_path = (
        getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
        if module == "SK"
        else getattr(settings, "isometric_template_path", None) or settings.template_path
    )

    def _render():
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
        return render_dxf_to_svg(doc, font_dir=_resolve_font_dir(settings))

    try:
        svg = await asyncio.to_thread(_render)
        return Response(content=svg, media_type="image/svg+xml")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview error: {str(e)}")


@router.post("/preview-drawing-svg")
async def preview_drawing_svg(
    payload: dict = Body(..., description="Drawing config + optional customer_data"),
    x_api_key: Optional[str] = Header(None),
):
    """Live preview: generate drawing in-memory + text replace + render SVG."""
    verify_api_key(x_api_key)
    module = payload.get("module", "SR")
    service = get_isometric_service(module=module)
    customer_data = payload.pop("customer_data", None)

    def _render():
        success, result = service.engine.generate_svg_preview(
            payload, customer_data, font_dir=service._pdf_font_dir()
        )
        if not success:
            raise ValueError(result)
        return result

    try:
        result = await asyncio.to_thread(_render)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return Response(content=result, media_type="image/svg+xml")


@router.post("/preview-drawing-pdf")
async def preview_drawing_pdf(
    payload: dict = Body(..., description="Drawing config + optional customer_data"),
    x_api_key: Optional[str] = Header(None),
):
    """Live preview as PDF using the production renderer."""
    verify_api_key(x_api_key)
    module = payload.get("module", "SR")
    service = get_isometric_service(module=module)
    customer_data = payload.pop("customer_data", None)

    def _render():
        try:
            return service.render_pdf_bytes_cached(payload, customer_data)
        except Exception:
            engine_req = {**payload, "customer_data": customer_data} if customer_data is not None else payload
            success, msg, doc = service.engine.generate(engine_req, None)
            if not success:
                raise ValueError(msg)
            service._apply_text_replacement(doc, customer_data)
            pdf_bytes = service.render_pdf_bytes(doc)
            need_crossing = service._customer_has_casing(customer_data) or any(
                s.get("type") == "crossing" for s in payload.get("segments", [])
            )
            if need_crossing:
                pdf_bytes = service.apply_crossing_overlay(
                    pdf_bytes, payload.get("start_block", "start-BR"), customer_data
                )
            return pdf_bytes

    try:
        async with _get_semaphore():
            pdf_bytes = await asyncio.to_thread(_render)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF preview error: {e}")
    return Response(content=pdf_bytes, media_type="application/pdf")


@router.get("/preview-blank-svg")
async def preview_blank_svg(
    module: str = "SR",
    x_api_key: Optional[str] = Header(None),
):
    """Render template kosong — untuk initial canvas view. Supports module=SR|SK."""
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
            "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0", "[21]": "0",
        }

    def _render():
        dxf_svc = DxfService(
            template_path=template_path,
            output_path=settings.output_path,
            oda_path=settings.oda_path,
            dwg_version=settings.dwg_version,
        )
        doc = ezdxf.readfile(str(template_path))
        dxf_svc.process_modelspace(doc.modelspace(), blank_replacements)
        dxf_svc.process_blocks(doc, blank_replacements)
        return render_dxf_to_svg(doc, font_dir=_resolve_font_dir(settings))

    try:
        svg = await asyncio.to_thread(_render)
        return Response(content=svg, media_type="image/svg+xml")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview error: {str(e)}")


# ---------------------------------------------------------------------------
# PDF cache management
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@router.get("/pdf-cache-status")
async def pdf_cache_status(module: str = "SR", x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    service   = get_isometric_service(module=module)
    cache_dir = service.output_dir / "pdf_cache"

    if not cache_dir.exists():
        return {"module": module, "entries": 0, "size_bytes": 0, "size_human": "0 B", "last_built": None}

    pdf_files  = list(cache_dir.glob("*.pdf"))
    meta_files = list(cache_dir.glob("*.meta.json"))
    total_size = sum(f.stat().st_size for f in pdf_files)
    last_mtime = max((f.stat().st_mtime for f in pdf_files), default=0.0)

    from datetime import datetime
    return {
        "module":     module,
        "entries":    len(meta_files),
        "size_bytes": total_size,
        "size_human": _human_size(int(total_size)),
        "last_built": datetime.fromtimestamp(last_mtime).strftime("%Y-%m-%d %H:%M") if last_mtime else None,
    }


@router.post("/pdf-cache/warm")
async def warm_pdf_cache(payload: dict = Body(...), x_api_key: Optional[str] = Header(None)):
    """Pre-build skeleton PDF caches. Body: {module, payloads: [{start_block, segments, ...}]}"""
    verify_api_key(x_api_key)
    module   = payload.get("module", "SR")
    payloads = payload.get("payloads", [])
    service  = get_isometric_service(module=module)

    def _warm():
        from app.services.pdf_template_cache import request_cache_key, load_cache
        cache_dir = service.output_dir / "pdf_cache"
        built = already = failed = 0
        errors: list = []
        for item in payloads:
            try:
                key = request_cache_key(
                    service.template_path,
                    item.get("start_block", "start-BR"),
                    item.get("segments", []),
                    item.get("combined_dims", []),
                )
                if load_cache(cache_dir, key) is not None:
                    already += 1
                    continue
                service.render_pdf_bytes_cached(item, customer_data=None)
                built += 1
            except Exception as exc:
                failed += 1
                errors.append(str(exc)[:120])
        return built, already, failed, errors

    built, already, failed, errors = await asyncio.to_thread(_warm)
    return {"total": len(payloads), "built": built, "already_cached": already, "failed": failed, "errors": errors}


@router.post("/pdf-cache/clear")
async def clear_pdf_cache(payload: dict = Body(default={}), x_api_key: Optional[str] = Header(None)):
    """Delete all cached skeleton PDFs for the given module. Body: {module}"""
    verify_api_key(x_api_key)
    module    = payload.get("module", "SR")
    service   = get_isometric_service(module=module)
    cache_dir = service.output_dir / "pdf_cache"
    deleted   = 0
    if cache_dir.exists():
        for f in cache_dir.iterdir():
            if f.suffix in (".pdf", ".json"):
                try:
                    f.unlink()
                    deleted += 1
                except Exception:
                    pass
    return {"deleted": deleted, "module": module}


# ---------------------------------------------------------------------------
# Bulk jobs — async, file-based state (shared across workers)
# ---------------------------------------------------------------------------
# Job state is stored in {jobs_path}/jobs/{job_id}.json so any worker can
# serve status/cancel requests regardless of which worker started the job.
# ---------------------------------------------------------------------------

def _make_bulk_callbacks(store: JobStore, job_id: str):
    """Return (progress_fn, is_cancelled_fn) callbacks for bulk workers."""
    def _progress(done: int, _total: int):
        store.update(job_id, done=done)

    def _is_cancelled() -> bool:
        return store.status(job_id) == "cancelled"

    return _progress, _is_cancelled


# -- Bulk PDF (merged) -------------------------------------------------------

@router.post("/bulk-pdf")
async def bulk_generate_pdf(payload: dict = Body(...), x_api_key: Optional[str] = Header(None)):
    """Start bulk PDF merge job. Returns job_id immediately; poll /bulk-pdf-status/{job_id}."""
    import datetime
    verify_api_key(x_api_key)

    items    = payload.get("items", [])
    count    = len(items)
    module   = items[0].get("module", "SR") if items else "SR"
    date_str = datetime.datetime.now().strftime("%d-%m-%Y")
    file_name = payload.get("file_name") or f"{module}_{count}_{date_str}"
    job_id   = _uuid.uuid4().hex
    store    = _get_job_store()
    service  = get_isometric_service(module=module)

    store.create(job_id, {"status": "running", "done": 0, "total": count,
                           "file_name": file_name, "download_url": None, "error": None})

    _progress, _is_cancelled = _make_bulk_callbacks(store, job_id)

    async def _run():
        success, message, pdf_path = await asyncio.to_thread(
            service.generate_bulk_pdf, items, file_name, _progress, _is_cancelled
        )
        if store.status(job_id) == "cancelled":
            return
        if success and pdf_path:
            store.update(job_id, status="done", done=count,
                         download_url=f"/api/isometric/download/{pdf_path.name}")
        else:
            store.update(job_id, status="error", error=message)

    asyncio.ensure_future(_run())
    return {"job_id": job_id, "total": count, "file_name": file_name}


@router.get("/bulk-pdf-status/{job_id}")
async def bulk_pdf_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    job = _get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/bulk-pdf/{job_id}")
async def cancel_bulk_pdf(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    store = _get_job_store()
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    store.update(job_id, status="cancelled")
    return {"success": True}


# -- Bulk PDF ZIP (individual PDFs zipped) -----------------------------------

@router.post("/bulk-pdf-zip")
async def bulk_generate_pdf_zip(payload: dict = Body(...), x_api_key: Optional[str] = Header(None)):
    """Start bulk PDF-ZIP job. Returns job_id immediately; poll /bulk-pdf-zip-status/{job_id}."""
    import datetime
    verify_api_key(x_api_key)

    items    = payload.get("items", [])
    count    = len(items)
    module   = items[0].get("module", "SR") if items else "SR"
    date_str = datetime.datetime.now().strftime("%d-%m-%Y")
    file_name = payload.get("file_name") or f"{module}_{count}_{date_str}"
    job_id   = _uuid.uuid4().hex
    store    = _get_job_store()
    service  = get_isometric_service(module=module)

    store.create(job_id, {"status": "running", "done": 0, "total": count,
                           "file_name": file_name, "download_url": None, "error": None})

    _progress, _is_cancelled = _make_bulk_callbacks(store, job_id)

    async def _run():
        success, message, zip_path = await asyncio.to_thread(
            service.generate_bulk_pdf_zip, items, file_name, _progress, _is_cancelled
        )
        if store.status(job_id) == "cancelled":
            return
        if success and zip_path:
            store.update(job_id, status="done", done=count,
                         download_url=f"/api/isometric/download/{zip_path.name}")
        else:
            store.update(job_id, status="error", error=message)

    asyncio.ensure_future(_run())
    return {"job_id": job_id, "total": count, "file_name": file_name}


@router.get("/bulk-pdf-zip-status/{job_id}")
async def bulk_pdf_zip_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    job = _get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/bulk-pdf-zip/{job_id}")
async def cancel_bulk_pdf_zip(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    store = _get_job_store()
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    store.update(job_id, status="cancelled")
    return {"success": True}


# -- Bulk DWG ----------------------------------------------------------------

@router.post("/bulk-dwg")
async def bulk_generate_dwg(payload: dict = Body(...), x_api_key: Optional[str] = Header(None)):
    """Start bulk DWG ZIP job. Returns job_id immediately; poll /bulk-dwg-status/{job_id}."""
    import datetime
    verify_api_key(x_api_key)

    items    = payload.get("items", [])
    count    = len(items)
    module   = items[0].get("module", "SR") if items else "SR"
    date_str = datetime.datetime.now().strftime("%d-%m-%Y")
    file_name = payload.get("file_name") or f"{module}_{count}_{date_str}"
    job_id   = _uuid.uuid4().hex
    store    = _get_job_store()
    service  = get_isometric_service(module=module)

    store.create(job_id, {"status": "running", "done": 0, "total": count,
                           "file_name": file_name, "download_url": None, "error": None})

    _progress, _is_cancelled = _make_bulk_callbacks(store, job_id)

    async def _run():
        success, message, zip_path = await asyncio.to_thread(
            service.generate_bulk_dwg, items, file_name, _progress, _is_cancelled
        )
        if store.status(job_id) == "cancelled":
            return
        if success and zip_path:
            store.update(job_id, status="done", done=count,
                         download_url=f"/api/isometric/download/{zip_path.name}")
        else:
            store.update(job_id, status="error", error=message)

    asyncio.ensure_future(_run())
    return {"job_id": job_id, "total": count, "file_name": file_name}


@router.get("/bulk-dwg-status/{job_id}")
async def bulk_dwg_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    job = _get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/bulk-dwg/{job_id}")
async def cancel_bulk_dwg(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    store = _get_job_store()
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    store.update(job_id, status="cancelled")
    return {"success": True}
