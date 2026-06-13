"""Generic PDF utilities — file-to-file merge using PyMuPDF (fitz).

Merges PDF files that already exist on disk (Laravel and this service share
the same filesystem) by linking page objects instead of re-encoding content,
which is dramatically faster than Ghostscript for large batches.
"""
import asyncio
import uuid as _uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException

from app.routes.isometric import _get_job_store, verify_api_key

router = APIRouter(prefix="/api/pdf", tags=["PDF Utilities"])


def _merge_files(file_paths: list[str], output_path: Path, progress_callback=None, cancel_check=None):
    import fitz

    merged = fitz.open()
    failed = 0

    for i, path in enumerate(file_paths):
        if cancel_check and cancel_check():
            merged.close()
            return False, "Cancelled", 0, failed

        try:
            single = fitz.open(path)
            merged.insert_pdf(single)
            single.close()
        except Exception:
            failed += 1
        finally:
            if progress_callback:
                progress_callback(i + 1, len(file_paths))

    total_pages = len(merged)
    if total_pages == 0:
        merged.close()
        return False, "No pages merged", 0, failed

    merged.save(str(output_path))
    merged.close()
    return True, "ok", total_pages, failed


@router.post("/merge-files")
async def merge_pdf_files(payload: dict = Body(...), x_api_key: Optional[str] = Header(None)):
    """Start an async PDF merge job for files already present on disk.

    Body: {"file_paths": ["C:/.../a.pdf", ...], "output_path": "C:/.../merged.pdf"}
    Returns job_id immediately; poll /merge-files-status/{job_id}.
    """
    verify_api_key(x_api_key)

    file_paths = payload.get("file_paths") or []
    output_path_str = payload.get("output_path")

    if not file_paths:
        raise HTTPException(status_code=422, detail="file_paths is required")
    if not output_path_str:
        raise HTTPException(status_code=422, detail="output_path is required")

    for path in file_paths:
        if not Path(path).is_file():
            raise HTTPException(status_code=422, detail=f"File not found: {path}")

    output_path = Path(output_path_str)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    job_id = _uuid.uuid4().hex
    store = _get_job_store()
    store.create(job_id, {
        "status": "running", "done": 0, "total": len(file_paths),
        "output_path": str(output_path), "pages": None, "error": None,
    })

    def _progress(done: int, _total: int):
        store.update(job_id, done=done)

    def _is_cancelled() -> bool:
        return store.status(job_id) == "cancelled"

    async def _run():
        success, message, pages, failed = await asyncio.to_thread(
            _merge_files, file_paths, output_path, _progress, _is_cancelled
        )
        if store.status(job_id) == "cancelled":
            return
        if success:
            store.update(job_id, status="done", done=len(file_paths), pages=pages, failed=failed)
        else:
            store.update(job_id, status="error", error=message)

    asyncio.ensure_future(_run())
    return {"job_id": job_id, "total": len(file_paths)}


@router.get("/merge-files-status/{job_id}")
async def merge_pdf_files_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    job = _get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/merge-files/{job_id}")
async def cancel_merge_pdf_files(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    store = _get_job_store()
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    store.update(job_id, status="cancelled")
    return {"success": True}
