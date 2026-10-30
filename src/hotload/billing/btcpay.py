"""BTCPay Server (self-hosted, non-custodial crypto). Uses the Greenfield API.

Confirm against your own instance's /docs and a staging store before launch (see docs/PAYMENTS.md).
"""

import hashlib
import hmac
import json
from collections.abc import Mapping

import httpx

from ..config import Settings
from .base import Checkout, ProviderError, Verified
from .catalog import to_major, to_minor


class BTCPay:
    name = "btcpay"

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.base = (settings.btcpay_url or "").rstrip("/")
        self.store = settings.btcpay_store_id or ""
        self.key = settings.btcpay_api_key or ""
        self.secret = settings.btcpay_webhook_secret or ""
        self.client = client

    @staticmethod
    def enabled(settings: Settings) -> bool:
        return all((settings.btcpay_url, settings.btcpay_store_id, settings.btcpay_api_key, settings.btcpay_webhook_secret))

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            r = await self.client.request(method, f"{self.base}/api/v1/stores/{self.store}{path}",
                                          headers={"Authorization": f"token {self.key}"}, timeout=20, **kwargs)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("BTCPay Server is unreachable") from exc
        if r.status_code >= 400:
            raise ProviderError(str(data.get("message") or f"BTCPay error {r.status_code}")[:200])
        return data

    async def create_checkout(self, payment, email: str, label: str, return_url: str) -> Checkout:
        data = await self._call("POST", "/invoices", json={
            "amount": to_major(payment.amount_minor, payment.currency),
            "currency": payment.currency,
            "metadata": {"orderId": payment.id, "buyerEmail": email, "itemDesc": label},
            "checkout": {"redirectURL": return_url, "expirationMinutes": 60},
        })
        return Checkout(url=data["checkoutLink"], provider_ref=data["id"])

    def check_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        sent = headers.get("btcpay-sig", "")
        expected = "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        return bool(self.secret) and hmac.compare_digest(sent.lower(), expected)

    def reference(self, body: bytes) -> tuple[str | None, str | None]:
        try:
            event = json.loads(body)
        except ValueError:
            return None, None
        invoice = event.get("invoiceId")
        if not isinstance(invoice, str) or not str(event.get("type", "")).startswith("Invoice"):
            return None, None
        return None, invoice  # the invoice is fetched to learn the real status; the webhook body is not trusted

    async def verify(self, payment) -> Verified:
        if not payment.provider_ref:
            raise ProviderError("Payment has no invoice yet")
        inv = await self._call("GET", f"/invoices/{payment.provider_ref}")
        meta = inv.get("metadata") or {}
        if meta.get("orderId") != payment.id:
            raise ProviderError("Invoice does not belong to this payment")
        status = {"Settled": "paid", "Expired": "expired", "Invalid": "failed"}.get(str(inv.get("status")), "pending")
        currency = str(inv.get("currency") or "").upper()
        return Verified(status, to_minor(inv.get("amount") or 0, currency or payment.currency), currency)
