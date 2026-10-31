"""Plan limits. Only enforced when HOTLOAD_QUOTAS_ENABLED=true (the hosted service); self-hosters are unlimited.

Billing (M3) only has to set `users.tier`; enforcement already reads it.
"""

import time
from dataclasses import dataclass

from redis.asyncio import Redis

from . import ratelimit

WINDOW_SECONDS = 12 * 3600
PRIORITY_QUEUE = "hotload:priority"


@dataclass(frozen=True)
class Tier:
    name: str
    jobs_per_window: int
    max_duration: int  # seconds of media (after trimming)
    batch_size: int
    trim_video: bool
    audio_tracks: int
    priority: bool = False  # served by the priority worker pool
    live_minutes: int = 0  # longest live recording allowed (0 = not on this plan)
    max_filesize_mb: int = 500  # largest single file; also bounds what FFmpeg may write for this job
    scheduled_max: int = 0  # recordings that may be waiting to start at once (0 = not on this plan)


TIERS: dict[str, Tier] = {
    t.name: t
    for t in (
        Tier("anonymous", 10, 20 * 60, 3, False, 1),
        Tier("free", 10, 20 * 60, 5, False, 1),
        Tier("premium", 100, 180 * 60, 10, True, 3, True, 30, 2000, 3),
        Tier("pro", 300, 360 * 60, 20, True, 3, True, 60, 4000, 10),
    )
}


def effective_name(user, now: float | None = None) -> str:
    """The plan a user is on right now: a paid plan whose period ended counts as free."""
    if user is None:
        return "anonymous"
    expires = getattr(user, "tier_expires", None)
    if user.tier not in ("free", "anonymous") and expires is not None and expires <= (now or time.time()):
        return "free"
    return user.tier or "free"


def tier_for(user) -> Tier:
    return TIERS.get(effective_name(user), TIERS["free"] if user else TIERS["anonymous"])


def subject(user, ip: str) -> str:
    return f"u:{user.id}" if user else f"ip:{ip}"


async def used(redis: Redis, who: str) -> int:
    key = f"hotload:rl:quota:{who}:{int(time.time()) // WINDOW_SECONDS}"
    return int(await redis.get(key) or 0)


async def consume(redis: Redis, who: str, tier: Tier, cost: int = 1) -> bool:
    return await ratelimit.allow(redis, f"quota:{who}", tier.jobs_per_window, WINDOW_SECONDS, cost)
