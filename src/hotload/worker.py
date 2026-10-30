"""Arq worker. Run with: arq hotload.worker.WorkerSettings"""

import asyncio
import re
from functools import partial

from arq import cron
from arq.connections import RedisSettings

from . import engine, storage
from .config import get_settings
from .jobs import JobStore


async def run_download(ctx: dict, job_id: str) -> None:
    settings = get_settings()
    store: JobStore = ctx["store"]
    job = await store.get(job_id)
    if job is None:
        return
    job.state, job.message = "running", "Starting"
    await store.save(job)
    loop = asyncio.get_running_loop()
    last = {"t": 0.0}

    def on_progress(d: dict) -> None:  # runs in the download thread
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if not total:
            return
        pct = round(100 * d.get("downloaded_bytes", 0) / total, 1)
        if pct - last["t"] >= 2:
            last["t"] = pct
            job.progress = pct
            asyncio.run_coroutine_threadsafe(store.save(job), loop)

    try:
        path = await asyncio.to_thread(
            partial(
                engine.download, job.url, settings, storage.job_dir(settings.output_dir, job.id),
                engine.PRESETS[job.preset], start=job.start, end=job.end,
                subtitles=job.subtitles, block_adult=settings.require_age_gate and not job.age_confirmed,
                progress=on_progress,
            )
        )
        job.state, job.progress, job.message = "done", 100.0, "Ready"
        job.path = str(path)
        job.filename = re.sub(r"[^\w .()\[\]-]", "_", path.name)
    except engine.EngineError as exc:
        job.state, job.message = "error", str(exc)
    except Exception:  # noqa: BLE001 - never leak internals to the client
        job.state, job.message = "error", "Unexpected error while downloading"
    await store.save(job)


async def cleanup(ctx: dict) -> None:
    s = get_settings()
    storage.cleanup_expired(s.output_dir, s.file_ttl_seconds)


async def startup(ctx: dict) -> None:
    from redis.asyncio import Redis

    s = get_settings()
    s.output_dir.mkdir(parents=True, exist_ok=True)
    ctx["redis_client"] = Redis.from_url(s.redis_url, decode_responses=True)
    ctx["store"] = JobStore(ctx["redis_client"], s.job_ttl_seconds)


async def shutdown(ctx: dict) -> None:
    await ctx["redis_client"].aclose()


class WorkerSettings:
    functions = [run_download]
    cron_jobs = [cron(cleanup, minute={0, 10, 20, 30, 40, 50})]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
    job_timeout = 1800
