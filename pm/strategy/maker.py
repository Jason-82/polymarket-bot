"""maker — rest bids on both YES and NO around a fair value.

A NO bid at (1 - p) is economically an ask on YES. When both bids fill we
hold a complete set bought for (1 - spread) that pays $1 at resolution.
Inventory is controlled by skewing the fair value and shrinking the size on
the heavy side; optionally we also post asks on tokens we already hold.

Quotes are pulled (no intent emitted, so the OMS cancels) when the book is
stale, too thin, too wide, already tighter than our edge, or outside the
price band.

Two scale features:
- rewards_first: in markets with a liquidity-rewards pool, quote inside the
  qualifying spread at (at least) the qualifying size, so resting orders
  score every second whether or not they fill.
- hold_within: if an existing resting order is within this distance of the
  new target, keep its price. Every cancel/replace goes to the back of the
  queue; holding a near-target quote is worth more than a tick of precision.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

from ..log import get_logger
from ..models import ONE, ZERO, D, Book, Intent, Market, Order, Side, TimeInForce, clamp_price, round_to_tick
from ..state import Context
from . import register

log = get_logger(__name__)


@register("maker")
class Maker:
    name = "maker"

    def __init__(self, params: dict[str, Any]):
        self.quote_size = D(params.get("quote_size_shares", 10))
        self.half_spread = D(params.get("half_spread", "0.02"))
        self.min_book_spread = D(params.get("min_book_spread", "0.02"))
        self.max_book_spread = D(params.get("max_book_spread", "0.15"))
        self.min_depth = D(params.get("min_depth_shares_at_touch", 50))
        band = params.get("price_band") or [0.10, 0.90]
        self.band_lo, self.band_hi = D(band[0]), D(band[1])
        self.max_inventory = D(params.get("max_inventory_shares", 40))
        self.skew_per_share = D(params.get("inventory_skew_per_share", "0.0005"))
        self.sell_inventory = bool(params.get("sell_inventory", True))
        self.stale_after = float(params.get("stale_after_seconds", 15))
        self.hold_within = D(params.get("hold_within", "0.01"))
        self.rewards_first = bool(params.get("rewards_first", True))
        self.reward_spread_buffer = D(params.get("reward_spread_buffer", "0.005"))

    def on_tick(self, ctx: Context) -> list[Intent]:
        out: list[Intent] = []
        for m in ctx.markets.values():
            if not m.is_binary:
                continue
            out.extend(self.quote_market(ctx, m))
        return out

    # ------------------------------------------------------------------ core
    def quote_market(self, ctx: Context, m: Market) -> list[Intent]:
        by, bn = ctx.book(m.yes.token_id), ctx.book(m.no.token_id)
        if not self._usable(by, ctx.now_ts) or not self._usable(bn, ctx.now_ts):
            return []

        # Fair value from both books (they should agree: mid_yes ≈ 1 - mid_no)
        fair = (by.mid + (ONE - bn.mid)) / 2
        if fair < self.band_lo or fair > self.band_hi:
            return []

        hs, base_size = self._spread_and_size(m)
        inv_yes, inv_no = ctx.shares(m.yes.token_id), ctx.shares(m.no.token_id)
        net = inv_yes - inv_no                       # complete sets net out
        fair_adj = clamp_price(fair - net * self.skew_per_share, m.tick_size)
        open_by_tag = {o.tag: o for o in ctx.open_orders if o.strategy == self.name}

        intents: list[Intent] = []
        note = f"fair={fair:.3f} hs={hs} net={net}"

        # --- YES bid (shrinks as we get long YES)
        size_y = self._scaled_size(base_size, net)
        if size_y >= m.min_order_size:
            px = self._bid_px(fair_adj - hs, by, m)
            if px is not None:
                px = self._hold(open_by_tag, m.yes.token_id, Side.BUY, px, by, m)
                intents.append(self._intent(m.yes.token_id, Side.BUY, px, size_y, note))

        # --- NO bid (shrinks as we get long NO, i.e. net negative)
        size_n = self._scaled_size(base_size, -net)
        if size_n >= m.min_order_size:
            px = self._bid_px((ONE - fair_adj) - hs, bn, m)
            if px is not None:
                px = self._hold(open_by_tag, m.no.token_id, Side.BUY, px, bn, m)
                intents.append(self._intent(m.no.token_id, Side.BUY, px, size_n, note))

        # --- Optional asks on inventory we actually hold
        if self.sell_inventory:
            if net > ZERO and inv_yes > ZERO:
                px = self._ask_px(fair_adj + hs, by, m)
                sz = min(inv_yes, base_size).quantize(Decimal("1"), rounding=ROUND_DOWN)
                if px is not None and sz >= m.min_order_size:
                    px = self._hold(open_by_tag, m.yes.token_id, Side.SELL, px, by, m)
                    intents.append(self._intent(m.yes.token_id, Side.SELL, px, sz, f"unwind net={net}"))
            elif net < ZERO and inv_no > ZERO:
                px = self._ask_px((ONE - fair_adj) + hs, bn, m)
                sz = min(inv_no, base_size).quantize(Decimal("1"), rounding=ROUND_DOWN)
                if px is not None and sz >= m.min_order_size:
                    px = self._hold(open_by_tag, m.no.token_id, Side.SELL, px, bn, m)
                    intents.append(self._intent(m.no.token_id, Side.SELL, px, sz, f"unwind net={net}"))

        return intents

    # ------------------------------------------------------------------ helpers
    def _spread_and_size(self, m: Market) -> tuple[Decimal, Decimal]:
        """Half-spread and base size, tightened to the rewards program's qualifying rules."""
        hs, size = self.half_spread, self.quote_size
        if self.rewards_first and m.has_rewards:
            qualifying = m.reward_max_spread - self.reward_spread_buffer
            hs = max(m.tick_size, min(hs, qualifying))
            if m.reward_min_size > size:
                size = m.reward_min_size
        return hs, size

    def _usable(self, b: Optional[Book], now_ts: float) -> bool:
        if b is None or b.mid is None:
            return False
        if b.age(now_ts) > self.stale_after:
            return False
        sp = b.spread
        if sp is None or sp < self.min_book_spread or sp > self.max_book_spread:
            return False
        if b.bid_size_at_touch < self.min_depth or b.ask_size_at_touch < self.min_depth:
            return False
        return True

    def _scaled_size(self, base: Decimal, exposure: Decimal) -> Decimal:
        """Linear shrink from base at 0 exposure to 0 at max_inventory."""
        if exposure <= ZERO:
            return base
        if exposure >= self.max_inventory:
            return ZERO
        frac = ONE - exposure / self.max_inventory
        return (base * frac).quantize(Decimal("1"), rounding=ROUND_DOWN)

    def _bid_px(self, target: Decimal, b: Book, m: Market) -> Optional[Decimal]:
        px = round_to_tick(clamp_price(target, m.tick_size), m.tick_size, Side.BUY)
        if b.best_ask is not None and px >= b.best_ask:      # never cross (post-only)
            px = b.best_ask - m.tick_size
        return px if px >= m.tick_size else None

    def _ask_px(self, target: Decimal, b: Book, m: Market) -> Optional[Decimal]:
        px = round_to_tick(clamp_price(target, m.tick_size), m.tick_size, Side.SELL)
        if b.best_bid is not None and px <= b.best_bid:
            px = b.best_bid + m.tick_size
        return px if px <= ONE - m.tick_size else None

    def _hold(self, open_by_tag: dict[str, Order], token_id: str, side: Side, target: Decimal,
              b: Book, m: Market) -> Decimal:
        """Keep the current resting price if it is close enough to the target and still valid."""
        cur = open_by_tag.get(f"{self.name}:{token_id}:{side.value}")
        if cur is None or abs(cur.price - target) > self.hold_within:
            return target
        if side is Side.BUY and b.best_ask is not None and cur.price >= b.best_ask:
            return target
        if side is Side.SELL and b.best_bid is not None and cur.price <= b.best_bid:
            return target
        if self.rewards_first and m.has_rewards and abs(cur.price - (b.mid or cur.price)) > m.reward_max_spread:
            return target  # drifted outside the scoring band; worth losing the queue for
        return cur.price

    def _intent(self, token_id: str, side: Side, price: Decimal, size: Decimal, reason: str) -> Intent:
        return Intent(
            strategy=self.name, token_id=token_id, side=side, price=price, size=size,
            tif=TimeInForce.GTC, post_only=True, reason=reason,
        )
