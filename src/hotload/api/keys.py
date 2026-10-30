"""API keys, usage metering and key authentication (Pro plan)."""

import time
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .. import auth, tiers
from ..config import Settings

MAX_KEYS = 10
USAGE_DAYS = 90


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def _day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), UTC).strftime("%Y%m%d")


async def record_usage(redis, user_id: str, key_id: str | None, field: str, amount: int = 1) -> None:
    """Per-day counters for the account and (if used) the key. They expire after USAGE_DAYS."""
    for subject in filter(None, (f"u:{user_id}", f"k:{key_id}" if key_id else None)):
        k = f"hotload:usage:{subject}:{_day()}"
        await redis.hincrby(k, field, amount)
        await redis.expire(k, USAGE_DAYS * 86400)


async def read_usage(redis, subject: str, days: int) -> list[dict]:
    out = []
    for back in range(days):
        day = _day(time.time() - back * 86400)
        data = await redis.hgetall(f"hotload:usage:{subject}:{day}")
        if data:
            out.append({"day": f"{day[:4]}-{day[4:6]}-{day[6:]}", **{k: int(v) for k, v in data.items()}})
    return out


def add_key_routes(app: FastAPI, settings: Settings) -> None:
    async def authenticate(request: Request):
        """Return (user, key) for a request. A bad API key is a hard 401; it never falls back to the cookie."""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer hl_"):
            parsed = auth.parse_api_key(header[7:].strip())
            row = await app.state.db.get_key(parsed[0]) if parsed else None
            if not parsed or row is None or row.revoked or not auth.key_matches(row.key_hash, parsed[1]):
                raise HTTPException(401, "Invalid API key")
            user = await app.state.db.user_by_id(row.user_id)
            if user is None:
                raise HTTPException(401, "Invalid API key")
            if settings.quotas_enabled and tiers.effective_name(user) != "pro":
                raise HTTPException(403, "API access needs an active Pro plan")
            if not row.last_used or time.time() - row.last_used > 60:  # avoid a DB write per request
                await app.state.db.touch_key(row.id, time.time())
            return user, row
        return await app.state.current_user(request), None

    app.state.authenticate = authenticate

    async def session_user(request: Request):
        """Key management needs a signed-in browser session, never an API key (a leaked key must not mint more)."""
        if request.headers.get("authorization", "").lower().startswith("bearer hl_"):
            raise HTTPException(403, "Manage API keys from a signed-in session")
        user = await app.state.current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in required")
        return user

    @app.post("/api/v1/me/keys", status_code=201)
    async def create_key(body: NewKey, request: Request):
        user = await session_user(request)
        if settings.quotas_enabled and tiers.effective_name(user) != "pro":
            raise HTTPException(403, "API keys need an active Pro plan")
        if settings.require_verified_email and not user.email_verified:
            raise HTTPException(403, "Confirm your email address first")
        if len(await app.state.db.list_keys(user.id)) >= MAX_KEYS:
            raise HTTPException(409, f"At most {MAX_KEYS} active keys")
        full, key_id, secret_hash, webhook_secret = auth.new_api_key()
        row = await app.state.db.create_key(user.id, body.name.strip(), secret_hash, webhook_secret, key_id)
        # The only time the key and the webhook secret are ever shown.
        return {**row.public(), "key": full, "webhook_secret": webhook_secret}

    @app.get("/api/v1/me/keys")
    async def list_keys(request: Request):
        user = await session_user(request)
        return [k.public() for k in await app.state.db.list_keys(user.id)]

    @app.delete("/api/v1/me/keys/{key_id}", status_code=204)
    async def revoke_key(key_id: str, request: Request):
        user = await session_user(request)
        if not await app.state.db.revoke_key(user.id, key_id):
            raise HTTPException(404, "Key not found")

    @app.get("/api/v1/me/usage")
    async def usage(request: Request, days: int = 30):
        user, key = await authenticate(request)
        if user is None:
            raise HTTPException(401, "Sign in required")
        days = max(1, min(days, USAGE_DAYS))
        return {"account": await read_usage(app.state.redis, f"u:{user.id}", days),
                "key": await read_usage(app.state.redis, f"k:{key.id}", days) if key else None}
