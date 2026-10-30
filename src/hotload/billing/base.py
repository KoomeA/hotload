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
