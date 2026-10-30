import shutil
import time
from pathlib import Path


def job_dir(output_dir: Path, job_id: str) -> Path:
    return output_dir / job_id


def cleanup_expired(output_dir: Path, ttl_seconds: int, now: float | None = None) -> int:
    """Delete job directories older than the TTL. Returns the number removed."""
    if not output_dir.exists():
        return 0
    now = now or time.time()
    removed = 0
    for child in output_dir.iterdir():
        if child.is_dir() and now - child.stat().st_mtime > ttl_seconds:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed
