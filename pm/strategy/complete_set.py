"""complete_set — structural arbitrage.

Binary:   if ask_yes + ask_no + fees < 1 - margin, buy both. Payoff is exactly $1.
Neg-risk: if Σ ask_yes_i + fees < 1 - margin across a complete, non-augmented
          event, buy every YES. Exactly one pays $1.

Also detects (but does not execute) the sell side: bid_yes + bid_no > 1 + margin,
which would require splitting collateral on-chain.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

from ..fees import taker_fee_per_share
from ..fees import WORST_CASE_RATE
from ..log import get_logger
from ..models import ONE, ZERO, D, Book, Intent, Market, Side, TimeInForce
from ..state import Context
from . import register

log = get_logger(__name__)


def _fee(m: Market, price: Decimal) -> Decimal:
    rate = m.fee_rate if m.fee_known else WORST_CASE_RATE
    return taker_fee_per_share(price, rate, m.fee_exponent)


def _floor_shares(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"), rounding=ROUND_DOWN)


@register("complete_set")
class CompleteSet:
    name = "complete_set"

    def __init__(self, params: dict[str, Any]):
        self.margin = D(params.get("margin", "0.01"))
        self.max_legs_notional = D(params.get("max_legs_notional_usd", "30"))
        self.taker = bool(params.get("taker", True))

    # ------------------------------------------------------------------ public
    def on_tick(self, ctx: Context) -> list[Intent]:
        intents: list[Intent] = []
        seen_events: set[str] = set()
        for m in ctx.markets.values():
            if m.neg_risk and m.event_id:
                if m.event_id in seen_events:
                    continue
                seen_events.add(m.event_id)
                intents.extend(self._neg_risk_event(ctx, m.event_id))
            elif m.is_binary:
                intents.extend(self._binary(ctx, m))
        return intents

    # ------------------------------------------------------------------ binary
    def _binary(self, ctx: Context, m: Market) -> list[Intent]:
        by, bn = ctx.book(m.yes.token_id), ctx.book(m.no.token_id)
        if not by or not bn or by.best_ask is None or bn.best_ask is None:
            return []

        ay, an = by.best_ask, bn.best_ask
        cost = ay + an + _fee(m, ay) + _fee(m, an)
        if by.best_bid is not None and bn.best_bid is not None:
            proceeds = by.best_bid + bn.best_bid
            if proceeds > ONE + self.margin:
                log.info("complete_set_sell_side_detected", market=m.slug, bid_sum=str(proceeds))

        if cost >= ONE - self.margin:
            return []

        size = self._size([(by, ay), (bn, an)], cost, m)
        if size <= ZERO:
            return []
        edge = ONE - cost
        reason = f"yes_ask={ay} no_ask={an} cost={cost:.4f} edge={edge:.4f}"
        log.info("complete_set_opportunity", market=m.slug, cost=str(cost), size=str(size))
        return [
            self._leg(m, m.yes.token_id, ay, size, reason, "YES"),
            self._leg(m, m.no.token_id, an, size, reason, "NO"),
        ]

    # ------------------------------------------------------------------ neg-risk
    def _neg_risk_event(self, ctx: Context, event_id: str) -> list[Intent]:
        group = [m for m in ctx.markets.values() if m.event_id == event_id and m.neg_risk]
        if len(group) < 2:
            return []
        if any(m.neg_risk_augmented for m in group):
            return []
        expected = group[0].event_market_count
        if expected and len(group) != expected:
            return []  # we don't hold the whole event; the sum is meaningless

        legs: list[tuple[Market, Book, Decimal]] = []
        cost = ZERO
        for m in group:
            b = ctx.book(m.yes.token_id)
            if not b or b.best_ask is None:
                return []
            legs.append((m, b, b.best_ask))
            cost += b.best_ask + _fee(m, b.best_ask)

        if cost >= ONE - self.margin:
            return []

        size = self._size([(b, a) for _, b, a in legs], cost, group[0])
        if size <= ZERO:
            return []
        reason = f"neg_risk n={len(legs)} cost={cost:.4f} edge={(ONE - cost):.4f}"
        log.info("complete_set_neg_risk_opportunity", event=event_id, cost=str(cost), legs=len(legs), size=str(size))
        return [self._leg(m, m.yes.token_id, a, size, reason, m.yes.outcome) for m, _, a in legs]

    # ------------------------------------------------------------------ helpers
    def _size(self, legs: list[tuple[Book, Decimal]], cost: Decimal, m: Market) -> Decimal:
        # Limited by what is available at the touch on every leg and by the dollar cap.
        avail = min(b.ask_size_at_touch for b, _ in legs)
        by_cap = self.max_legs_notional / cost if cost > ZERO else ZERO
        size = _floor_shares(min(avail, by_cap))
        return size if size >= m.min_order_size else ZERO

    def _leg(self, m: Market, token_id: str, price: Decimal, size: Decimal, reason: str, outcome: str) -> Intent:
        return Intent(
            strategy=self.name,
            token_id=token_id,
            side=Side.BUY,
            price=price,
            size=size,
            tif=TimeInForce.FAK,
            post_only=False,
            reason=reason,
            tag=f"{self.name}:{m.condition_id}:{outcome}",
        )
