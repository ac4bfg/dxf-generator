"""Isometric drawing API endpoints. New dynamic engine, separate from legacy /api/dxf."""
import asyncio
import uuid as _uuid
from pathlib import Path
from typing import List, Optional

import tempfile

from fastapi import APIRouter, BackgroundTasks, Body, File, Form, HTTPException, Header, UploadFile
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
from app.services.pdf_renderer import render_doc_to_pdf_bytes
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
    background_tasks: BackgroundTasks,
    cleanup: bool = False,
    x_api_key: Optional[str] = Header(None),
):
    """Stream file DWG hasil generate dari output_path.

    Bila cleanup=1, file dihapus dari disk generator SETELAH selesai
    di-stream (via BackgroundTasks). Dipakai caller yang mengunduh sekali
    lalu menyimpan sendiri (mis. Laravel → upload ke Google Drive), supaya
    output_path tak menumpuk file DWG lama.
    """
    verify_api_key(x_api_key)
    from urllib.parse import unquote
    decoded = unquote(filename)
    settings = get_settings()
    file_path = Path(settings.output_path) / decoded
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {decoded}")

    if cleanup:
        # Hapus HANYA setelah response terkirim penuh. Path di-resolve dan
        # dipastikan berada di dalam output_path untuk mencegah path traversal.
        output_root = Path(settings.output_path).resolve()
        resolved = file_path.resolve()
        if output_root in resolved.parents:
            background_tasks.add_task(_safe_unlink, resolved)

    return FileResponse(
        path=str(file_path),
        filename=decoded,
        media_type="application/octet-stream",
        background=background_tasks,
    )


def _safe_unlink(path: Path) -> None:
    """Hapus file, abaikan error (mis. sudah terhapus / race)."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


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


def _logo_overlays_from_doc(doc, settings, module: str):
    """Build logo overlays (mm coords + PNG paths) from a *rendered doc's own*
    OLE2FRAME entities. Manual AutoCAD files carry their own kop/logo frames at
    positions the drafter chose — those must be used, not the system template's,
    so SVG matches the PDF path (which stamps over the doc's own frames)."""
    try:
        from app.services.pdf_renderer import collect_ole_frames, _resolve_ole_overlays
        from app.services.isometric_service import IsometricService

        svc = get_isometric_service(module=module)
        logo_dir = svc._pdf_logo_dir()

        frames = collect_ole_frames(doc)
        if not frames:
            return []
        overlays = _resolve_ole_overlays(logo_dir, frames)

        result = []
        for f in frames:
            png = overlays.get(f["idx"])
            if not png:
                continue
            result.append({
                "png_path": png,
                "x1": f["x1"], "x2": f["x2"], "y1": f["y1"], "y2": f["y2"],
            })
        return result
    except Exception:
        return []


def _logo_overlays_from_template(settings, module: str):
    """Build logo overlays (mm coords + PNG paths) from a module template's
    OLE2FRAME entities, so an uploaded DWG/DXF gets the same logos at the same
    positions as the editor/PDF. Returns [] if template or logos are missing.
    """
    try:
        from app.services.isometric_service import IsometricService
        from app.services.pdf_renderer import collect_ole_frames, _resolve_ole_overlays

        template = (
            getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
            if module == "SK"
            else getattr(settings, "isometric_template_path", None) or settings.template_path
        )
        tpl_path = Path(template)
        if not tpl_path.exists():
            return []

        svc = IsometricService(
            template_path=str(tpl_path),
            output_dir=settings.output_path,
            thumbnails_dir=getattr(settings, "thumbnails_path", "thumbnails"),
            oda_path=settings.oda_path,
            dwg_version=settings.dwg_version,
        )
        logo_dir = svc._pdf_logo_dir()

        tpl_doc = ezdxf.readfile(str(tpl_path))
        frames = collect_ole_frames(tpl_doc)
        if not frames:
            return []
        overlays = _resolve_ole_overlays(logo_dir, frames)

        result = []
        for f in frames:
            png = overlays.get(f["idx"])
            if not png:
                continue
            result.append({
                "png_path": png,
                "x1": f["x1"], "x2": f["x2"], "y1": f["y1"], "y2": f["y2"],
            })
        return result
    except Exception:
        return []


@router.post("/render-file-svg")
async def render_file_svg(
    file: UploadFile = File(..., description="A .dxf or .dwg file to render"),
    module: str = "SK",
    x_api_key: Optional[str] = Header(None),
):
    """Render an uploaded DXF/DWG file to SVG, with module logos overlaid.

    DXF is read directly by ezdxf. DWG is converted to DXF via ODA first
    (requires ODA on Linux). Logos are stamped at the same OLE-frame positions
    as the editor/PDF, read from the module template. Used for on-demand
    evidence preview; callers should cache the returned SVG.
    """
    verify_api_key(x_api_key)

    filename = (file.filename or "").lower()
    ext = Path(filename).suffix
    if ext not in (".dxf", ".dwg"):
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext or 'unknown'}")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    settings = get_settings()
    module = (module or "SK").upper()

    def _render() -> str:
        with tempfile.TemporaryDirectory() as tmp_dir:
            src_path = Path(tmp_dir) / f"upload{ext}"
            src_path.write_bytes(raw)

            if ext == ".dwg":
                dxf_svc = DxfService(
                    template_path=settings.template_path,
                    output_path=tmp_dir,
                    oda_path=settings.oda_path,
                    dwg_version=settings.dwg_version,
                )
                ok, msg, dxf_path = dxf_svc.convert_to_dxf(src_path, output_dir=Path(tmp_dir))
                if not ok or not dxf_path:
                    raise ValueError(msg)
                doc = ezdxf.readfile(str(dxf_path))
            else:
                doc = ezdxf.readfile(str(src_path))

            # Prefer the uploaded file's OWN OLE frames (manual AutoCAD kop
            # positions); fall back to template positions only if it has none.
            logo_overlays = _logo_overlays_from_doc(doc, settings, module)
            if not logo_overlays:
                logo_overlays = _logo_overlays_from_template(settings, module)
            return render_dxf_to_svg(
                doc,
                font_dir=_resolve_font_dir(settings),
                logo_overlays=logo_overlays,
            )

    sem = _get_semaphore()
    async with sem:
        try:
            svg = await asyncio.to_thread(_render)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Render error: {str(e)}")

    return Response(content=svg, media_type="image/svg+xml")


@router.post("/render-file-pdf")
async def render_file_pdf(
    file: UploadFile = File(..., description="A .dxf or .dwg file to render"),
    module: str = "SK",
    x_api_key: Optional[str] = Header(None),
):
    """Render an uploaded DXF/DWG file to PDF (DWG → DXF via ODA → PDF).

    Untuk asbuilt yang tak punya config drawing sistem (mis. DWG manual
    AutoCAD): file DWG-nya dikonversi langsung jadi PDF. Logo/kop OLE bawaan
    file ikut ter-stamp. Callers dapat men-cache PDF hasilnya.
    """
    verify_api_key(x_api_key)

    filename = (file.filename or "").lower()
    ext = Path(filename).suffix
    if ext not in (".dxf", ".dwg"):
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext or 'unknown'}")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    settings = get_settings()
    module = (module or "SK").upper()

    def _render() -> bytes:
        with tempfile.TemporaryDirectory() as tmp_dir:
            src_path = Path(tmp_dir) / f"upload{ext}"
            src_path.write_bytes(raw)

            if ext == ".dwg":
                dxf_svc = DxfService(
                    template_path=settings.template_path,
                    output_path=tmp_dir,
                    oda_path=settings.oda_path,
                    dwg_version=settings.dwg_version,
                )
                ok, msg, dxf_path = dxf_svc.convert_to_dxf(src_path, output_dir=Path(tmp_dir))
                if not ok or not dxf_path:
                    raise ValueError(msg)
                doc = ezdxf.readfile(str(dxf_path))
            else:
                doc = ezdxf.readfile(str(src_path))

            svc = get_isometric_service(module=module)
            return render_doc_to_pdf_bytes(
                doc,
                font_dir=_resolve_font_dir(settings),
                logo_dir=svc._pdf_logo_dir(),
                layout_name=module,
            )

    sem = _get_semaphore()
    async with sem:
        try:
            pdf_bytes = await asyncio.to_thread(_render)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Render error: {str(e)}")

    return Response(content=pdf_bytes, media_type="application/pdf")


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


# -- Bulk FILE PDF (render DWG/DXF files → individual PDFs zipped) ------------

@router.post("/bulk-file-pdf")
async def bulk_generate_file_pdf(
    files: List[UploadFile] = File(..., description="DWG/DXF files to render"),
    meta: str = Form("[]", description="JSON list [{reff_id, folder}] parallel to files"),
    module: str = Form("SK"),
    file_name: Optional[str] = Form(None),
    merge: bool = Form(False),
    x_api_key: Optional[str] = Header(None),
):
    """Start bulk job that renders uploaded DWG/DXF files → PDF.

    Untuk asbuilt dari file (tanpa config drawing sistem), mis. DWG manual.
    merge=False → tiap file jadi PDF, di-ZIP (default).
    merge=True  → semua digabung jadi SATU PDF (mode "PDF Gabungan").
    Returns job_id immediately; poll /bulk-file-pdf-status/{job_id}.
    """
    import datetime
    import json as _json

    verify_api_key(x_api_key)

    try:
        meta_list = _json.loads(meta) if meta else []
    except Exception:
        meta_list = []

    payloads = []
    for i, uf in enumerate(files):
        raw = await uf.read()
        m = meta_list[i] if i < len(meta_list) else {}
        payloads.append({
            "filename": uf.filename or f"upload_{i}.dwg",
            "bytes": raw,
            "reff_id": m.get("reff_id") or f"file_{i}",
            "folder": m.get("folder"),
        })

    count = len(payloads)
    module = (module or "SK").upper()
    date_str = datetime.datetime.now().strftime("%d-%m-%Y")
    fname = file_name or f"{module}_DWG_{count}_{date_str}"
    job_id = _uuid.uuid4().hex
    store = _get_job_store()
    service = get_isometric_service(module=module)

    store.create(job_id, {"status": "running", "done": 0, "total": count,
                          "file_name": fname, "download_url": None, "error": None})

    _progress, _is_cancelled = _make_bulk_callbacks(store, job_id)

    async def _run():
        success, message, out_path = await asyncio.to_thread(
            service.generate_bulk_file_pdf, payloads, fname, _progress, _is_cancelled, merge
        )
        if store.status(job_id) == "cancelled":
            return
        if success and out_path:
            store.update(job_id, status="done", done=count,
                         download_url=f"/api/isometric/download/{out_path.name}")
        else:
            store.update(job_id, status="error", error=message)

    asyncio.ensure_future(_run())
    return {"job_id": job_id, "total": count, "file_name": fname}


@router.get("/bulk-file-pdf-status/{job_id}")
async def bulk_file_pdf_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    job = _get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/bulk-file-pdf/{job_id}")
async def cancel_bulk_file_pdf(job_id: str, x_api_key: Optional[str] = Header(None)):
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
