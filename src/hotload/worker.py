"""Arq worker. Run with: arq hotload.worker.WorkerSettings"""

import asyncio
import logging
import re
import time
from functools import partial

import httpx
from arq import Retry, cron
from arq.connections import RedisSettings

from . import canary, engine, logging_setup, metrics, storage, webhooks
from .config import get_settings
from .db import Database
from .jobs import JobStore
from .logging_setup import log, url_ref
from .proxies import NoProxyAvailable, ProxyPool, mask

logger = logging.getLogger("hotload.worker")
_pool: ProxyPool | None = None


def get_pool() -> ProxyPool:
    global _pool
    if _pool is None:
        _pool = ProxyPool.from_settings(get_settings())
    return _pool


async def run_download(ctx: dict, job_id: str) -> None:
    settings = get_settings()
    store: JobStore = ctx["store"]
    job = await store.get(job_id)
    if job is None:
        return
    job.state, job.message = "running", "Starting"
    await store.save(job)
    redis = store.redis
    loop = asyncio.get_running_loop()
    last = {"t": 0.0}
    started = time.monotonic()

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

    pool = get_pool()
    tried: set[str] = set()
    error: engine.EngineError | None = None
    path = None
    attempts = 0
    for attempt in range(1, settings.max_attempts + 1):
        try:
            proxy = pool.select(job.url, exclude=tried)
        except NoProxyAvailable:
            break
        attempts = attempt
        tried.add(proxy or "direct")
        try:
            result = await asyncio.to_thread(
                partial(
                    engine.download, job.url, settings, storage.job_dir(settings.output_dir, job.id),
                    engine.PRESETS[job.preset], proxy=proxy, start=job.start, end=job.end,
                    subtitle_langs=job.subtitle_langs, subtitle_format=job.subtitle_format,
                    subtitle_mode=job.subtitle_mode, subtitle_auto=job.subtitle_auto,
                    audio_lang=job.audio_lang, max_duration=job.max_duration,
                    block_adult=settings.require_age_gate and not job.age_confirmed,
                    progress=on_progress,
                )
            )
            pool.report(proxy, True)
            path, job.title, job.site = result.path, result.title, result.site
            error = None
            break
        except engine.EngineError as exc:
            error = exc
            if exc.retryable:
                pool.report(proxy, False)
                log(logger, "download attempt failed", logging.WARNING, job=job.id, url=url_ref(job.url),
                    attempt=attempt, proxy=mask(proxy), error=str(exc))
                job.message = f"Retrying ({attempt}/{settings.max_attempts})"
                await store.save(job)
                last["t"] = 0.0
                continue
            break
        except Exception:  # noqa: BLE001 - never leak internals to the client
            logger.exception("unexpected download failure", extra={"ctx": {"job": job.id}})
            error = engine.EngineError("Unexpected error while downloading")
            break

    if path is not None and error is None:
        job.state, job.progress, job.message = "done", 100.0, "Ready"
        job.path = str(path)
        job.filename = re.sub(r"[^\w .()\[\]-]", "_", path.name)
    else:
        job.state = "error"
        job.message = str(error) if error else "No working connection was available, try again later"
    await store.save(job)

    if job.user_id:
        try:
            await ctx["db"].finish_history(job)
        except Exception:  # noqa: BLE001 - history is best-effort; never fail a finished download over it
            logger.exception("history update failed", extra={"ctx": {"job": job.id}})

    if job.webhook_url and job.key_id:
        await ctx["redis"].enqueue_job("deliver_webhook", job.id)

    elapsed = time.monotonic() - started
    await metrics.inc(redis, "hotload_jobs_total", state=job.state, preset=job.preset, adult=str(job.adult).lower())
    await metrics.observe(redis, "hotload_job_seconds", elapsed, state=job.state)
    if job.state == "error":
        await metrics.inc(redis, "hotload_job_errors_total", retryable=str(bool(error and error.retryable)).lower())
    log(logger, "job finished", job=job.id, url=url_ref(job.url), state=job.state, seconds=round(elapsed, 2),
        attempts=attempts, adult=job.adult)


async def deliver_webhook(ctx: dict, job_id: str) -> None:
    """Send the signed job.finished webhook, retrying with backoff on network errors, 5xx and 429."""
    settings = get_settings()
    job = await ctx["store"].get(job_id)
    if job is None or not job.webhook_url or not job.key_id:
        return
    key = await ctx["db"].get_key(job.key_id)
    if key is None or key.revoked:
        return  # a revoked key must stop receiving data
    body, headers = webhooks.build(job.public(), key.webhook_secret)
    try:
        await webhooks.deliver(job.webhook_url, body, headers, settings, ctx["http"])
        await metrics.inc(ctx["store"].redis, "hotload_webhooks_total", result="ok")
    except webhooks.WebhookError as exc:
        attempt = ctx.get("job_try", 1)
        log(logger, "webhook delivery failed", logging.WARNING, job=job_id, attempt=attempt, error=str(exc))
        if exc.retryable and attempt < settings.webhook_max_attempts:
            raise Retry(defer=min(30 * 4 ** (attempt - 1), 3600)) from exc  # 30s, 2m, 8m, 32m
        await metrics.inc(ctx["store"].redis, "hotload_webhooks_total", result="failed")


async def cleanup(ctx: dict) -> None:
    s = get_settings()
    removed = storage.cleanup_expired(s.output_dir, s.file_ttl_seconds)
    if removed:
        log(logger, "expired job files removed", count=removed)


async def run_canaries(ctx: dict) -> None:
    s = get_settings()
    if s.canary_enabled:
        await canary.run_and_record(ctx["redis_client"], s, get_pool())


async def startup(ctx: dict) -> None:
    from redis.asyncio import Redis

    logging_setup.setup()
    s = get_settings()
    s.output_dir.mkdir(parents=True, exist_ok=True)
    ctx["redis_client"] = Redis.from_url(s.redis_url, decode_responses=True)
    ctx["store"] = JobStore(ctx["redis_client"], s.job_ttl_seconds)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    ctx["http"] = httpx.AsyncClient()
    ctx["db"] = Database(s)
    await ctx["db"].migrate()


async def shutdown(ctx: dict) -> None:
    await ctx["http"].aclose()
    await ctx["db"].dispose()
    await ctx["redis_client"].aclose()


class WorkerSettings:
    functions = [run_download, deliver_webhook]
    cron_jobs = [
        cron(cleanup, minute={0, 10, 20, 30, 40, 50}),
        cron(run_canaries, minute={5, 20, 35, 50}, run_at_startup=True, timeout=600),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
    job_timeout = 1800
