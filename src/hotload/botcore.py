"""What every chat bot does to turn a link into a queued job, so Telegram and Discord cannot drift apart.

The caller says who is asking (`platform` + `subject`) and how to tell them later (`notify_payload`); this applies the same
protections as the web app (rate limits, SSRF checks, takedown list, age gate, anonymous plan quota) and returns the text to
show the requester. Replies are plain text because titles and URLs are untrusted.
"""

import asyncio
import logging

from fastapi import FastAPI

from . import compliance, ratelimit, security, tiers
from .config import Settings
from .logging_setup import log

logger = logging.getLogger("hotload.bot")


async def request_download(app: FastAPI, settings: Settings, platform: str, subject: int | str, url: str, preset: str,
                           notify_payload: dict, age_hint: str) -> str:
    redis = app.state.redis
    who = f"{platform}:{subject}"
    if not await ratelimit.allow(redis, who, 10, 60) or not await ratelimit.allow(redis, f"{platform}global", 300, 60):
        # per requester, plus one bot-wide cap so many throwaway accounts cannot add up to unlimited work
        return "Slow down a little, then send the link again."
    try:
        clean = await asyncio.to_thread(security.validate_url, url, settings)
    except security.UnsafeURL as exc:
        return f"I can't use that link: {exc}."
    if (await app.state.rules.get()).blocks_url(clean):
        return "That content is not available."
    confirmed = bool(await redis.get(f"hotload:{platform}:age:{subject}"))
    try:
        adult = compliance.enforce_age_gate(clean, None, confirmed, settings.require_age_gate)
    except compliance.AgeGateRequired:
        return "That looks like adult content." + age_hint
    tier = tiers.TIERS["anonymous"]
    if settings.quotas_enabled and not await tiers.consume(redis, who, tier):
        return f"Daily limit reached ({tier.jobs_per_window} downloads per 12 hours)."
    job = await app.state.store.create(
        url=clean, preset=preset, adult=adult, age_confirmed=confirmed,
        max_duration=tier.max_duration if settings.quotas_enabled else None, notify=notify_payload)
    await app.state.enqueue(job.id, None)
    log(logger, "bot job queued", job=job.id, preset=preset, platform=platform)
    return "Queued. I'll message you when it's ready."
