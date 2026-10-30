"""Public abuse/DMCA reporting and the admin takedown tools."""

import logging
import time
from typing import Literal

from email_validator import EmailNotValidError, validate_email
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .. import blocklist, ratelimit, tiers
from ..billing.service import CheckoutRefused
from ..blocklist import Blocklist
from ..config import Settings
from ..logging_setup import log

logger = logging.getLogger("hotload.admin")
RULE_TTL = 30  # seconds a process may use a cached copy of the blocklist


class ReportIn(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    kind: Literal["copyright", "abuse", "csam", "other"]
    contact: str = Field(max_length=320)
    details: str = Field(default="", max_length=2000)


class BlockIn(BaseModel):
    kind: Literal["host", "url", "media"]
    value: str = Field(min_length=1, max_length=2048)
    reason: str = Field(default="", max_length=200)


class ResolveIn(BaseModel):
    status: Literal["actioned", "rejected"]
    block: BlockIn | None = None  # take the content down in the same step


class TierIn(BaseModel):
    email: str = Field(max_length=320)
    tier: Literal["free", "premium", "pro"]
    days: int | None = Field(default=None, ge=1, le=3660)  # None = no expiry


class PaymentAction(BaseModel):
    note: str = Field(default="", max_length=250)
    through_provider: bool = False  # refund only: have the provider move the money (needs API support)


class RuleCache:
    """Per-process snapshot of the blocklist, refreshed every RULE_TTL seconds and on any admin change."""

    def __init__(self, db):
        self.db, self._rules, self._at = db, Blocklist(), 0.0

    async def get(self) -> Blocklist:
        if time.monotonic() - self._at > RULE_TTL:
            self._rules, self._at = Blocklist(await self.db.blocked_items()), time.monotonic()
        return self._rules

    def invalidate(self) -> None:
        self._at = 0.0


def add_admin_routes(app: FastAPI, settings: Settings, client_ip) -> None:
    async def require_admin(request: Request):
        if request.headers.get("authorization", "").lower().startswith("bearer hl_"):
            raise HTTPException(403, "Admin tools need a signed-in session")  # never reachable with an API key
        user = await app.state.current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in required")
        if not user.is_admin:
            raise HTTPException(403, "Admins only")
        return user

    @app.post("/api/v1/reports", status_code=201)
    async def submit_report(body: ReportIn, request: Request):
        # Validate first: a typo must not use up the hourly allowance. Only well-formed reports are rate limited.
        if not body.url.lower().startswith(("http://", "https://")):
            raise HTTPException(422, "The link must start with http:// or https://")
        try:
            contact = validate_email(body.contact.strip(), check_deliverability=False).normalized
        except EmailNotValidError as exc:
            raise HTTPException(422, "Enter a valid contact email") from exc
        if not await ratelimit.allow(app.state.redis, f"report:{client_ip(request)}", 3, 3600):
            raise HTTPException(429, "Too many reports from this address. Email us if this is urgent.")
        report = await app.state.db.add_report(url=body.url.strip(), kind=body.kind, contact=contact,
                                               details=body.details.strip())
        # The reported URL is stored, never fetched. CSAM reports are logged at ERROR so alerting picks them up.
        log(logger, "report received", logging.ERROR if body.kind == "csam" else logging.WARNING,
            report=report.id, kind=body.kind)
        return {"id": report.id}

    @app.get("/api/v1/admin/overview")
    async def overview(request: Request):
        await require_admin(request)
        return {**await app.state.db.counts(), "blocked": len(await app.state.db.blocked_items())}

    @app.get("/api/v1/admin/reports")
    async def reports(request: Request, status: str | None = None):
        await require_admin(request)
        return await app.state.db.list_reports(status if status in ("open", "actioned", "rejected") else None)

    @app.get("/api/v1/admin/blocklist")
    async def list_block(request: Request):
        await require_admin(request)
        return [b.public() for b in await app.state.db.blocked_items()]

    async def add_block(admin, body: BlockIn):
        value = blocklist.normalise_value(body.kind, body.value)
        if not value or (body.kind == "media" and ":" not in value):
            raise HTTPException(422, 'Media rules look like "Youtube:VIDEO_ID"')
        item = await app.state.db.add_block(body.kind, value, body.reason, admin.id)
        app.state.rules.invalidate()
        log(logger, "block added", admin=admin.id, kind=body.kind, value=value[:200], new=item is not None)
        return item

    @app.post("/api/v1/admin/blocklist", status_code=201)
    async def create_block(body: BlockIn, request: Request):
        admin = await require_admin(request)
        item = await add_block(admin, body)
        if item is None:
            raise HTTPException(409, "Already blocked")
        return item.public()

    @app.delete("/api/v1/admin/blocklist/{block_id}", status_code=204)
    async def delete_block(block_id: int, request: Request):
        admin = await require_admin(request)
        if not await app.state.db.remove_block(block_id):
            raise HTTPException(404, "Rule not found")
        app.state.rules.invalidate()
        log(logger, "block removed", admin=admin.id, rule=block_id)

    @app.post("/api/v1/admin/reports/{report_id}/resolve")
    async def resolve(report_id: str, body: ResolveIn, request: Request):
        admin = await require_admin(request)
        if body.block:
            await add_block(admin, body.block)
        result = await app.state.db.resolve_report(report_id, body.status)
        if result is None:
            raise HTTPException(404, "Report not found")
        log(logger, "report resolved", admin=admin.id, report=report_id, status=body.status, blocked=bool(body.block))
        return result

    # --- payments that need a human: held (paid but not granted), disputed, and the refund/chargeback actions
    @app.get("/api/v1/admin/payments")
    async def payments_to_review(request: Request, status: str = "held,disputed"):
        await require_admin(request)
        wanted = tuple(x for x in status.split(",") if x in ("held", "disputed", "refunded", "chargedback", "paid"))
        return await app.state.db.payments_needing_attention(wanted or ("held", "disputed"))

    @app.get("/api/v1/admin/payments/{payment_id}")
    async def payment_detail(payment_id: str, request: Request):
        await require_admin(request)
        payment = await app.state.db.get_payment(payment_id)
        if payment is None:
            raise HTTPException(404, "Payment not found")
        user = await app.state.db.user_by_id(payment.user_id)
        return {**payment.public(), "email": user.email if user else None, "events": await app.state.db.payment_events(payment_id)}

    async def act(request: Request, payment_id: str, action: str, body: PaymentAction):
        admin = await require_admin(request)
        service = app.state.billing
        payment = await app.state.db.get_payment(payment_id)
        if payment is None:
            raise HTTPException(404, "Payment not found")
        # One action at a time per payment: two clicks must not ask the provider to refund twice.
        lock = f"paylock:{payment_id}"
        if not await app.state.redis.set(lock, "1", nx=True, ex=60):
            raise HTTPException(409, "Another action on this payment is in progress")
        try:
            if action == "refund":
                try:
                    result = await service.refund(payment, admin.id, body.note, body.through_provider)
                except CheckoutRefused as exc:
                    raise HTTPException(exc.status, str(exc)) from exc
            else:
                result = await service.reverse(payment_id, action, admin.id, body.note)
        finally:
            await app.state.redis.delete(lock)
        if not result["ok"]:
            raise HTTPException(409, result.get("error") or "Not allowed")
        log(logger, "payment action", admin=admin.id, payment=payment_id, action=action, status=result["status"],
            entitlement=result["entitlement"])
        return result

    @app.post("/api/v1/admin/payments/{payment_id}/refund")
    async def refund_payment(payment_id: str, body: PaymentAction, request: Request):
        return await act(request, payment_id, "refund", body)

    @app.post("/api/v1/admin/payments/{payment_id}/dispute")
    async def dispute_payment(payment_id: str, body: PaymentAction, request: Request):
        return await act(request, payment_id, "dispute", body)

    @app.post("/api/v1/admin/payments/{payment_id}/dispute-won")
    async def dispute_won(payment_id: str, body: PaymentAction, request: Request):
        return await act(request, payment_id, "dispute_won", body)

    @app.post("/api/v1/admin/payments/{payment_id}/chargeback")
    async def chargeback(payment_id: str, body: PaymentAction, request: Request):
        return await act(request, payment_id, "chargeback", body)

    @app.post("/api/v1/admin/users/tier")
    async def grant_tier(body: TierIn, request: Request):
        admin = await require_admin(request)
        user = await app.state.db.user_by_email(body.email.strip().lower())
        if user is None:
            raise HTTPException(404, "No account with that email")
        expires = time.time() + body.days * 86400 if body.days else None
        await app.state.db.set_tier(user.id, body.tier, expires if body.tier != "free" else None)
        log(logger, "tier granted", admin=admin.id, user=user.id, tier=body.tier, days=body.days)
        fresh = await app.state.db.user_by_id(user.id)
        return {"email": fresh.email, "tier": tiers.effective_name(fresh), "tier_expires": fresh.tier_expires}
