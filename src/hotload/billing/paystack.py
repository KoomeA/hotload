"""Paystack (cards, M-Pesa and bank methods in supported countries).

Checked against third-party documentation only; confirm in Paystack test mode before launch (see docs/PAYMENTS.md).
Paystack's Acceptable Use Policy bars pornography and copyright-infringing items: get written approval first.
"""

import hashlib
import hmac
import json
from collections.abc import Mapping

import httpx

from ..config import Settings
from .base import Checkout, ProviderError, Verified


class Paystack:
    name = "paystack"
    # Only currencies whose smallest unit is 1/100 (amounts are sent in subunits). Others are refused until their
    # subunit handling has been confirmed in Paystack test mode, so a price can never be charged 100x too low.
    currencies = frozenset({"NGN", "GHS", "ZAR", "KES", "USD"})

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.key = settings.paystack_secret_key or ""
        self.base = settings.paystack_base_url.rstrip("/")
        self.client = client

    @staticmethod
    def enabled(settings: Settings) -> bool:
        return bool(settings.paystack_secret_key)

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            r = await self.client.request(method, f"{self.base}{path}", headers=self._auth, timeout=20, **kwargs)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("Paystack is unreachable") from exc
        if r.status_code >= 400 or not data.get("status"):
            raise ProviderError(str(data.get("message") or f"Paystack error {r.status_code}")[:200])
        return data["data"]

    async def create_checkout(self, payment, email: str, label: str, return_url: str) -> Checkout:
        data = await self._call("POST", "/transaction/initialize", json={
            "email": email, "amount": payment.amount_minor, "currency": payment.currency,
            "reference": payment.id, "callback_url": return_url,
            "metadata": {"plan": payment.plan, "label": label, "user_id": payment.user_id},
        })
        return Checkout(url=data["authorization_url"], provider_ref=data.get("access_code"))

    def check_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        sent = headers.get("x-paystack-signature", "")
        expected = hmac.new(self.key.encode(), body, hashlib.sha512).hexdigest()
        return bool(self.key) and hmac.compare_digest(sent.lower().encode("utf-8", "ignore"), expected.encode())

    def reference(self, body: bytes) -> tuple[str | None, str | None]:
        try:
            event = json.loads(body)
        except ValueError:
            return None, None
        if event.get("event") != "charge.success":
            return None, None
        ref = (event.get("data") or {}).get("reference")
        return (ref if isinstance(ref, str) else None), None

    async def refund(self, payment) -> str:
        """Full refund through Paystack's refund API. Unconfirmed against live documentation (see docs/PAYMENTS.md)."""
        data = await self._call("POST", "/refund", json={"transaction": payment.id})
        ref = data.get("id") or data.get("reference") or data.get("transaction_reference") or ""
        return str(ref)[:64]

    def reversal(self, body: bytes) -> tuple[str, str] | None:
        try:
            event = json.loads(body)
        except ValueError:
            return None
        kind = {"refund.processed": "refund", "charge.dispute.create": "dispute_opened",
                "charge.dispute.resolve": "dispute_resolved"}.get(str(event.get("event")))
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        txn = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
        ref = data.get("transaction_reference") or txn.get("reference")  # where each event carries our reference
        return (kind, ref) if kind and isinstance(ref, str) and ref else None

    async def verify(self, payment) -> Verified:
        data = await self._call("GET", f"/transaction/verify/{payment.id}")
        status = {"success": "paid", "failed": "failed", "reversed": "failed"}.get(str(data.get("status")), "pending")
        return Verified(status, int(data.get("amount") or 0), str(data.get("currency") or "").upper())
