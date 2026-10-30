import time

from redis.asyncio import Redis


async def allow(redis: Redis, bucket: str, limit: int, window: int = 60, cost: int = 1) -> bool:
    """Fixed-window limiter. Returns False when `bucket` exceeded `limit` in the current window."""
    key = f"hotload:rl:{bucket}:{int(time.time()) // window}"
    count = await redis.incrby(key, cost)
    if count == cost:
        await redis.expire(key, window + 1)
    if count > limit:
        await redis.decrby(key, cost)  # a refused request must not use up allowance (a batch of 20 with 5 left costs nothing)
        return False
    return True
