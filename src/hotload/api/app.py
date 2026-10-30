import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from redis.asyncio import Redis
from starlette.middleware.sessions import SessionMiddleware
from zipstream import ZipStream

from .. import (
    auth,
    canary,
    compliance,
    engine,
    logging_setup,
    metrics,
    ratelimit,
    security,
    storage,
    tiers,
)
from ..billing.service import BillingService, build_providers
from ..config import Settings, get_settings
from ..cookies import CookieVault
from ..db import Database
from ..jobs import BatchStore, JobStore
from ..proxies import NoProxyAvailable, ProxyPool
from .accounts import add_account_routes, origin_allowed
from .admin import RuleCache, add_admin_routes
from .billing import add_billing_routes
from .cookies import add_cookie_routes
from .keys import add_key_routes, record_usage

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ExtractRequest(BaseModel):
    url: str
    use_cookies: bool = False


class JobOptions(BaseModel):
    preset: str = "best_mp4"
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    subtitle_langs: list[str] = Field(default_factory=list, max_length=10)
    subtitle_format: Literal["srt", "vtt"] = "srt"
    subtitle_mode: Literal["embed", "file"] = "file"
    subtitle_auto: bool = False
    audio_lang: str | None = None
    sponsorblock: Literal["off", "mark", "remove"] = "off"
    use_cookies: bool = False  # use my saved cookies for this site (see docs/COOKIES.md)
    age_confirmed: bool = False
    webhook_url: str | None = Field(default=None, max_length=2048)  # API keys only: signed POST when the job finishes

    @model_validator(mode="after")
    def _check(self):
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        if self.sponsorblock != "off" and (self.start is not None or self.end is not None):
            raise ValueError("Choose either trimming or SponsorBlock, not both")
        for code in [*self.subtitle_langs, *([self.audio_lang] if self.audio_lang else [])]:
            engine.validate_lang(code)
        return self


class JobRequest(JobOptions):
    url: str


class BatchRequest(JobOptions):
    urls: list[str] = Field(min_length=1, max_length=50)


class PlaylistRequest(BaseModel):
    url: str


def create_app(settings: Settings | None = None, redis: Redis | None = None, enqueue=None,
               http_client: httpx.AsyncClient | None = None) -> FastAPI:
    """App factory. `redis`, `enqueue` and `http_client` (payment providers) are injectable for tests."""
    settings = settings or get_settings()
    secret = settings.secret_key or secrets.token_urlsafe(48)  # per-process if unset: sessions reset on restart

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging_setup.setup()
        app.state.pool = ProxyPool.from_settings(settings)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        app.state.db = Database(settings)
        await app.state.db.migrate()
        app.state.sessions = auth.Sessions(secret)
        app.state.rules = RuleCache(app.state.db)
        app.state.vault = CookieVault(settings, app.state.db)
        app.state.remote = storage.build_remote(settings)
        app.state.http = http_client or httpx.AsyncClient(follow_redirects=False)
        app.state.billing = BillingService(settings, app.state.db, build_providers(settings, app.state.http))
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        app.state.redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        app.state.store = JobStore(app.state.redis, settings.job_ttl_seconds)
        app.state.batches = BatchStore(app.state.redis, settings.job_ttl_seconds)
        pool = None
        if enqueue is None:
            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            app.state.enqueue = lambda job_id, queue=None: pool.enqueue_job(
                "run_download", job_id, **({"_queue_name": queue} if queue else {}))
        else:
            app.state.enqueue = enqueue
        yield
        if pool:
            await pool.aclose()
        if http_client is None:
            await app.state.http.aclose()
        await app.state.db.dispose()
        if redis is None:
            await app.state.redis.aclose()

    app = FastAPI(title="Hotload", version="0.1.0", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="hl_oauth", same_site="lax",
                       https_only=settings.cookie_secure, max_age=600)

    # CORS for API clients (browser extensions, third-party web apps). Credentials are NOT allowed, so browsers never
    # attach cookies to cross-origin calls: only Authorization-header API keys work cross-origin.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                       allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                       allow_headers=["authorization", "content-type"], max_age=600)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        # Requests that authenticate with an API key carry no ambient cookie, so there is nothing for a hostile
        # site to ride on: the CSRF origin check only protects cookie sessions.
        keyed = request.headers.get("authorization", "").lower().startswith("bearer hl_")
        if request.method not in ("GET", "HEAD", "OPTIONS") and not keyed and not origin_allowed(request, settings):
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        return await call_next(request)

    def client_ip(request: Request) -> str:
        if settings.trust_proxy_headers:
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def guard(request: Request, url: str | None = None, cost: int = 1, key=None) -> str | None:
        bucket, limit = (f"key:{key.id}", settings.api_rate_limit_per_minute) if key else (client_ip(request), settings.rate_limit_per_minute)
        if not await ratelimit.allow(app.state.redis, bucket, limit, cost=cost):
            raise HTTPException(429, "Too many requests, slow down")
        if url is None:
            return None
        # Takedown rules first: no DNS lookup for blocked content, and a blocked host answers 451 even if DNS is down.
        rules = await app.state.rules.get()
        if rules.blocks_url(url.strip()):
            raise HTTPException(451, "This content is not available")
        try:
            clean = await asyncio.to_thread(security.validate_url, url, settings)
        except security.UnsafeURL as exc:
            raise HTTPException(400, str(exc)) from exc
        if rules.blocks_url(clean):
            raise HTTPException(451, "This content is not available")
        return clean

    def extract_with_retries(url: str, cookiefile=None) -> dict:
        """Blocking: try up to `max_attempts` egress routes, rotating proxies on retryable failures."""
        pool: ProxyPool = app.state.pool
        tried: set[str] = set()
        last: engine.EngineError | None = None
        for _ in range(settings.max_attempts):
            try:
                proxy = pool.select(url, exclude=tried)
            except NoProxyAvailable:
                break
            tried.add(proxy or "direct")
            try:
                info = engine.extract(url, settings, proxy=proxy, cookiefile=cookiefile)
                pool.report(proxy, True)
                return info
            except engine.EngineError as exc:
                last = exc
                if not exc.retryable:
                    break
                pool.report(proxy, False)
        raise last or engine.EngineError("No working connection was available, try again later", retryable=True)

    @app.get("/healthz")
    async def healthz():
        await app.state.redis.ping()
        return {"ok": True}

    @app.get("/api/v1/status")
    async def status():
        """Public health summary: yt-dlp version and which sites are currently degraded."""
        latest = await canary.latest(app.state.redis)
        return {
            "ok": True,
            "canary": latest and {k: latest[k] for k in ("checked_at", "ytdlp_version", "degraded")},
            "sites": latest and [{"name": r["name"], "site": r["site"], "ok": r["ok"]} for r in latest["results"]],
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus(request: Request):
        if settings.metrics_token and request.headers.get("authorization") != f"Bearer {settings.metrics_token}":
            raise HTTPException(401, "Unauthorized")
        return await metrics.render(app.state.redis)

    @app.get("/api/v1/presets")
    async def presets():
        return [{"key": p.key, "label": p.label, "audio": bool(p.audio_codec)} for p in engine.PRESETS.values()]

    @app.post("/api/v1/extract")
    async def extract(body: ExtractRequest, request: Request):
        _, _, key = await who(request)
        url = await guard(request, body.url, key=key)
        user, _, _ = await who(request)
        try:
            if body.use_cookies:
                await require_cookie_access(user, url)
                async with app.state.vault.file_for(user.id, url) as cookie_path:
                    info = await asyncio.to_thread(extract_with_retries, url, cookie_path)
            else:
                info = await asyncio.to_thread(extract_with_retries, url)
        except engine.EngineError as exc:
            await metrics.inc(app.state.redis, "hotload_extract_total", result="error")
            raise HTTPException(503 if exc.retryable else 422, str(exc)) from exc
        await metrics.inc(app.state.redis, "hotload_extract_total", result="ok", site=str(info.get("site")))
        if (await app.state.rules.get()).blocks_media(info.get("site"), info.get("id")):
            raise HTTPException(451, "This content is not available")
        info["adult"] = compliance.is_adult(url, info)
        info["age_gate_required"] = info["adult"] and settings.require_age_gate
        return info

    async def require_cookie_access(user, url: str) -> None:
        """Opting in to saved cookies needs an account whose plan allows it, with cookies saved for this very site."""
        if user is None:
            raise HTTPException(401, "Sign in to use your saved cookies")
        if not app.state.vault.enabled:
            raise HTTPException(503, "Saved cookies are not enabled on this server")
        if settings.quotas_enabled and tiers.effective_name(user) == "free":
            raise HTTPException(403, "Saved cookies need a Premium or Pro plan")
        if await app.state.vault.match(user.id, url) is None:
            raise HTTPException(400, "You have no saved cookies for this site")

    def queue_for(tier: tiers.Tier) -> str | None:
        """Paying plans go to the priority worker pool (only when plans are enforced, i.e. the hosted service)."""
        return tiers.PRIORITY_QUEUE if settings.quotas_enabled and tier.priority else None

    async def who(request: Request):
        user, key = await app.state.authenticate(request)
        return user, tiers.tier_for(user), key

    def precheck(url: str, o: JobOptions, tier: tiers.Tier) -> bool:
        """Validate options for one URL against the preset, age gate and plan. Returns whether it is adult."""
        if o.preset not in engine.PRESETS:
            raise HTTPException(400, "Unknown preset")
        preset = engine.PRESETS[o.preset]
        if preset.subs_only and not o.subtitle_langs:
            raise HTTPException(400, "Choose at least one subtitle language")
        if settings.quotas_enabled:
            if (o.start is not None or o.end is not None) and not preset.audio_codec and not preset.subs_only \
                    and not tier.trim_video:
                raise HTTPException(403, "Trimming video needs a Premium plan (audio trimming is free)")
        try:
            return compliance.enforce_age_gate(url, None, o.age_confirmed, settings.require_age_gate)
        except compliance.AgeGateRequired as exc:
            raise HTTPException(451, str(exc)) from exc

    async def spend_quota(request: Request, user, tier: tiers.Tier, cost: int) -> None:
        if settings.quotas_enabled and not await tiers.consume(
                app.state.redis, tiers.subject(user, client_ip(request)), tier, cost):
            raise HTTPException(429, f"Daily limit reached for your plan ({tier.jobs_per_window} downloads per 12 hours)")

    async def check_webhook(o: JobOptions, key) -> str | None:
        if not o.webhook_url:
            return None
        if key is None:
            raise HTTPException(400, "Webhooks need an API key")
        if not o.webhook_url.lower().startswith("https://") and not settings.allow_private_targets:
            raise HTTPException(400, "Webhook URLs must use https")  # the payload carries a download token
        try:
            return await asyncio.to_thread(security.validate_url, o.webhook_url, settings)
        except security.UnsafeURL as exc:
            raise HTTPException(400, f"Webhook URL: {exc}") from exc

    async def new_job(url: str, o: JobOptions, adult: bool, user, tier: tiers.Tier, batch_id: str | None = None,
                      key=None, webhook_url: str | None = None):
        job = await app.state.store.create(
            url=url, preset=o.preset, start=o.start, end=o.end, subtitle_langs=o.subtitle_langs,
            subtitle_format=o.subtitle_format, subtitle_mode=o.subtitle_mode, subtitle_auto=o.subtitle_auto,
            audio_lang=o.audio_lang, sponsorblock=o.sponsorblock, use_cookies=o.use_cookies, adult=adult, age_confirmed=o.age_confirmed, batch_id=batch_id,
            user_id=user.id if user else None, key_id=key.id if key else None, webhook_url=webhook_url,
            max_duration=tier.max_duration if settings.quotas_enabled else None,
        )
        if user:
            await app.state.db.add_history(user.id, job)
            await record_usage(app.state.redis, user.id, key.id if key else None, "jobs")
        return job

    @app.post("/api/v1/jobs", status_code=202)
    async def create_job(body: JobRequest, request: Request):
        user, tier, key = await who(request)
        url = await guard(request, body.url, key=key)
        hook = await check_webhook(body, key)
        if body.use_cookies:
            await require_cookie_access(user, url)
        adult = precheck(url, body, tier)
        await spend_quota(request, user, tier, 1)
        job = await new_job(url, body, adult, user, tier, key=key, webhook_url=hook)
        await app.state.enqueue(job.id, queue_for(tier))
        return job.public()

    @app.post("/api/v1/playlist")
    async def playlist(body: PlaylistRequest, request: Request):
        _, _, key = await who(request)
        url = await guard(request, body.url, key=key)
        try:
            data = await asyncio.to_thread(
                lambda: engine.extract_playlist(url, settings, app.state.pool.select(url), settings.max_playlist_entries))
        except (engine.EngineError, NoProxyAvailable) as exc:
            raise HTTPException(422, str(exc)) from exc
        if data["playlist"]:
            checked = await asyncio.gather(*(asyncio.to_thread(_safe, e["url"]) for e in data["entries"]))
            rules = await app.state.rules.get()
            data["entries"] = [e for e, ok in zip(data["entries"], checked, strict=True) if ok and not rules.blocks_url(e["url"])]
            data["count"] = len(data["entries"])
        return data

    def _safe(url: str) -> bool:
        try:
            security.validate_url(url, settings)
            return True
        except security.UnsafeURL:
            return False

    @app.post("/api/v1/batches", status_code=202)
    async def create_batch(body: BatchRequest, request: Request):
        user, tier, key = await who(request)
        limit = min(settings.max_batch_size, tier.batch_size) if settings.quotas_enabled else settings.max_batch_size
        if len(body.urls) > limit:
            raise HTTPException(400, f"Your plan allows up to {limit} links per batch")
        await guard(request, cost=len(body.urls), key=key)
        hook = await check_webhook(body, key)
        urls = []
        for raw in dict.fromkeys(u.strip() for u in body.urls if u.strip()):
            rules = await app.state.rules.get()
            if rules.blocks_url(raw):
                raise HTTPException(451, f"{raw[:80]}: this content is not available")
            try:
                clean = await asyncio.to_thread(security.validate_url, raw, settings)
            except security.UnsafeURL as exc:
                raise HTTPException(400, f"{raw[:80]}: {exc}") from exc
            if rules.blocks_url(clean):
                raise HTTPException(451, f"{raw[:80]}: this content is not available")
            urls.append(clean)
        if body.use_cookies:
            for u in urls:
                await require_cookie_access(user, u)
        adults = [precheck(u, body, tier) for u in urls]
        await spend_quota(request, user, tier, len(urls))
        jobs = [await new_job(u, body, adult, user, tier, key=key, webhook_url=hook)
                for u, adult in zip(urls, adults, strict=True)]
        batch = await app.state.batches.create([j.id for j in jobs])
        for j in jobs:
            j.batch_id = batch.id
            await app.state.store.save(j)
            await app.state.enqueue(j.id, queue_for(tier))
        return batch_view(batch, jobs)

    def batch_view(batch, jobs):
        done = [j for j in jobs if j.state == "done"]
        return {
            "id": batch.id,
            "jobs": [j.public() for j in jobs],
            "done": len(done),
            "total": len(jobs),
            "finished": all(j.state in ("done", "error") for j in jobs),
            "zip_url": f"/api/v1/batches/{batch.id}/zip?token={batch.token}" if done else None,
        }

    async def load_batch(batch_id: str):
        batch = await app.state.batches.get(batch_id)
        if batch is None:
            raise HTTPException(404, "Batch not found or expired")
        jobs = [j for j in [await app.state.store.get(i) for i in batch.job_ids] if j]
        return batch, jobs

    @app.get("/api/v1/batches/{batch_id}")
    async def get_batch(batch_id: str):
        batch, jobs = await load_batch(batch_id)
        return batch_view(batch, jobs)

    @app.get("/api/v1/batches/{batch_id}/zip")
    async def batch_zip(batch_id: str, token: str = Query(...)):
        batch, jobs = await load_batch(batch_id)
        if not secrets.compare_digest(token, batch.token):
            raise HTTPException(403, "Invalid token")
        files = []  # (local path or None, storage key or None, filename)
        for j in jobs:
            if j.state != "done":
                continue
            if j.storage_key and app.state.remote:
                files.append((None, j.storage_key, j.filename))
            elif j.path and Path(j.path).is_file():
                files.append((j.path, None, j.filename))
        if not files:
            raise HTTPException(404, "No finished files yet")
        zs = ZipStream(sized=False)
        for n, (path, key, name) in enumerate(files, 1):
            arc = f"{n:02d} - {name}"
            if path:
                zs.add_path(path, arcname=arc)
            else:
                zs.add(app.state.remote.chunks(key), arcname=arc)  # streamed from the bucket, never fully in memory
        return StreamingResponse(zs, media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="hotload-{batch.id}.zip"'})

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
        if job.state == "done" and job.storage_key and app.state.remote:
            url = await asyncio.to_thread(app.state.remote.presigned_url, job.storage_key, job.filename or "download")
            return RedirectResponse(url, status_code=302)  # short-lived signed URL; the API never proxies the bytes
        if job.state != "done" or not job.path or not Path(job.path).is_file():
            raise HTTPException(404, "File is not available")
        return FileResponse(job.path, filename=job.filename)

    add_account_routes(app, settings, client_ip)
    add_billing_routes(app, settings, client_ip)
    add_key_routes(app, settings)
    add_admin_routes(app, settings, client_ip)
    add_cookie_routes(app, settings, client_ip)

    web_dir = settings.web_dir or WEB_DIR
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def _default_app() -> FastAPI:
    return create_app()


app = _default_app()
