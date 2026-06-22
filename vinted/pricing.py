"""Brand-based pricing rules for Vinted SE listings.

Rule (set by owner 2026-06-21): Tommy Hilfiger = 200, Polo Sport / Ralph Lauren
family = 300. Unknown brands -> use Vinted's PREMIUM suggestion (the maximum of
the price band) unless overridden.
"""

from __future__ import annotations

from typing import Any, Optional

BRAND_PRICE: dict[str, int] = {
    "Tommy Hilfiger": 200,
    "Polo Sport": 300,
    "Polo Sport Ralph Lauren": 300,
    "Polo Ralph Lauren": 300,
    "Ralph Lauren": 300,
    "Lauren Ralph Lauren": 300,
    "Ralph Lauren Sport": 300,
}


def premium_from_suggestion(suggestion: dict[str, Any]) -> Optional[int]:
    """Return the premium (max) price from a pricing_suggestions response."""
    ps = (suggestion or {}).get("price_suggestion") or {}
    amount = (ps.get("maximum") or ps.get("price") or {}).get("amount")
    try:
        return int(round(float(amount)))
    except (TypeError, ValueError):
        return None


def resolve_price(
    brand: Optional[str], suggestion: Optional[dict[str, Any]] = None
) -> Optional[int]:
    """Brand rule first; otherwise Vinted's premium suggestion."""
    if brand and brand in BRAND_PRICE:
        return BRAND_PRICE[brand]
    if suggestion is not None:
        return premium_from_suggestion(suggestion)
    return None
