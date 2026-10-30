import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from redis.asyncio import Redis

from .. import compliance, engine, ratelimit, security
from ..config import Settings, get_settings
from ..jobs import JobStore

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ExtractRequest(BaseModel):
    url: str


class JobRequest(BaseModel):
    url: str
    preset: str = "best_mp4"
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    subtitles: bool = False
    age_confirmed: bool = False

    @model_validator(mode="after")
    def _range(self):
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


def create_app(settings: Settings | None = None, redis: Redis | None = None, enqueue=None) -> FastAPI:
    """App factory. `redis` and `enqueue` are injectable for tests."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        app.state.redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        app.state.store = JobStore(app.state.redis, settings.job_ttl_seconds)
        pool = None
        if enqueue is None:
            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            app.state.enqueue = lambda job_id: pool.enqueue_job("run_download", job_id)
        else:
            app.state.enqueue = enqueue
        yield
        if pool:
            await pool.aclose()
        if redis is None:
            await app.state.redis.aclose()

    app = FastAPI(title="Hotload", version="0.1.0", lifespan=lifespan)

    def client_ip(request: Request) -> str:
        if settings.trust_proxy_headers:
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def guard(request: Request, url: str | None = None) -> str | None:
        if not await ratelimit.allow(app.state.redis, client_ip(request), settings.rate_limit_per_minute):
            raise HTTPException(429, "Too many requests, slow down")
        if url is None:
            return None
        try:
            return await asyncio.to_thread(security.validate_url, url, settings)
        except security.UnsafeURL as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/healthz")
    async def healthz():
        await app.state.redis.ping()
        return {"ok": True}

    @app.get("/api/v1/presets")
    async def presets():
        return [{"key": p.key, "label": p.label, "audio": bool(p.audio_codec)} for p in engine.PRESETS.values()]

    @app.post("/api/v1/extract")
    async def extract(body: ExtractRequest, request: Request):
        url = await guard(request, body.url)
        try:
            info = await asyncio.to_thread(engine.extract, url, settings)
        except engine.EngineError as exc:
            raise HTTPException(422, str(exc)) from exc
        info["adult"] = compliance.is_adult(url, info)
        info["age_gate_required"] = info["adult"] and settings.require_age_gate
        return info

    @app.post("/api/v1/jobs", status_code=202)
    async def create_job(body: JobRequest, request: Request):
        url = await guard(request, body.url)
        if body.preset not in engine.PRESETS:
            raise HTTPException(400, "Unknown preset")
        try:
            adult = compliance.enforce_age_gate(url, None, body.age_confirmed, settings.require_age_gate)
        except compliance.AgeGateRequired as exc:
            raise HTTPException(451, str(exc)) from exc
        job = await app.state.store.create(
            url=url, preset=body.preset, start=body.start, end=body.end, subtitles=body.subtitles,
            adult=adult, age_confirmed=body.age_confirmed,
        )
        await app.state.enqueue(job.id)
        return job.public()

    async def load(job_id: str):
        job = await app.state.store.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found or expired")
        return job

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str):
        return (await load(job_id)).public()

    @app.get("/api/v1/jobs/{job_id}/events")
    async def job_events(job_id: str):
        await load(job_id)

        async def stream():
            last = None
            for _ in range(settings.max_duration_seconds * 2):
                job = await app.state.store.get(job_id)
                if job is None:
                    break
                payload = json.dumps(job.public())
                if payload != last:
                    yield f"data: {payload}\n\n"
                    last = payload
                if job.state in ("done", "error"):
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/v1/jobs/{job_id}/file")
    async def job_file(job_id: str, token: str = Query(...)):
        job = await load(job_id)
        if not secrets.compare_digest(token, job.token):
            raise HTTPException(403, "Invalid token")
        if job.state != "done" or not job.path or not Path(job.path).is_file():
            raise HTTPException(404, "File is not available")
        return FileResponse(job.path, filename=job.filename)

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _default_app() -> FastAPI:
    return create_app()


app = _default_app()
