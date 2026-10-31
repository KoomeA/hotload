"""Provider interface. Adding a processor (CCBill, Segpay, NOWPayments...) means one module implementing this."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Checkout:
    url: str
    provider_ref: str | None = None


@dataclass
class Verified:
    """The provider's own answer about a payment, fetched server-side (never taken from a webhook body)."""

    status: str  # paid | pending | failed | expired
    amount_minor: int = 0
    currency: str = ""


class ProviderError(Exception):
    """The provider rejected a request or was unreachable."""


class Provider(Protocol):
    name: str

    async def create_checkout(self, payment, email: str, label: str, return_url: str) -> Checkout: ...

    def check_signature(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def reference(self, body: bytes) -> tuple[str | None, str | None]:
        """Return (our payment id, provider ref) named in a webhook, or (None, None) for events we ignore."""

    async def verify(self, payment) -> Verified: ...


class SupportsRefunds(Protocol):
    """Optional: providers that can refund through their API, and/or report refunds and disputes by webhook."""

    async def refund(self, payment) -> str:
        """Refund the payment in full at the provider. Returns the provider's refund reference."""

    def reversal(self, body: bytes) -> tuple[str, str] | None:
        """(kind, our payment id) for a signed webhook about a refund or dispute, else None.
        kind is "refund", "dispute_opened" or "dispute_resolved"."""
