"""Isometric drawing API endpoints. New dynamic engine, separate from legacy /api/dxf."""
import asyncio
import uuid as _uuid
from pathlib import Path
from typing import List, Optional

import tempfile

from fastapi import APIRouter, BackgroundTasks, Body, File, Form, HTTPException, Header, UploadFile
from fastapi.responses import FileResponse, Response

from app.config import get_settings, turunkan_path_per_project
from app.schemas.isometric_schema import (
    BlockListResponse,
    IsometricGenerateRequest,
    IsometricGenerateResponse,
    VariantDirectionsResponse,
)
from app.services.dxf_service import DxfService
from app.services.dxf_to_svg import render_dxf_to_svg
from app.services.pdf_renderer import render_doc_to_pdf_bytes, collect_ole_frames
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


def get_isometric_service(module: str = "SR", project_id: Optional[int] = None) -> IsometricService:
    """project_id opsional — 1 server fisik BISA melayani lebih dari 1
    project_id (mis. Batang+Kendal+Wajo di 1 mesin), jadi base template dari
    .env TIDAK CUKUP kalau region itu sudah upload kop sendiri di halaman
    As Built. Path turunan (_p{project_id}) di-cek dulu, fallback ke base
    path kalau region itu belum pernah upload (atau project_id None =
    default nasional, perilaku SAMA seperti sebelum project_id ada)."""
    settings = get_settings()
    if module == "SK":
        template = getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
    else:
        template = getattr(settings, "isometric_template_path", None) or settings.template_path

    template_path = turunkan_path_per_project(template, project_id)
    if not template_path.exists():
        template_path = Path(template)

    thumbnails = getattr(settings, "thumbnails_path", "thumbnails")
    return IsometricService(
        template_path=str(template_path),
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
    service = get_isometric_service(module=request.module, project_id=request.project_id)
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


@router.get("/placeholder-offsets-default")
async def placeholder_offsets_default(x_api_key: Optional[str] = Header(None)):
    """Read-only: expose PLACEHOLDER_OFFSETS hardcode (pdf_template_cache.py)
    supaya Laravel bisa tampilkan nilai BASELINE di form admin sebelum region
    pernah override apapun — menghindari duplikasi sumber kebenaran (nilai
    ini SATU-SATUNYA yang dipakai render PDF kalau region tidak override)."""
    verify_api_key(x_api_key)
    from app.services.pdf_template_cache import PLACEHOLDER_OFFSETS
    return {"offsets": {k: list(v) for k, v in PLACEHOLDER_OFFSETS.items()}}


@router.post("/static-map")
async def static_map(payload: dict = Body(..., description="lat, lng, zoom (opsional), width/height (opsional)"),
                     x_api_key: Optional[str] = Header(None)):
    """Render peta lokasi statis (PNG, stitch tile OpenStreetMap) dari
    koordinat lat/long — dipakai fitur "peta di kop dokumen asbuilt".
    Laravel panggil ini lalu sisipkan hasilnya sebagai entry tambahan di
    array logo_overlays yang SUDAH ADA (lihat static_map.py untuk detail
    cache tile+composite)."""
    verify_api_key(x_api_key)
    lat = payload.get("lat")
    lng = payload.get("lng")
    if lat is None or lng is None:
        raise HTTPException(status_code=400, detail="lat/lng wajib diisi")
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="lat/lng harus angka")

    zoom = int(payload.get("zoom", 19))
    # width/height DIBATASI biar mismatch rasio (stretch tidak proporsional
    # di OLE frame) tidak jadi alasan minta ukuran ekstrem — Laravel kirim
    # rasio ASLI bounding box OLE frame, skala px/mm konstan (lihat
    # IsometricDrawingService::renderStaticMap()).
    width = max(80, min(2000, int(payload.get("width", 640))))
    height = max(80, min(2000, int(payload.get("height", 480))))
    label = payload.get("label")
    if label is not None:
        label = str(label).strip()[:60] or None  # potong label kepanjangan, kosong -> None
    module = payload.get("module")

    settings = get_settings()

    def _render() -> bytes:
        from app.services.static_map import render_static_map_cached
        return render_static_map_cached(
            lat, lng, zoom, width, height,
            output_dir=Path(settings.output_path),
            label=label, module=module,
        )

    try:
        png_bytes = await asyncio.to_thread(_render)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Render peta gagal: {str(e)}")

    import base64
    return {"png_base64": base64.b64encode(png_bytes).decode()}


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
    data: dict = Body(..., description="Customer data for text replacement + optional logo_overlays"),
    x_api_key: Optional[str] = Header(None),
):
    """Render template dengan text replacement customer data → SVG. Supports
    module=SR|SK in body. Dipakai DrawingController::loadDrawing()/loadDrawingSk()
    saat pertama buka customer — beda dari preview-drawing-svg (dipakai live-edit
    kanvas). logo_overlays (opsional) — sama format {idx, png_base64, x1, y1,
    x2, y2} yang dipakai asbuilt_dxf_preview()/preview-drawing-svg."""
    verify_api_key(x_api_key)
    module = data.pop("module", "SR")
    project_id = data.pop("project_id", None)
    logo_overlays_in = data.pop("logo_overlays", None) or []
    settings = get_settings()
    base_template = (
        getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
        if module == "SK"
        else getattr(settings, "isometric_template_path", None) or settings.template_path
    )
    template_path = turunkan_path_per_project(base_template, project_id)
    if not template_path.exists():
        template_path = Path(base_template)

    def _render(tmp_dir: str):
        import base64
        from app.services.pdf_renderer import collect_ole_frames
        dxf_svc = DxfService(
            template_path=str(template_path),
            output_path=settings.output_path,
            oda_path=settings.oda_path,
            dwg_version=settings.dwg_version,
        )
        replacements = dxf_svc.prepare_data(data)
        doc = ezdxf.readfile(str(template_path))
        dxf_svc.process_modelspace(doc.modelspace(), replacements)
        dxf_svc.process_blocks(doc, replacements)

        frames_by_idx = {f["idx"]: f for f in collect_ole_frames(doc)} if logo_overlays_in else {}

        logo_overlays = []
        for ov in logo_overlays_in:
            b64 = ov.get("png_base64")
            if not b64:
                continue
            # Posisi CUSTOM kalau ada, fallback ke bounding box OLE2FRAME asli
            # kalau logo baru diupload (belum pernah digeser/resize) — SAMA
            # pola asbuilt_dxf_preview(), sebelumnya tidak ada di sini jadi
            # logo yang belum pernah di-drag di-skip total, tidak pernah muncul.
            if all(k in ov for k in ("x1", "y1", "x2", "y2")):
                pos = ov
            else:
                pos = frames_by_idx.get(ov.get("idx"))
                if not pos:
                    continue
            try:
                png_bytes = base64.b64decode(b64)
            except Exception:
                continue
            png_path = Path(tmp_dir) / f"logo_{ov.get('idx', 0)}.png"
            png_path.write_bytes(png_bytes)
            logo_overlays.append({
                "png_path": str(png_path),
                "x1": pos["x1"], "y1": pos["y1"], "x2": pos["x2"], "y2": pos["y2"],
            })

        return render_dxf_to_svg(doc, font_dir=_resolve_font_dir(settings), logo_overlays=logo_overlays)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            svg = await asyncio.to_thread(_render, tmp_dir)
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
    payload: dict = Body(..., description="Drawing config + optional customer_data + optional logo_overlays"),
    x_api_key: Optional[str] = Header(None),
):
    """Live preview: generate drawing in-memory + text replace + render SVG.

    logo_overlays (opsional) — logo PNG per-region dari As Built settings
    (Laravel resolve, kirim base64), sama format {idx, png_base64, x1, y1,
    x2, y2} yang dipakai asbuilt_dxf_preview() — didekode ke file temp di
    sini (bukan di engine) supaya generate_svg_preview() tetap murni terima
    path, konsisten dengan render_dxf_to_svg()."""
    verify_api_key(x_api_key)
    module = payload.get("module", "SR")
    project_id = payload.pop("project_id", None)
    service = get_isometric_service(module=module, project_id=project_id)
    customer_data = payload.pop("customer_data", None)
    logo_overlays_in = payload.pop("logo_overlays", None) or []

    def _render(tmp_dir: str):
        import base64
        # Fallback ke posisi OLE2FRAME asli (kalau logo belum pernah
        # digeser/resize) sekarang ditangani generate_svg_preview() sendiri
        # (butuh akses ke doc yang di-build di dalamnya) — di sini cukup
        # decode PNG ke temp file + teruskan idx+posisi APA ADANYA (posisi
        # opsional, tidak di-skip walau belum lengkap).
        logo_overlays = []
        for ov in logo_overlays_in:
            b64 = ov.get("png_base64")
            if not b64:
                continue
            try:
                png_bytes = base64.b64decode(b64)
            except Exception:
                continue
            png_path = Path(tmp_dir) / f"logo_{ov.get('idx', 0)}.png"
            png_path.write_bytes(png_bytes)
            entry = {"png_path": str(png_path), "idx": ov.get("idx")}
            if all(k in ov for k in ("x1", "y1", "x2", "y2")):
                entry.update(x1=ov["x1"], y1=ov["y1"], x2=ov["x2"], y2=ov["y2"])
            logo_overlays.append(entry)

        success, result = service.engine.generate_svg_preview(
            payload, customer_data, font_dir=service._pdf_font_dir(), logo_overlays=logo_overlays
        )
        if not success:
            raise ValueError(result)
        return result

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await asyncio.to_thread(_render, tmp_dir)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return Response(content=result, media_type="image/svg+xml")


@router.post("/preview-drawing-pdf")
async def preview_drawing_pdf(
    payload: dict = Body(..., description="Drawing config + optional customer_data + optional logo_overlays"),
    x_api_key: Optional[str] = Header(None),
):
    """Live preview as PDF using the production renderer.

    logo_overlays (opsional) — logo PNG per-region, base64 langsung (TIDAK
    perlu file temp — compose_customer_pdf/PyMuPDF insert_image terima bytes
    langsung). Diteruskan ke render_pdf_bytes_cached() yang overlay-nya SETELAH
    skeleton cache (lihat komentar di sana) — fallback path di bawah TIDAK
    menerima logo_overlays, tetap pakai _pdf_logo_dir() global seperti biasa."""
    verify_api_key(x_api_key)
    module = payload.get("module", "SR")
    project_id = payload.pop("project_id", None)
    service = get_isometric_service(module=module, project_id=project_id)
    customer_data = payload.pop("customer_data", None)
    logo_overlays_in = payload.pop("logo_overlays", None) or []

    def _resolve_logo_positions():
        # compose_customer_pdf() TIDAK punya akses ke ezdxf doc (skeleton
        # sudah jadi bytes PDF), jadi fallback ke OLE2FRAME asli HARUS
        # diresolve di sini, dari template file langsung — posisi OLE
        # frame kop sama persis baik di template polos maupun hasil
        # generate (geometri pipa tidak menggeser kop). Tanpa ini, logo
        # yang belum pernah digeser/resize di-skip total di compose_
        # customer_pdf() (exception KeyError x1/y1/x2/y2, di-catch, continue).
        need_fallback = any(
            not all(k in ov for k in ("x1", "y1", "x2", "y2")) for ov in logo_overlays_in
        )
        if not need_fallback:
            return logo_overlays_in
        try:
            frames_by_idx = {f["idx"]: f for f in collect_ole_frames(ezdxf.readfile(str(service.template_path)))}
        except Exception:
            frames_by_idx = {}
        resolved = []
        for ov in logo_overlays_in:
            if all(k in ov for k in ("x1", "y1", "x2", "y2")):
                resolved.append(ov)
                continue
            pos = frames_by_idx.get(ov.get("idx"))
            if not pos:
                continue
            resolved.append({**ov, "x1": pos["x1"], "y1": pos["y1"], "x2": pos["x2"], "y2": pos["y2"]})
        return resolved

    def _render():
        logo_overlays = _resolve_logo_positions()
        try:
            return service.render_pdf_bytes_cached(payload, customer_data, logo_overlays=logo_overlays)
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
    project_id: Optional[int] = None,
    x_api_key: Optional[str] = Header(None),
):
    """Render template kosong — untuk initial canvas view. Supports module=SR|SK.
    project_id opsional (query string) — path per-region, TIDAK terkait
    logo (endpoint ini sengaja tanpa logo, lihat komentar
    preview_blank_svg_dengan_logo di bawah)."""
    verify_api_key(x_api_key)
    settings = get_settings()
    if module == "SK":
        base_template = getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[PADUKUHAN]": "-", "[SEKTOR]": "-",
            "[NO_SK]": "-",
            "[1]": "0", "[2]": "0", "[3]": "0", "[6]": "0", "[7]": "0",
            "[113]": "0", "[114]": "0", "[115]": "0", "[4]": "0",
        }
    else:
        base_template = getattr(settings, "isometric_template_path", None) or settings.template_path
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[PADUKUHAN]": "-", "[SEKTOR]": "-",
            "[NO_MGRT]": "-", "[SN_AWAL]": "-", "[KOORDINAT_TAPPING]": "-",
            "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0", "[21]": "0",
        }

    template_path = turunkan_path_per_project(base_template, project_id)
    if not template_path.exists():
        template_path = Path(base_template)

    def _render():
        dxf_svc = DxfService(
            template_path=str(template_path),
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


@router.post("/preview-blank-svg")
async def preview_blank_svg_dengan_logo(
    payload: dict = Body(..., description="module + optional logo_overlays"),
    x_api_key: Optional[str] = Header(None),
):
    """Sama seperti GET /preview-blank-svg, TAPI terima logo_overlays di body
    (POST, base64 bisa besar, tidak muat di query string). ENDPOINT TERPISAH
    (bukan extend GET yang sudah ada) — GET dipakai AsbuiltDxfTemplateController
    sebagai fallback eksplisit "tidak ada logo tersimpan", tidak boleh diubah
    perilakunya. POST ini dipakai DrawingController untuk kanvas kosong awal
    (/drawing, sebelum pilih pelanggan) supaya logo per-region tetap konsisten
    tampil dari saat halaman pertama dibuka."""
    verify_api_key(x_api_key)
    module = (payload.get("module") or "SR").upper()
    project_id = payload.get("project_id")
    logo_overlays_in = payload.get("logo_overlays") or []
    tanggal_override = payload.get("tanggal")
    material_kosong = payload.get("default_material_kosong")
    settings = get_settings()
    if module == "SK":
        base_template = getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[PADUKUHAN]": "-", "[SEKTOR]": "-",
            "[NO_SK]": "-",
            "[1]": "0", "[2]": "0", "[3]": "0", "[6]": "0", "[7]": "0",
            "[113]": "0", "[114]": "0", "[115]": "0", "[4]": "0",
        }
    else:
        base_template = getattr(settings, "isometric_template_path", None) or settings.template_path
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[PADUKUHAN]": "-", "[SEKTOR]": "-",
            "[NO_MGRT]": "-", "[SN_AWAL]": "-", "[KOORDINAT_TAPPING]": "-",
            "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0", "[21]": "0",
        }

    # Preview kanvas kosong (sebelum CALPEL dipilih) — tanggal & default
    # material kosong ikut config per-region (AsbuiltDxfTemplate), bukan
    # placeholder "-"/"0" generik, supaya admin lihat hasil MIRIP generate
    # sungguhan. Placeholder lain ([NAMA], [ALAMAT], dst) TETAP "-" — itu
    # data transaksional customer yang memang belum ada.
    if tanggal_override:
        blank_replacements["[TANGGAL]"] = tanggal_override
    if material_kosong is not None:
        for key in blank_replacements:
            if key not in ("[TANGGAL]", "[REFF_ID]", "[NAMA]", "[ALAMAT]", "[RT]", "[RW]",
                           "[KELURAHAN]", "[PADUKUHAN]", "[SEKTOR]", "[NO_SK]",
                           "[NO_MGRT]", "[SN_AWAL]", "[KOORDINAT_TAPPING]"):
                blank_replacements[key] = material_kosong

    template_path = turunkan_path_per_project(base_template, project_id)
    if not template_path.exists():
        template_path = Path(base_template)

    def _render(tmp_dir: str):
        import base64
        from app.services.pdf_renderer import collect_ole_frames
        dxf_svc = DxfService(
            template_path=str(template_path),
            output_path=settings.output_path,
            oda_path=settings.oda_path,
            dwg_version=settings.dwg_version,
        )
        doc = ezdxf.readfile(str(template_path))
        dxf_svc.process_modelspace(doc.modelspace(), blank_replacements)
        dxf_svc.process_blocks(doc, blank_replacements)

        frames_by_idx = {f["idx"]: f for f in collect_ole_frames(doc)} if logo_overlays_in else {}

        logo_overlays = []
        for ov in logo_overlays_in:
            b64 = ov.get("png_base64")
            if not b64:
                continue
            # Fallback ke posisi OLE2FRAME asli kalau logo belum pernah
            # digeser/resize — SAMA pola asbuilt_dxf_preview()/preview_svg().
            if all(k in ov for k in ("x1", "y1", "x2", "y2")):
                pos = ov
            else:
                pos = frames_by_idx.get(ov.get("idx"))
                if not pos:
                    continue
            try:
                png_bytes = base64.b64decode(b64)
            except Exception:
                continue
            png_path = Path(tmp_dir) / f"logo_{ov.get('idx', 0)}.png"
            png_path.write_bytes(png_bytes)
            logo_overlays.append({
                "png_path": str(png_path),
                "x1": pos["x1"], "y1": pos["y1"], "x2": pos["x2"], "y2": pos["y2"],
            })

        return render_dxf_to_svg(doc, font_dir=_resolve_font_dir(settings), logo_overlays=logo_overlays)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            svg = await asyncio.to_thread(_render, tmp_dir)
        return Response(content=svg, media_type="image/svg+xml")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview error: {str(e)}")


def _resolve_asbuilt_template_path(settings, module: str, project_id: Optional[int] = None) -> Path:
    """Path template yang SAMA dipakai preview_blank_svg/generate As Built —
    Laravel me-materialize file .dxf per region ke path override ini (lihat
    InternalAsbuiltSyncController), jadi endpoint OLE-frame/logo di bawah
    HARUS baca dari path yang sama, bukan template default hardcode.

    project_id opsional — 1 server fisik bisa melayani >1 project_id (mis.
    Batang+Kendal+Wajo di 1 mesin), jadi path turunan (_p{project_id}) dicek
    dulu, fallback ke base path kalau region itu belum pernah upload sendiri."""
    if module == "SK":
        base = getattr(settings, "sk_isometric_template_path", None) or "templates/SK_POLOS.dxf"
    else:
        base = getattr(settings, "isometric_template_path", None) or settings.template_path

    template_path = turunkan_path_per_project(base, project_id)
    if not template_path.exists():
        template_path = Path(base)
    return template_path


@router.get("/asbuilt-dxf/ole-frames")
async def asbuilt_dxf_ole_frames(
    module: str = "SK",
    project_id: Optional[int] = None,
    x_api_key: Optional[str] = Header(None),
):
    """Deteksi jumlah + posisi OLE2FRAME (slot logo/kop) di template As Built
    yang SEDANG dipakai server ini — dipakai Laravel untuk tahu berapa slot
    upload logo yang perlu ditampilkan (bukan angka hardcode). Metadata posisi
    saja, TANPA gambar."""
    verify_api_key(x_api_key)
    settings = get_settings()
    module = (module or "SK").upper()
    template_path = _resolve_asbuilt_template_path(settings, module, project_id)

    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template {module} belum tersedia di server ini.")

    def _baca():
        from app.services.pdf_renderer import collect_ole_frames
        doc = ezdxf.readfile(str(template_path))
        return collect_ole_frames(doc)

    try:
        frames = await asyncio.to_thread(_baca)
        return {"module": module, "frames": [
            {"idx": f["idx"], "x1": f["x1"], "y1": f["y1"], "x2": f["x2"], "y2": f["y2"]}
            for f in frames
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal membaca OLE frame: {str(e)}")


@router.post("/asbuilt-dxf/preview")
async def asbuilt_dxf_preview(
    payload: dict = Body(..., description="module + logo_overlays (base64 PNG per idx) + replacements (override nilai placeholder opsional)"),
    x_api_key: Optional[str] = Header(None),
):
    """Sama seperti preview-blank-svg, TAPI dengan logo PNG di-overlay di
    posisi OLE2FRAME yang sesuai — logo_overlays dikirim per-region dari
    Laravel (kolom asbuilt_dxf_templates.logo_overlays), BUKAN dari folder
    PDF_LOGO_DIR global. Base64 ditulis ke file temp karena
    _build_logo_image_tags() butuh path file, bukan bytes langsung (sama pola
    yang sudah dipakai render_file_svg untuk upload sekali-pakai).

    `replacements` opsional ({"[NAMA]": "Budi", "[19]": "3", ...}) — admin
    isi contoh nilai di editor untuk lihat hasil cetak sungguhan, BUKAN
    disimpan permanen (murni preview sekali render, sama seperti logo)."""
    verify_api_key(x_api_key)
    settings = get_settings()
    module = (payload.get("module") or "SK").upper()
    project_id = payload.get("project_id")
    logo_overlays_in = payload.get("logo_overlays") or []
    replacements_override = payload.get("replacements") or {}
    template_path = _resolve_asbuilt_template_path(settings, module, project_id)

    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template {module} belum tersedia di server ini.")

    if module == "SK":
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[PADUKUHAN]": "-", "[SEKTOR]": "-",
            "[NO_SK]": "-",
            "[1]": "0", "[2]": "0", "[3]": "0", "[6]": "0", "[7]": "0",
            "[113]": "0", "[114]": "0", "[115]": "0", "[4]": "0",
        }
    else:
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[PADUKUHAN]": "-", "[SEKTOR]": "-",
            "[NO_MGRT]": "-", "[SN_AWAL]": "-", "[KOORDINAT_TAPPING]": "-",
            "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0", "[21]": "0",
        }

    # Cuma key yang DIKENAL (ada di blank_replacements) yang boleh di-override
    # — mencegah admin menyuntik placeholder [xxx] arbitrer yang tidak pernah
    # ada di template (tidak berbahaya, tapi tidak ada gunanya juga).
    for key, val in replacements_override.items():
        if key in blank_replacements and val:
            blank_replacements[key] = str(val)

    def _render(tmp_dir: str) -> str:
        import base64
        from app.services.pdf_renderer import collect_ole_frames

        dxf_svc = DxfService(
            template_path=str(template_path),
            output_path=settings.output_path,
            oda_path=settings.oda_path,
            dwg_version=settings.dwg_version,
        )
        doc = ezdxf.readfile(str(template_path))
        dxf_svc.process_modelspace(doc.modelspace(), blank_replacements)
        dxf_svc.process_blocks(doc, blank_replacements)

        frames = collect_ole_frames(doc)
        frames_by_idx = {f["idx"]: f for f in frames}

        logo_overlays = []
        for ov in logo_overlays_in:
            idx = ov.get("idx")
            b64 = ov.get("png_base64")
            if not b64:
                continue

            # Posisi CUSTOM (admin geser/resize di editor) kalau ada,
            # fallback ke bounding box OLE2FRAME asli dari file kalau belum
            # pernah diatur — backward compatible dengan data logo_overlays
            # yang cuma punya {idx, png_base64} (belum ada x1/y1/x2/y2).
            if all(k in ov for k in ("x1", "y1", "x2", "y2")):
                pos = ov
            else:
                pos = frames_by_idx.get(idx)
                if not pos:
                    continue

            try:
                png_bytes = base64.b64decode(b64)
            except Exception:
                continue
            png_path = Path(tmp_dir) / f"logo_{idx}.png"
            png_path.write_bytes(png_bytes)
            logo_overlays.append({
                "png_path": str(png_path),
                "x1": pos["x1"], "x2": pos["x2"], "y1": pos["y1"], "y2": pos["y2"],
            })

        return render_dxf_to_svg(doc, font_dir=_resolve_font_dir(settings), logo_overlays=logo_overlays)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            svg = await asyncio.to_thread(_render, tmp_dir)
        return Response(content=svg, media_type="image/svg+xml")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview error: {str(e)}")


@router.post("/asbuilt-dxf/preview-pdf")
async def asbuilt_dxf_preview_pdf(
    payload: dict = Body(..., description="module + logo_overlays + replacements + pdf_offsets (opsional)"),
    x_api_key: Optional[str] = Header(None),
):
    """PDF counterpart dari /asbuilt-dxf/preview (SVG) — dipakai tab "PDF" di
    halaman admin supaya offset per-placeholder (PLACEHOLDER_OFFSETS /
    AsbuiltPdfPlaceholderOffset) bisa diverifikasi visual TANPA generate
    customer sungguhan.

    Skeleton (kop KOSONG, tanpa placeholder text) DI-CACHE di disk — sama
    folder/mekanisme dengan render_pdf_bytes_cached() (drawing customer
    sungguhan), tapi key pakai start_block="asbuilt-preview" + segments=[]
    supaya TIDAK PERNAH bentrok dengan cache drawing customer asli. Cache
    key sudah termasuk _get_template_hash() yang mtime-aware — kop diupdate
    (mtime file berubah) otomatis bikin key baru, cache lama otomatis tidak
    terpakai lagi, TANPA perlu invalidasi manual.

    Alur SAMA seperti render_pdf_bytes_cached() versi cache-miss:
    extract_placeholder_entities() SEBELUM replace teks (butuh entity yang
    MASIH mengandung "[KEY]" literal), lalu replace, render skeleton tanpa
    placeholder, overlay teks+logo via compose_customer_pdf() (yang menerapkan
    custom_offsets)."""
    verify_api_key(x_api_key)
    settings = get_settings()
    module = (payload.get("module") or "SK").upper()
    project_id = payload.get("project_id")
    logo_overlays_in = payload.get("logo_overlays") or []
    replacements_override = payload.get("replacements") or {}
    pdf_offsets = payload.get("pdf_offsets") or {}
    pdf_font_scale = payload.get("pdf_font_scale") or {}
    template_path = _resolve_asbuilt_template_path(settings, module, project_id)

    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template {module} belum tersedia di server ini.")

    if module == "SK":
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[PADUKUHAN]": "-", "[SEKTOR]": "-",
            "[NO_SK]": "-",
            "[1]": "0", "[2]": "0", "[3]": "0", "[6]": "0", "[7]": "0",
            "[113]": "0", "[114]": "0", "[115]": "0", "[4]": "0",
        }
    else:
        blank_replacements = {
            "[TANGGAL]": "-", "[REFF_ID]": "-", "[NAMA]": "-", "[ALAMAT]": "-",
            "[RT]": "-", "[RW]": "-", "[KELURAHAN]": "-", "[PADUKUHAN]": "-", "[SEKTOR]": "-",
            "[NO_MGRT]": "-", "[SN_AWAL]": "-", "[KOORDINAT_TAPPING]": "-",
            "[19]": "0", "[10]": "0", "[8]": "0", "[7]": "0", "[21]": "0",
        }

    for key, val in replacements_override.items():
        if key in blank_replacements and val:
            blank_replacements[key] = str(val)

    def _render() -> bytes:
        from app.services.pdf_template_cache import (
            extract_placeholder_entities, compose_customer_pdf, _skip_placeholders,
            request_cache_key, load_cache, save_cache,
        )
        from app.services.pdf_renderer import get_page_height_mm

        cache_dir = Path(settings.output_path) / "pdf_cache"
        cache_key = request_cache_key(template_path, "asbuilt-preview", [], [])
        cached = load_cache(cache_dir, cache_key)

        # doc SELALU dibaca (murah — baca file, bukan render) karena
        # collect_ole_frames() di bawah butuh doc TERLEPAS cache hit/miss.
        doc = ezdxf.readfile(str(template_path))
        page_h_mm = get_page_height_mm(doc, layout_name=module)

        if cached is not None:
            skeleton_bytes, placeholders = cached
        else:
            # Posisi DIBACA dan skeleton DI-RENDER SEBELUM teks di-replace —
            # placeholder entity WAJIB masih mengandung literal "[KEY]" untuk
            # KEDUA operasi ini: extract_placeholder_entities() cari entity lewat
            # pola itu, dan _skip_placeholders() (filter_func di bawah) SKIP
            # entity dengan cara yang SAMA (cek PLACEHOLDER_RE.search(text)).
            #
            # BUG YANG PERNAH TERJADI: kalau process_modelspace/process_blocks
            # (replace [KEY] -> value sungguhan) dipanggil DULU, lalu render
            # skeleton SESUDAHNYA — _skip_placeholders() re-cek entity yang
            # TEKSNYA SUDAH BUKAN "[KEY]" LAGI (sudah "Budi Santoso"), regex
            # GAGAL MATCH, entity LOLOS FILTER dan ikut ter-bake ke skeleton di
            # POSISI ASLI TANPA OFFSET — lalu compose_customer_pdf() STAMP LAGI
            # value yang SAMA di posisi BENAR (dengan offset) → teks tampak
            # DOBEL (satu salah tempat, satu benar). Text-replacement TIDAK
            # DIPERLUKAN sama sekali di sini — value diterapkan HANYA lewat
            # compose_customer_pdf(replacements=blank_replacements) di bawah.
            placeholders = extract_placeholder_entities(doc)

            skeleton_bytes = render_doc_to_pdf_bytes(
                doc,
                font_dir=_resolve_font_dir(settings),
                layout_name=module,
                filter_func=_skip_placeholders,
            )
            save_cache(cache_dir, cache_key, skeleton_bytes, placeholders)

        # compose_customer_pdf() terima logo_overlays base64 LANGSUNG
        # ({"png_base64", x1, y1, x2, y2}) — tidak butuh file temp di disk
        # sama sekali (beda dari render_doc_to_pdf_bytes/render_dxf_to_svg
        # yang baca dari logo_dir/png_path).
        frames_by_idx = {f["idx"]: f for f in collect_ole_frames(doc)}
        logo_overlays = []
        for ov in logo_overlays_in:
            idx = ov.get("idx")
            b64 = ov.get("png_base64")
            if not idx or not b64:
                continue
            # Posisi CUSTOM kalau admin pernah geser/resize, fallback ke
            # bounding box OLE2FRAME asli — sama pola endpoint SVG.
            if all(k in ov for k in ("x1", "y1", "x2", "y2")):
                pos = ov
            else:
                pos = frames_by_idx.get(idx)
                if not pos:
                    continue
            logo_overlays.append({
                "png_base64": b64,
                "x1": pos["x1"], "x2": pos["x2"], "y1": pos["y1"], "y2": pos["y2"],
            })

        return compose_customer_pdf(
            skeleton_bytes, placeholders, blank_replacements,
            page_height_mm=page_h_mm,
            font_dir=_resolve_font_dir(settings),
            logo_overlays=logo_overlays,
            custom_offsets=pdf_offsets,
            custom_font_scale=pdf_font_scale,
        )

    try:
        pdf_bytes = await asyncio.to_thread(_render)
        return Response(content=pdf_bytes, media_type="application/pdf")
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
    module     = payload.get("module", "SR")
    payloads   = payload.get("payloads", [])
    project_id = payload.get("project_id")
    service    = get_isometric_service(module=module, project_id=project_id)

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

    items      = payload.get("items", [])
    count      = len(items)
    module     = items[0].get("module", "SR") if items else "SR"
    project_id = payload.get("project_id")
    date_str = datetime.datetime.now().strftime("%d-%m-%Y")
    file_name = payload.get("file_name") or f"{module}_{count}_{date_str}"
    job_id   = _uuid.uuid4().hex
    store    = _get_job_store()
    service  = get_isometric_service(module=module, project_id=project_id)

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

    items      = payload.get("items", [])
    count      = len(items)
    module     = items[0].get("module", "SR") if items else "SR"
    project_id = payload.get("project_id")
    date_str = datetime.datetime.now().strftime("%d-%m-%Y")
    file_name = payload.get("file_name") or f"{module}_{count}_{date_str}"
    job_id   = _uuid.uuid4().hex
    store    = _get_job_store()
    service  = get_isometric_service(module=module, project_id=project_id)

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
    project_id: Optional[int] = Form(None),
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
    service = get_isometric_service(module=module, project_id=project_id)

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

    items      = payload.get("items", [])
    count      = len(items)
    module     = items[0].get("module", "SR") if items else "SR"
    project_id = payload.get("project_id")
    date_str = datetime.datetime.now().strftime("%d-%m-%Y")
    file_name = payload.get("file_name") or f"{module}_{count}_{date_str}"
    job_id   = _uuid.uuid4().hex
    store    = _get_job_store()
    service  = get_isometric_service(module=module, project_id=project_id)

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
