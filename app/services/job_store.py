"""File-based async job state — shared safely across multiple Uvicorn worker processes.

Each job is a single JSON file: {jobs_dir}/{job_id}.json
Writes use atomic rename (os.replace) so a half-written file is never visible.
No in-process locking needed: OS-level atomicity covers concurrent workers.
"""
import json
import os
from pathlib import Path
from typing import Optional


class JobStore:
    def __init__(self, jobs_dir: Path):
        self.dir = Path(jobs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, job_id: str, data: dict) -> None:
        self._atomic_write(job_id, data)

    def get(self, job_id: str) -> Optional[dict]:
        try:
            return json.loads(self._path(job_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def exists(self, job_id: str) -> bool:
        return self._path(job_id).exists()

    def update(self, job_id: str, **fields) -> bool:
        """Merge *fields* into the stored job state. Returns False if job not found."""
        p = self._path(job_id)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        data.update(fields)
        self._atomic_write(job_id, data)
        return True

    def status(self, job_id: str) -> Optional[str]:
        job = self.get(job_id)
        return job.get("status") if job else None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.json"

    def _atomic_write(self, job_id: str, data: dict) -> None:
        p = self._path(job_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)  # atomic on POSIX; effectively atomic on Windows
