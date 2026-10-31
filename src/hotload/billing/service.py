import logging
import time

import httpx

from ..config import Settings
from ..db import PAYMENT_SETTLED, Database, Payment
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
        allowed = getattr(prov, "currencies", None)
        if allowed is not None and currency not in allowed:
            raise CheckoutRefused(f"{prov.name} cannot take {currency} payments")
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
        if payment.status in PAYMENT_SETTLED:
            return payment
        if verified.status == "paid":
            if verified.amount_minor < payment.amount_minor or verified.currency != payment.currency:
                # Never grant for an amount or currency we did not ask for; leave it for a human to look at.
                log(logger, "payment amount mismatch", logging.ERROR, payment=payment.id, expected=payment.amount_minor,
                    got=verified.amount_minor, currency=verified.currency)
                return payment
            outcome = await self.db.complete_payment(payment.id)
            if outcome == "granted":
                late = payment.status != "pending"
                log(logger, "payment completed", logging.ERROR if late else logging.INFO, payment=payment.id,
                    provider=payment.provider, plan=payment.plan, paid_after_close=late)
            elif outcome == "held":
                log(logger, "payment HELD: money received but not granted (higher plan active or account gone): "
                    "refund or credit it", logging.ERROR, payment=payment.id, provider=payment.provider, plan=payment.plan)
        elif payment.status != "pending":
            return payment  # an already closed payment that is still unpaid stays closed
        elif verified.status in ("failed", "expired"):
            await self.db.close_payment(payment.id, verified.status)
        elif payment.created < time.time() - PENDING_MAX_AGE:
            await self.db.close_payment(payment.id, "expired")
        return await self.db.get_payment(payment.id)

    async def _reversal_event(self, provider: str, kind: str, payment_id: str) -> str:
        """A signed provider notice about a refund or dispute. The signature proves who sent it. The automatic effects are
        the safe ones: a refund the provider already made is recorded, and a new dispute suspends the plan. How a dispute
        ended is NOT acted on automatically (outcome wording differs between processors): it is logged for a human."""
        payment = await self.db.get_payment(payment_id)
        if payment is None or payment.provider != provider:
            return "ignored"
        actor = f"provider:{provider}"
        if kind == "refund":
            await self.reverse(payment.id, "refund", actor, "reported by the provider")
        elif kind == "dispute_opened":
            await self.reverse(payment.id, "dispute", actor, "opened by the customer's bank")
        else:
            await self.db.record_payment_event(payment.id, "dispute_resolved", actor, "provider reports a resolution")
            log(logger, "dispute RESOLVED by provider: check the outcome and confirm it (win or chargeback) in the admin console",
                logging.ERROR, payment=payment.id, provider=provider)
        return "ok"

    async def refresh(self, payment: Payment) -> Payment:
        """Ask the provider about an unsettled payment (used by the return page, so slow webhooks do not matter).

        Closed (expired/failed) payments are re-checked too: a customer can still pay a BTCPay invoice late or finish a
        Paystack checkout after our 24 h window, and that money must not vanish."""
        if payment.status in PAYMENT_SETTLED:
            return payment
        prov = self.providers.get(payment.provider)
        if prov is None:
            return payment
        try:
            return await self.apply(payment, await prov.verify(payment))
        except ProviderError as exc:
            log(logger, "verify failed", logging.WARNING, payment=payment.id, error=str(exc))
            return payment

    async def reverse(self, payment_id: str, action: str, actor: str, note: str = "") -> dict:
        """Refund / dispute / dispute won / chargeback bookkeeping (Database.reverse_payment), with the alerts."""
        result = await self.db.reverse_payment(payment_id, action, actor, note)
        if result["changed"]:
            level = logging.INFO if action in ("refund", "dispute_won") else logging.ERROR
            log(logger, f"payment {action.upper().replace('_', ' ')}", level, payment=payment_id, actor=actor,
                status=result["status"], entitlement=result["entitlement"])
        return result

    async def refund(self, payment: Payment, actor: str, note: str, through_provider: bool) -> dict:
        """Refund a payment. With `through_provider` the provider moves the money first; without it the operator is
        recording a refund they already made elsewhere (BTCPay, a processor dashboard, a bank transfer)."""
        if payment.status not in ("paid", "held", "disputed"):
            if payment.status == "refunded":
                return await self.reverse(payment.id, "refund", actor, note)  # idempotent answer
            raise CheckoutRefused(f"A {payment.status} payment cannot be refunded", 409)
        ref = ""
        if through_provider:
            prov = self.providers.get(payment.provider)
            if prov is None:
                raise CheckoutRefused(f"{payment.provider} is not configured on this server, so it cannot be asked to refund. "
                                      "Refund where you hold the money, then record it here without the provider option", 400)
            if not hasattr(prov, "refund"):
                raise CheckoutRefused(f"{payment.provider} cannot refund through its API: refund in its dashboard, then "
                                      "record it here without the provider option", 400)
            try:
                ref = await prov.refund(payment)
            except ProviderError as exc:
                log(logger, "provider refund failed", logging.WARNING, payment=payment.id, error=str(exc))
                raise CheckoutRefused(f"The provider refused the refund: {exc}", 502) from exc
        return await self.reverse(payment.id, "refund", actor, f"{note} [provider refund {ref}]" if ref else note)

    async def handle_webhook(self, provider: str, headers, body: bytes) -> str:
        """Returns 'bad-signature', 'ignored' or 'ok'. Signature first, then re-verification with the provider."""
        prov = self.providers.get(provider)
        if prov is None or not prov.check_signature(headers, body):
            return "bad-signature"
        event = prov.reversal(body) if hasattr(prov, "reversal") else None
        if event:
            return await self._reversal_event(provider, *event)
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
