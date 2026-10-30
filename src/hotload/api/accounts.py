"""Account, session, history, quota and OAuth routes."""

import logging
import re
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from .. import auth, oauth, ratelimit, tiers
from ..config import Settings
from ..logging_setup import log

logger = logging.getLogger("hotload.accounts")


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=128)


class DeleteAccount(BaseModel):
    password: str | None = Field(default=None, max_length=128)


def _email(raw: str) -> str:
    try:
        return validate_email(raw.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise HTTPException(422, "Enter a valid email address") from exc


def add_account_routes(app: FastAPI, settings: Settings, client_ip) -> None:
    oauth_client = oauth.build(settings)

    def set_cookie(response: Response, user) -> None:
        response.set_cookie(auth.COOKIE, app.state.sessions.issue(user.id, user.session_epoch), max_age=auth.MAX_AGE,
                            httponly=True, samesite="lax", secure=settings.secure_cookies, path="/")

    async def current_user(request: Request):
        """The signed-in user for a cookie session, or None.

        A request that carries an `Authorization: Bearer hl_...` header authenticates only through that key
        (see keys.authenticate); the cookie is deliberately ignored so a cookie can never ride along with a key.
        """
        if request.headers.get("authorization", "").lower().startswith("bearer hl_"):
            return None
        session = app.state.sessions.read(request.cookies.get(auth.COOKIE))
        if session is None:
            return None
        user = await app.state.db.user_by_id(session[0])
        return user if user is not None and user.session_epoch == session[1] else None  # a bumped epoch revokes it
    app.state.current_user = current_user

    async def require_user(request: Request):
        user = await current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in required")
        return user

    def view(user) -> dict:
        t = tiers.tier_for(user)
        return {"id": user.id, "email": user.email, "tier": tiers.effective_name(user),
                "tier_expires": user.tier_expires if tiers.effective_name(user) != "free" else None,
                "is_admin": bool(user.is_admin), "has_password": bool(user.password_hash),
                "provider": user.oauth_provider, "limits": _limits(t)}

    def _limits(t: tiers.Tier) -> dict:
        return {"jobs_per_12h": t.jobs_per_window, "max_minutes": t.max_duration // 60, "batch_size": t.batch_size,
                "trim_video": t.trim_video, "audio_tracks": t.audio_tracks, "enforced": settings.quotas_enabled}

    @app.post("/api/v1/auth/register", status_code=201)
    async def register(body: Credentials, request: Request, response: Response):
        if not settings.registration_open:
            raise HTTPException(403, "Registration is closed")
        if not await ratelimit.allow(app.state.redis, f"reg:{client_ip(request)}", 5, 3600):
            raise HTTPException(429, "Too many sign-ups from this address")
        email = _email(body.email)
        if len(body.password) < 10:
            raise HTTPException(422, "Password must be at least 10 characters")
        try:
            user = await app.state.db.add_user(email=email, password_hash=auth.hash_password(body.password))
        except IntegrityError:
            raise HTTPException(409, "An account with this email already exists") from None
        set_cookie(response, user)
        return view(user)

    @app.post("/api/v1/auth/login")
    async def login(body: Credentials, request: Request, response: Response):
        try:
            email = _email(body.email)  # the same normalisation as registration, or some users could never sign in
        except HTTPException:
            email = body.email.strip().lower()
        ip = client_ip(request)
        # Three layers: one address+account pair, one address across all accounts (credential stuffing), and one account
        # from anywhere (a distributed guess). The per-account cap is generous so it cannot be used to lock a victim out.
        for bucket, limit, window in ((f"login:{ip}:{email}", 10, 900), (f"loginip:{ip}", 40, 900), (f"loginacct:{email}", 60, 3600)):
            if not await ratelimit.allow(app.state.redis, bucket, limit, window):
                raise HTTPException(429, "Too many attempts, try again in a few minutes")
        user = await app.state.db.user_by_email(email)
        if not auth.verify_password(user.password_hash if user else None, body.password) or user is None:
            raise HTTPException(401, "Incorrect email or password")  # same message for unknown email
        set_cookie(response, user)
        return view(user)

    @app.post("/api/v1/auth/logout", status_code=204)
    async def logout(response: Response):
        response.delete_cookie(auth.COOKIE, path="/")

    @app.post("/api/v1/auth/logout-all", status_code=204)
    async def logout_all(request: Request, response: Response):
        """Sign out everywhere: every session issued so far stops working (e.g. a copied cookie)."""
        user = await require_user(request)
        await app.state.db.bump_epoch(user.id)
        response.delete_cookie(auth.COOKIE, path="/")

    @app.get("/api/v1/me")
    async def me(request: Request):
        user = await require_user(request)
        data = view(user)
        data["used_12h"] = await tiers.used(app.state.redis, tiers.subject(user, client_ip(request)))
        return data

    @app.delete("/api/v1/me", status_code=204)
    async def delete_me(body: DeleteAccount, request: Request, response: Response):
        user = await require_user(request)
        if user.password_hash:
            # Password checks here are as guessable as login, so they share a limit (a stolen cookie must not be an oracle).
            if not await ratelimit.allow(app.state.redis, f"pwcheck:{user.id}", 10, 900):
                raise HTTPException(429, "Too many attempts, try again in a few minutes")
            if not auth.verify_password(user.password_hash, body.password or ""):
                raise HTTPException(403, "Password is incorrect")
        await app.state.db.delete_user(user.id)
        response.delete_cookie(auth.COOKIE, path="/")

    @app.get("/api/v1/me/history")
    async def history(request: Request, limit: int = 50):
        user = await require_user(request)
        return await app.state.db.list_history(user.id, max(1, min(limit, 200)))

    @app.delete("/api/v1/me/history", status_code=204)
    async def clear_history(request: Request):
        user = await require_user(request)
        await app.state.db.clear_history(user.id)

    @app.get("/api/v1/auth/providers")
    async def providers():
        return {"password": settings.registration_open, "oauth": oauth.enabled(settings)}

    def callback_url(request: Request, provider: str) -> str:
        base = (settings.public_url or str(request.base_url)).rstrip("/")
        return f"{base}/api/v1/auth/oauth/{provider}/callback"

    @app.get("/api/v1/auth/oauth/{provider}/login")
    async def oauth_login(provider: str, request: Request):
        if provider not in oauth.enabled(settings):
            raise HTTPException(404, "Unknown sign-in provider")
        return await getattr(oauth_client, provider).authorize_redirect(request, callback_url(request, provider))

    @app.get("/api/v1/auth/oauth/{provider}/callback")
    async def oauth_callback(provider: str, request: Request):
        if provider not in oauth.enabled(settings):
            raise HTTPException(404, "Unknown sign-in provider")
        try:
            profile = await oauth.fetch_profile(oauth_client, provider, request)
        except Exception:  # noqa: BLE001 - state mismatch, denied consent, provider outage
            log(logger, "oauth callback failed", logging.WARNING, provider=provider)
            return RedirectResponse("/account/?error=oauth", status_code=303)
        if not profile.email or not profile.email_verified:
            return RedirectResponse("/account/?error=unverified_email", status_code=303)
        db = app.state.db
        user = await db.user_by_oauth(provider, profile.sub)
        if user is None:
            existing = await db.user_by_email(profile.email)  # linking is safe: the provider verified this email
            if existing and existing.password_hash:
                # Pre-hijack defence: someone else may have registered this address with a password they know.
                await db.take_over_for_oauth(existing.id, provider, profile.sub)
                user = await db.user_by_id(existing.id)
                log(logger, "oauth took over a password account", logging.WARNING, user=existing.id, provider=provider)
            elif existing:
                await db.link_oauth(existing.id, provider, profile.sub)
                user = existing
            elif not settings.registration_open:
                return RedirectResponse("/account/?error=closed", status_code=303)
            else:
                user = await db.add_user(email=profile.email, oauth_provider=provider, oauth_sub=profile.sub)
        response = RedirectResponse("/", status_code=303)
        set_cookie(response, user)
        return response


_SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")


def origin_allowed(request: Request, settings: Settings) -> bool:
    """CSRF defence for cookie auth: browsers send Origin on cross-site writes; it must match us or be allow-listed."""
    origin = request.headers.get("origin")
    if not origin:
        return True  # non-browser clients (curl, SDKs) send no Origin
    if origin == "null":
        return False
    host = urlsplit(origin).netloc
    if not host or not _SAFE_HOST.match(host):
        return False
    expected = {request.headers.get("host", ""), urlsplit(settings.public_url or "").netloc}
    return host in expected or origin in settings.allowed_origins
