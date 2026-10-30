"""Redis-backed metrics shared by the API and worker processes, rendered as Prometheus text.

The API and worker are separate processes, so counters live in one Redis hash instead of in-process
registries; `/metrics` renders them on demand. No client library is required.
"""

import re
from collections.abc import Mapping

from redis.asyncio import Redis

KEY = "hotload:metrics"
_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


def _field(name: str, labels: Mapping[str, str]) -> str:
    assert _NAME.fullmatch(name), name
    lab = ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))
    return f"{name}{{{lab}}}" if lab else name


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:80]


async def inc(redis: Redis, name: str, amount: float = 1, /, **labels: str) -> None:
    await redis.hincrbyfloat(KEY, _field(name, labels), amount)


async def observe(redis: Redis, name: str, value: float, /, **labels: str) -> None:
    """Record a summary observation as `<name>_sum` and `<name>_count`."""
    await redis.hincrbyfloat(KEY, _field(f"{name}_sum", labels), value)
    await redis.hincrbyfloat(KEY, _field(f"{name}_count", labels), 1)


async def set_gauge(redis: Redis, name: str, value: float, /, **labels: str) -> None:
    await redis.hset(f"{KEY}:gauges", _field(name, labels), value)


async def render(redis: Redis) -> str:
    lines: list[str] = []
    for key in (KEY, f"{KEY}:gauges"):
        data = await redis.hgetall(key)
        for field_name in sorted(data):
            lines.append(f"{field_name} {float(data[field_name]):.15g}")
    return "\n".join(lines) + "\n"
