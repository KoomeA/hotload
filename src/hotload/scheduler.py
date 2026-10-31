"""Scheduled recording: release a waiting schedule when its stream is live.

A schedule is a row in the database, not a running job, so waiting costs no worker. A timer on the workers (every 30 s) looks
at the schedules whose planned start has passed, checks whether the stream is live (at most `schedule_probes_per_tick`
distinct pages per tick, one check per page however many people wait for it), and for each live one creates and queues an
ordinary live-recording job. A stream that has not started within the grace period is marked missed.

Guards: the plan is checked again at release time (it may have lapsed), as are the address and the takedown list;
`max_scheduled_running` bounds how many scheduled recordings run at once so they cannot take every worker; and releasing is a
compare-and-set on the schedule's state, so two workers (or a cancel) can never produce two recordings.
"""

import asyncio
import logging
import time

from . import engine, mail, metrics, security, tiers
from .blocklist import Blocklist
from .config import Settings, get_settings
from .logging_setup import log, url_ref
from .proxies import NoProxyAvailable

logger = logging.getLogger("hotload.scheduler")
RUNNING = "hotload:sched:running"  # sorted set: job id -> time it should be finished by (self-heals if a worker dies)
RECHECK_SECONDS = 30


def _settings(ctx: dict) -> Settings:
    return ctx.get("settings") or get_settings()


def probe_live(url: str, settings: Settings, pool) -> bool:
    """Blocking: is the stream live right now? Any failure (an offline channel, an unannounced event) means "not yet"."""
    try:
        proxy = pool.select(url)
    except NoProxyAvailable:
        return False
    try:
        info = engine.extract(url, settings, proxy=proxy)
    except engine.EngineError:
        return False
    return bool(info.get("is_live")) or info.get("live_status") == "is_live"


async def _tell(ctx: dict, settings: Settings, user, ok: bool, title: str | None, detail: str) -> None:
    mailer = ctx.get("mailer")
    if mailer is None or user is None or not settings.public_url:
        return
    link = f"{settings.public_url.rstrip('/')}/account/#recordings"
    await mailer.send(user.email, *mail.recording_message(link, title, ok, detail, settings.scheduled_keep_hours))


async def notify_owner(ctx: dict, job) -> None:
    """Email the owner of a finished scheduled recording (the worker calls this for jobs carrying notify kind "email")."""
    settings = _settings(ctx)
    user = await ctx["db"].user_by_id((job.notify or {}).get("user_id") or "")
    await _tell(ctx, settings, user, job.state == "done", job.title, job.message or "The recording failed.")


async def _settle(ctx: dict, settings: Settings, s, user, state: str, message: str) -> None:
    if await ctx["db"].settle_schedule(s.id, state, message):
        await metrics.inc(ctx["redis_client"], "hotload_scheduled_total", result=state)
        log(logger, f"schedule {state}", logging.WARNING, schedule=s.id, url=url_ref(s.url), reason=message)
        await _tell(ctx, settings, user, False, None, message)


async def release(ctx: dict, settings: Settings, s, running: int) -> str:
    """Turn one schedule whose stream is live into a queued recording. Returns "queued", "deferred" or "failed"."""
    db, store, rc = ctx["db"], ctx["store"], ctx["redis_client"]
    if running >= settings.max_scheduled_running:
        log(logger, "schedule deferred: too many scheduled recordings running", schedule=s.id)
        return "deferred"  # stays waiting; the next tick tries again (until the grace period runs out)
    user = await db.user_by_id(s.user_id)
    if user is None:
        await _settle(ctx, settings, s, None, "failed", "The account no longer exists")
        return "failed"
    tier = tiers.tier_for(user)
    minutes = s.minutes
    if settings.quotas_enabled:
        if tier.live_minutes <= 0:
            await _settle(ctx, settings, s, user, "failed", "Your plan no longer includes live recording")
            return "failed"
        if not settings.egress_filtered:
            await _settle(ctx, settings, s, user, "failed", "Live recording is temporarily unavailable")
            return "failed"
        minutes = min(minutes, tier.live_minutes)
        if not await tiers.consume(rc, tiers.subject(user, ""), tier):
            await _settle(ctx, settings, s, user, "failed", f"Daily limit reached ({tier.jobs_per_window} downloads per 12 hours)")
            return "failed"
    else:
        minutes = min(minutes, settings.max_live_minutes)
    rules = Blocklist(await db.blocked_items())
    if rules.blocks_url(s.url):  # takedowns first: no DNS lookup for blocked content
        await _settle(ctx, settings, s, user, "failed", "That content is not available")
        return "failed"
    try:
        clean = await asyncio.to_thread(security.validate_url, s.url, settings)
    except security.UnsafeURL as exc:
        await _settle(ctx, settings, s, user, "failed", f"The link is no longer allowed: {exc}")
        return "failed"
    if rules.blocks_url(clean):
        await _settle(ctx, settings, s, user, "failed", "That content is not available")
        return "failed"
    job = await store.create(
        url=clean, preset=s.preset, live_seconds=minutes * 60, adult=s.adult, age_confirmed=s.age_confirmed, user_id=user.id,
        max_duration=tier.max_duration if settings.quotas_enabled else None,
        max_filesize=tier.max_filesize_mb * 1024 * 1024 if settings.quotas_enabled else None,
        retention=settings.scheduled_keep_hours * 3600, notify={"kind": "email", "user_id": user.id})
    if not await db.settle_schedule(s.id, "queued", f"Recording for up to {minutes} minutes", job.id):
        return "deferred"  # cancelled (or released elsewhere) while we were preparing: the unqueued job simply expires
    await db.add_history(user.id, job)
    queue = tiers.PRIORITY_QUEUE if settings.quotas_enabled and tier.priority else None
    await ctx["redis"].enqueue_job("run_download", job.id, **({"_queue_name": queue} if queue else {}))
    await rc.zadd(RUNNING, {job.id: time.time() + minutes * 60 + 900})
    await metrics.inc(rc, "hotload_scheduled_total", result="queued")
    log(logger, "schedule released", schedule=s.id, job=job.id, url=url_ref(s.url), minutes=minutes)
    return "queued"


async def release_due(ctx: dict, now: float | None = None, probe=probe_live) -> dict:
    """One tick: check the live status of due schedules and release the live ones. Returns counts (for tests and logs)."""
    settings = _settings(ctx)
    now = now or time.time()
    db, rc = ctx["db"], ctx["redis_client"]
    stats = {"queued": 0, "missed": 0, "failed": 0, "deferred": 0, "waiting": 0, "probed": 0}
    due = await db.due_schedules(now, 100)
    if not due:
        return stats
    await rc.zremrangebyscore(RUNNING, "-inf", now)
    running = await rc.zcount(RUNNING, now, "+inf")
    live: dict[str, bool] = {}
    grace = settings.schedule_grace_minutes * 60
    for s in due:  # oldest planned start first
        if s.url not in live:
            if len(live) >= settings.schedule_probes_per_tick:
                continue  # over this tick's budget: stays due, so the next tick picks it up
            live[s.url] = await asyncio.to_thread(probe, s.url, settings, ctx["pool"])
            stats["probed"] += 1
        if live[s.url]:
            outcome = await release(ctx, settings, s, running)
            stats[outcome] += 1
            running += outcome == "queued"
        elif now > s.start_at + grace:
            await _settle(ctx, settings, s, await db.user_by_id(s.user_id), "missed",
                          f"The stream did not start within {settings.schedule_grace_minutes} minutes of the planned time")
            stats["missed"] += 1
        else:
            await db.defer_schedule(s.id, now + RECHECK_SECONDS)
            stats["waiting"] += 1
    return stats
