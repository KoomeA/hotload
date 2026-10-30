"""Job model and Redis-backed store."""

import json
import secrets
import time
from dataclasses import asdict, dataclass, field

from redis.asyncio import Redis

STATES = ("queued", "running", "done", "error")


@dataclass
class Job:
    id: str
    url: str
    preset: str
    state: str = "queued"
    progress: float = 0.0
    message: str = ""
    title: str | None = None
    filename: str | None = None
    path: str | None = None
    adult: bool = False
    age_confirmed: bool = False
    start: float | None = None
    end: float | None = None
    subtitles: bool = False
    created: float = field(default_factory=time.time)
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    def public(self) -> dict:
        d = asdict(self)
        for hidden in ("path", "token", "url", "age_confirmed"):
            d.pop(hidden)
        d["download_url"] = f"/api/v1/jobs/{self.id}/file?token={self.token}" if self.state == "done" else None
        return d


class JobStore:
    def __init__(self, redis: Redis, ttl: int):
        self.redis, self.ttl = redis, ttl

    @staticmethod
    def key(job_id: str) -> str:
        return f"hotload:job:{job_id}"

    async def create(self, **kwargs) -> Job:
        job = Job(id=secrets.token_urlsafe(9), **kwargs)
        await self.save(job)
        return job

    async def save(self, job: Job) -> None:
        await self.redis.set(self.key(job.id), json.dumps(asdict(job)), ex=self.ttl)

    async def get(self, job_id: str) -> Job | None:
        raw = await self.redis.get(self.key(job_id))
        return Job(**json.loads(raw)) if raw else None
