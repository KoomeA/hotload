import logging
import time

import httpx

from ..config import Settings
from ..db import Database, Payment
from ..logging_setup import log
from .base import Provider, ProviderError, Verified
from .btcpay import BTCPay
from .catalog import PLANS, TIER_RANK, price
from .paystack import Paystack

logger = logging.getLogger("hotload.billing")
PENDING_MAX_AGE = 24 * 3600


class CheckoutRefused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def build_providers(settings: Settings, client: httpx.AsyncClient) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    if Paystack.enabled(settings):
        providers["paystack"] = Paystack(settings, client)
    if BTCPay.enabled(settings):
        providers["btcpay"] = BTCPay(settings, client)
    return providers


class BillingService:
    def __init__(self, settings: Settings, db: Database, providers: dict[str, Provider]):
        self.settings, self.db, self.providers = settings, db, providers

    async def start_checkout(self, user, effective_tier: str, plan_key: str, provider: str, currency: str,
                             return_base: str) -> tuple[Payment, str]:
        """`return_base` is the page the buyer returns to; the payment id is appended so that page can look it up."""
        plan = PLANS.get(plan_key)
        if plan is None:
            raise CheckoutRefused("Unknown plan")
        prov = self.providers.get(provider)
        if prov is None:
            raise CheckoutRefused("That payment method is not available")
        currency = currency.upper()
        amount = price(self.settings, plan_key, currency)
        if amount is None:
            raise CheckoutRefused(f"{plan.label} is not sold in {currency}")
        if TIER_RANK.get(effective_tier, 0) > TIER_RANK[plan.tier]:
            raise CheckoutRefused("You already have a higher plan active. Buy this plan after it ends.", 409)
        payment = await self.db.create_payment(user_id=user.id, provider=provider, plan=plan_key,
                                               amount_minor=amount, currency=currency)
        try:
            checkout = await prov.create_checkout(payment, user.email, plan.label, f"{return_base}{payment.id}")
        except ProviderError as exc:
            await self.db.close_payment(payment.id, "failed")
            log(logger, "checkout failed", logging.WARNING, payment=payment.id, provider=provider, error=str(exc))
            raise CheckoutRefused("The payment provider could not start the checkout. Please try again.", 502) from exc
        if checkout.provider_ref:
            await self.db.set_provider_ref(payment.id, checkout.provider_ref)
        log(logger, "checkout started", payment=payment.id, provider=provider, plan=plan_key, currency=currency)
        return payment, checkout.url

    async def apply(self, payment: Payment, verified: Verified) -> Payment:
        """Act on the provider's verified answer. Idempotent."""
        if payment.status != "pending":
            return payment
        if verified.status == "paid":
            if verified.amount_minor < payment.amount_minor or verified.currency != payment.currency:
                # Never grant for an amount or currency we did not ask for; leave it pending for a human to look at.
                log(logger, "payment amount mismatch", logging.ERROR, payment=payment.id, expected=payment.amount_minor,
                    got=verified.amount_minor, currency=verified.currency)
                return payment
            if await self.db.complete_payment(payment.id):
                log(logger, "payment completed", payment=payment.id, provider=payment.provider, plan=payment.plan)
        elif verified.status in ("failed", "expired"):
            await self.db.close_payment(payment.id, verified.status)
        elif payment.created < time.time() - PENDING_MAX_AGE:
            await self.db.close_payment(payment.id, "expired")
        return await self.db.get_payment(payment.id)

    async def refresh(self, payment: Payment) -> Payment:
        """Ask the provider about a pending payment (used by the return page, so slow webhooks do not matter)."""
        if payment.status != "pending":
            return payment
        prov = self.providers.get(payment.provider)
        if prov is None:
            return payment
        try:
            return await self.apply(payment, await prov.verify(payment))
        except ProviderError as exc:
            log(logger, "verify failed", logging.WARNING, payment=payment.id, error=str(exc))
            return payment

    async def handle_webhook(self, provider: str, headers, body: bytes) -> str:
        """Returns 'bad-signature', 'ignored' or 'ok'. Signature first, then re-verification with the provider."""
        prov = self.providers.get(provider)
        if prov is None or not prov.check_signature(headers, body):
            return "bad-signature"
        payment_id, ref = prov.reference(body)
        payment = None
        if payment_id:
            payment = await self.db.get_payment(payment_id)
        elif ref:
            payment = await self.db.payment_by_provider_ref(provider, ref)
        if payment is None or payment.provider != provider:
            return "ignored"
        await self.refresh(payment)
        return "ok"
