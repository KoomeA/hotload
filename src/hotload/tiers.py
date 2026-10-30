"""Plan limits. Only enforced when HOTLOAD_QUOTAS_ENABLED=true (the hosted service); self-hosters are unlimited.

Billing (M3) only has to set `users.tier`; enforcement already reads it.
"""

from dataclasses import dataclass

from redis.asyncio import Redis

from . import ratelimit

WINDOW_SECONDS = 12 * 3600


@dataclass(frozen=True)
class Tier:
    name: str
    jobs_per_window: int
    max_duration: int  # seconds of media (after trimming)
    batch_size: int
    trim_video: bool
    audio_tracks: int


TIERS: dict[str, Tier] = {
    t.name: t
    for t in (
        Tier("anonymous", 10, 20 * 60, 3, False, 1),
        Tier("free", 10, 20 * 60, 5, False, 1),
        Tier("premium", 100, 180 * 60, 10, True, 3),
        Tier("pro", 300, 360 * 60, 20, True, 3),
    )
}


def tier_for(user) -> Tier:
    return TIERS.get(getattr(user, "tier", None) or "anonymous", TIERS["anonymous"])


def subject(user, ip: str) -> str:
    return f"u:{user.id}" if user else f"ip:{ip}"


async def used(redis: Redis, who: str) -> int:
    import time

    key = f"hotload:rl:quota:{who}:{int(time.time()) // WINDOW_SECONDS}"
    return int(await redis.get(key) or 0)


async def consume(redis: Redis, who: str, tier: Tier, cost: int = 1) -> bool:
    return await ratelimit.allow(redis, f"quota:{who}", tier.jobs_per_window, WINDOW_SECONDS, cost)
