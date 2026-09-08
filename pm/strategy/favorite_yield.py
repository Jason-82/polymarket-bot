"""favorite_yield — buy near-certain outcomes close to resolution, as a maker.

Payoff: (1 - p) per share if right; -p if wrong. We only take it when the
annualised yield clears a threshold, and we post the bid rather than lift the
ask so no taker fee applies. Off by default: the tail risk is real.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..models import ONE, ZERO, D, Intent, Side, TimeInForce
from ..state import Context
from . import register


@register("favorite_yield")
class FavoriteYield:
    name = "favorite_yield"

    def __init__(self, params: dict[str, Any]):
        self.min_price = D(params.get("min_price", "0.95"))
        self.max_days = float(params.get("max_days_to_resolution", 14))
        self.min_annualised = D(params.get("min_annualized_yield", "0.25"))
        self.size = D(params.get("size_shares", 10))

    def on_tick(self, ctx: Context) -> list[Intent]:
        out: list[Intent] = []
        for m in ctx.markets.values():
            if not m.is_binary:
                continue
            secs = m.seconds_to_resolution(ctx.now)
            if secs is None or secs <= 0:
                continue
            days = Decimal(secs) / Decimal(86400)
            if days > Decimal(self.max_days):
                continue
            for tok in m.tokens:
                b = ctx.book(tok.token_id)
                if not b or b.best_bid is None or b.best_ask is None:
                    continue
                if ctx.shares(tok.token_id) >= self.size:
                    continue
                # Join the bid; if the spread is one tick this is the best maker price.
                px = max(b.best_bid, b.best_ask - m.tick_size)
                if px < self.min_price or px >= ONE:
                    continue
                yld = (ONE - px) / px
                annual = yld * Decimal(365) / max(days, Decimal("0.01"))
                if annual < self.min_annualised:
                    continue
                out.append(Intent(
                    strategy=self.name, token_id=tok.token_id, side=Side.BUY,
                    price=px, size=self.size, tif=TimeInForce.GTC, post_only=True,
                    reason=f"px={px} days={days:.1f} annualised={annual:.2f}",
                ))
        return out
