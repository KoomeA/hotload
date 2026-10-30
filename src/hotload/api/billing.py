"""Pricing, checkout and payment-provider webhooks."""

import logging

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .. import ratelimit, tiers
from ..billing import catalog
from ..billing.service import BillingService, CheckoutRefused
from ..config import Settings
from ..logging_setup import log

logger = logging.getLogger("hotload.billing")


class CheckoutRequest(BaseModel):
    plan: str = Field(max_length=24)
    provider: str = Field(max_length=16)
    currency: str = Field(min_length=3, max_length=3)


def add_billing_routes(app: FastAPI, settings: Settings, client_ip) -> None:
    def service() -> BillingService:
        return app.state.billing

    async def require_user(request: Request):
        user = await app.state.current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in required")
        return user

    @app.get("/api/v1/billing/plans")
    async def plans():
        """What can be bought, and through which methods (only methods that are configured are listed)."""
        offers = []
        for plan in catalog.PLANS.values():
            options = [{"provider": name, "currency": cur, "amount_minor": amount}
                       for name in service().providers
                       for cur, table in catalog.prices(settings).items()
                       if (amount := table.get(plan.key))]
            if options:
                offers.append({"plan": plan.key, "tier": plan.tier, "days": plan.days, "label": plan.label,
                               "options": options})
        return {"providers": sorted(service().providers), "plans": offers}

    @app.post("/api/v1/billing/checkout", status_code=201)
    async def checkout(body: CheckoutRequest, request: Request):
        user = await require_user(request)
        if not await ratelimit.allow(app.state.redis, f"checkout:{user.id}", 6, 3600):
            raise HTTPException(429, "Too many checkout attempts, try again later")
        base = (settings.public_url or str(request.base_url)).rstrip("/")
        try:
            payment, url = await service().start_checkout(
                user, tiers.effective_name(user), body.plan, body.provider, body.currency, f"{base}/account/?payment=")
        except CheckoutRefused as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return {"payment_id": payment.id, "url": url}

    async def owned(request: Request, payment_id: str):
        user = await require_user(request)
        payment = await app.state.db.get_payment(payment_id)
        if payment is None or payment.user_id != user.id:
            raise HTTPException(404, "Payment not found")
        return payment

    @app.get("/api/v1/billing/payments")
    async def payments(request: Request):
        user = await require_user(request)
        return await app.state.db.list_payments(user.id)

    @app.get("/api/v1/billing/payments/{payment_id}")
    async def payment_status(payment_id: str, request: Request):
        payment = await service().refresh(await owned(request, payment_id))
        return payment.public()

    @app.post("/api/v1/billing/webhooks/{provider}")
    async def webhook(provider: str, request: Request):
        body = await request.body()  # raw bytes: the signature covers exactly these
        if len(body) > 64_000:
            raise HTTPException(413, "Payload too large")
        result = await service().handle_webhook(provider, request.headers, body)
        if result == "bad-signature":
            log(logger, "webhook rejected", logging.WARNING, provider=provider, ip=client_ip(request))
            raise HTTPException(401, "Invalid signature")
        return {"ok": True}
