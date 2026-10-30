"""What can be bought. Passes grant a plan for a fixed number of days: every rail (cards, M-Pesa, crypto) can pay for one."""

from dataclasses import dataclass
from decimal import Decimal

from ..config import Settings

TIER_RANK = {"anonymous": 0, "free": 0, "premium": 1, "pro": 2}


@dataclass(frozen=True)
class Plan:
    key: str
    tier: str
    days: int
    label: str


PLANS: dict[str, Plan] = {
    p.key: p
    for p in (
        Plan("premium_30", "premium", 30, "Premium, 30 days"),
        Plan("premium_365", "premium", 365, "Premium, 1 year"),
        Plan("pro_30", "pro", 30, "Pro, 30 days"),
        Plan("pro_365", "pro", 365, "Pro, 1 year"),
    )
}

# Minor units (cents). Only USD is defaulted: other currencies need operator-chosen prices, never guessed rates.
DEFAULT_PRICES: dict[str, dict[str, int]] = {
    "USD": {"premium_30": 999, "premium_365": 8399, "pro_30": 2999, "pro_365": 23999},
}

# Currencies whose minor unit is not 1/100 (extend as needed when adding prices for them).
_EXPONENT = {
    **dict.fromkeys(("JPY", "KRW", "UGX", "RWF", "XOF", "XAF", "CLP", "VND", "BIF", "DJF", "GNF", "KMF", "MGA", "PYG", "VUV", "XPF"), 0),
    **dict.fromkeys(("BHD", "KWD", "OMR", "JOD", "TND", "IQD", "LYD"), 3),
}


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def to_major(amount_minor: int, currency: str) -> str:
    """9990 + USD -> "99.90" (exact decimal string, never a float)."""
    e = exponent(currency)
    return format(Decimal(amount_minor).scaleb(-e), f".{e}f")


def to_minor(amount_major: str | int | float | Decimal, currency: str) -> int:
    return int((Decimal(str(amount_major)).scaleb(exponent(currency))).to_integral_value())


def prices(settings: Settings) -> dict[str, dict[str, int]]:
    merged = {cur: dict(p) for cur, p in DEFAULT_PRICES.items()}
    for cur, p in settings.prices.items():
        merged.setdefault(cur.upper(), {}).update({k: v for k, v in p.items() if k in PLANS and v > 0})
    return merged


def price(settings: Settings, plan: str, currency: str) -> int | None:
    return prices(settings).get(currency.upper(), {}).get(plan)


def entitlement_after(current_tier: str, current_expires: float | None, plan: Plan, now: float) -> tuple[str, float | None]:
    """The (tier, expiry) a user ends up with after a plan is paid for.

    - Same plan still active: extends from the current expiry (renewing early loses nothing).
    - Higher plan than the active one: switches immediately for the full period (no proration).
    - Lower plan than the active one: NOT handled here. `Database.complete_payment` holds such a payment for review
      instead of calling this, because extending the higher plan would sell its days at the lower plan's price.
    - Manual perpetual grants (expiry None) stay perpetual.
    """
    active = current_tier not in ("free", "anonymous") and (current_expires is None or current_expires > now)
    seconds = plan.days * 86400
    if active and TIER_RANK[current_tier] >= TIER_RANK[plan.tier]:
        return current_tier, None if current_expires is None else current_expires + seconds
    return plan.tier, now + seconds
