"""Polymarket taker fee math.

fee_per_share = rate * (p * (1 - p)) ** exponent
Makers pay nothing. Unknown fee rates are treated as the worst category (0.07)
so that expected-value calculations are conservative.
"""

from __future__ import annotations

from decimal import Decimal

from .models import ONE, ZERO, D, Market

WORST_CASE_RATE = Decimal("0.07")


def taker_fee_per_share(price: Decimal, rate: Decimal, exponent: Decimal = ONE) -> Decimal:
    p = D(price)
    if p <= ZERO or p >= ONE or rate <= ZERO:
        return ZERO
    base = p * (ONE - p)
    if exponent == ONE:
        return rate * base
    return rate * Decimal(float(base) ** float(exponent))


def taker_fee(price: Decimal, shares: Decimal, market: Market) -> Decimal:
    rate = market.fee_rate if market.fee_known else WORST_CASE_RATE
    return taker_fee_per_share(price, rate, market.fee_exponent) * shares


def effective_taker_price(price: Decimal, market: Market) -> Decimal:
    """Price per share including the fee, for a BUY."""
    return D(price) + taker_fee(price, ONE, market)
