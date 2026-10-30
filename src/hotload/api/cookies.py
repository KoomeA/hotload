"""Saved cookies (bring your own session). See hotload.cookies for the handling rules."""

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .. import ratelimit, tiers
from ..config import Settings
from ..cookies import MAX_UPLOAD_BYTES, CookieError, CookieVault, clean_domain


class CookieUpload(BaseModel):
    domain: str = Field(max_length=253)
    cookies_txt: str = Field(max_length=MAX_UPLOAD_BYTES + 1000)


def add_cookie_routes(app: FastAPI, settings: Settings, client_ip) -> None:
    async def session_user(request: Request):
        """Managing credentials needs a browser session; API keys cannot upload, list or delete cookies."""
        if request.headers.get("authorization", "").lower().startswith("bearer hl_"):
            raise HTTPException(403, "Manage saved cookies from a signed-in session")
        user = await app.state.current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in required")
        vault: CookieVault = app.state.vault
        if not vault.enabled:
            raise HTTPException(503, "Saved cookies are not enabled on this server")
        if settings.quotas_enabled and tiers.effective_name(user) == "free":
            raise HTTPException(403, "Saved cookies need a Premium or Pro plan")
        return user

    @app.get("/api/v1/me/cookies")
    async def list_cookies(request: Request):
        user = await session_user(request)
        return [{"domain": c.domain, "cookies": c.count, "saved": c.created} for c in await app.state.db.list_cookies(user.id)]

    @app.put("/api/v1/me/cookies")
    async def save_cookies(body: CookieUpload, request: Request):
        user = await session_user(request)
        if not await ratelimit.allow(app.state.redis, f"cookies:{user.id}", 20, 3600):
            raise HTTPException(429, "Too many uploads, try again later")
        try:
            parsed = await app.state.vault.save(user.id, clean_domain(body.domain), body.cookies_txt)
        except CookieError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"domain": clean_domain(body.domain), "cookies": parsed.kept, "ignored": parsed.dropped}

    @app.delete("/api/v1/me/cookies/{domain}", status_code=204)
    async def delete_cookies(domain: str, request: Request):
        user = await session_user(request)
        try:
            domain = clean_domain(domain)
        except CookieError as exc:
            raise HTTPException(404, "No saved cookies for that site") from exc
        if not await app.state.db.delete_cookie(user.id, domain):
            raise HTTPException(404, "No saved cookies for that site")
