"""Pricing rules.

Fixed per-brand prices live in config/markets.json (`brand_prices`, in that
market's currency). Brands without a rule use Vinted's PREMIUM suggestion
(the maximum of the price band) unless a price is given explicitly.
"""

from __future__ import annotations

from typing import Any, Optional


def premium_from_suggestion(suggestion: dict[str, Any]) -> Optional[float]:
    """Return the premium (max) price from a pricing_suggestions response.

    Keeps cents (e.g. 7.5 EUR); whole amounts come back as int.
    """
    ps = (suggestion or {}).get("price_suggestion") or {}
    amount = (ps.get("maximum") or ps.get("price") or {}).get("amount")
    try:
        value = round(float(amount), 2)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def resolve_price(
    brand: Optional[str],
    suggestion: Optional[dict[str, Any]] = None,
    brand_prices: Optional[dict[str, float]] = None,
) -> Optional[float]:
    """Brand rule first; otherwise Vinted's premium suggestion."""
    if brand and brand_prices and brand in brand_prices:
        return brand_prices[brand]
    if suggestion is not None:
        return premium_from_suggestion(suggestion)
    return None


def item_amount(item: dict[str, Any]) -> Optional[float]:
    """Price of a search/ad item (handles {"amount": "3.0"} and plain numbers)."""
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def price_summary(items: list[dict[str, Any]]) -> Optional[dict[str, float]]:
    """{'count', 'min', 'median', 'max'} of asking prices, or None if no prices."""
    prices = sorted(x for x in (item_amount(i) for i in items) if x is not None)
    if not prices:
        return None
    n = len(prices)
    mid = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    return {"count": n, "min": prices[0], "median": round(mid, 2), "max": prices[-1]}
