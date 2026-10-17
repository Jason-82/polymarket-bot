"""Order management: reconcile desired intents (by tag) with open orders.

Resting intents (GTC): keep if an open order with the same tag is within
`requote_epsilon` and same size; otherwise cancel + replace, rate-limited.
Immediate intents (FAK/FOK): fire once, then cool down for the same tag. Intents
that share a `group` (the legs of a complete set) fire as a unit: one cooldown,
each later leg sized to what the earlier legs actually filled, and the group stops
at the first leg that fills nothing.
"""

from __future__ import annotations

import dataclasses
import time
from decimal import Decimal
from typing import Optional

from .config import ExecutionConfig
from .execution import Exchange, ExchangeError
from .log import get_logger
from .models import ZERO, Fill, Intent, Order, TimeInForce
from .store import Store

log = get_logger(__name__)


class OMS:
    def __init__(self, exchange: Exchange, cfg: ExecutionConfig, store: Store, clock=time.time):
        self.x = exchange
        self.cfg = cfg
        self.store = store
        self._clock = clock          # the backtest passes replay time; cooldowns must not use the wall clock
        self._last_replace: dict[str, float] = {}
        self._last_taker: dict[str, float] = {}
        self.placed = 0
        self.cancelled = 0
        self.rejected = 0

    async def sync(self, intents: list[Intent]) -> None:
        now = self._clock()
        open_orders = await self.x.open_orders()
        by_tag: dict[str, Order] = {o.tag: o for o in open_orders}
        desired = {i.tag: i for i in intents if i.tif is TimeInForce.GTC}
        immediates = [i for i in intents if i.tif is not TimeInForce.GTC]

        # 1. cancel resting orders no longer wanted
        for tag, o in list(by_tag.items()):
            if tag not in desired:
                await self._cancel(o, "not desired")
                by_tag.pop(tag, None)

        # 2. place / replace resting
        for tag, it in desired.items():
            cur = by_tag.get(tag)
            if cur is None:
                await self._place(it)
                continue
            price_moved = abs(cur.price - it.price) >= self.cfg.requote_epsilon
            # Size: compare against the ORIGINAL size (a partial fill must not trigger a replace),
            # and only act on a material change; every replace costs queue position.
            size_changed = abs(it.size - cur.size) > cur.size * self.cfg.size_change_band
            if not price_moved and not size_changed:
                continue
            if now - self._last_replace.get(tag, 0.0) < self.cfg.min_seconds_between_requotes:
                continue
            await self._cancel(cur, f"requote {cur.price}->{it.price}")
            if await self._place(it):
                self._last_replace[tag] = now

        # 3. immediates, one group at a time (an ungrouped intent is a group of one).
        groups: dict[str, list[Intent]] = {}
        for it in immediates:
            groups.setdefault(it.group or it.tag, []).append(it)
        for key, legs in groups.items():
            if now - self._last_taker.get(key, 0.0) < self.cfg.taker_retry_seconds:
                continue
            self._last_taker[key] = now
            await self._fire_group(key, legs)

    async def _fire_group(self, key: str, legs: list[Intent]) -> None:
        """Fire legs in order. Each later leg is sized down to the smallest fill so far, and the group
        stops at the first leg that fills nothing. The only way a set can still end unbalanced is a
        shortfall on a later leg after earlier legs filled; that is logged and recorded loudly."""
        if len(legs) == 1:
            await self._place(legs[0])
            return
        size = legs[0].size
        filled: list[tuple[str, Decimal]] = []
        for i, it in enumerate(legs):
            if size < it.size:
                it = dataclasses.replace(it, size=size)
            o = await self._place(it)
            got = o.filled if o is not None else ZERO
            if got <= ZERO:
                rest = legs[i + 1:]
                for r in rest:
                    self.store.order_event("aborted", r.tag, r.strategy, r.token_id, r.side, r.price, r.size,
                                           note=f"group {key}: leg {it.tag} filled nothing")
                if filled:
                    log.error("group_leg_unhedged", group=key, failed_leg=it.tag, filled=filled)
                    self.store.event("group_unhedged", f"{key} failed_leg={it.tag} filled={filled}")
                else:
                    log.info("group_aborted", group=key, failed_leg=it.tag, skipped=len(rest))
                return
            filled.append((it.tag, got))
            size = min(size, got)
        if len({g for _, g in filled}) > 1:
            log.error("group_imbalanced", group=key, filled=filled)
            self.store.event("group_imbalanced", f"{key} filled={filled}")

    async def cancel_all(self) -> None:
        try:
            await self.x.cancel_all()
        except Exception as e:
            log.error("cancel_all_failed", error=str(e))

    async def drain_fills(self) -> list[Fill]:
        fills = await self.x.drain_fills()
        for f in fills:
            self.store.fill(f)
        return fills

    # ------------------------------------------------------------------ internals
    async def _place(self, it: Intent) -> Optional[Order]:
        """Place one intent; returns the Order (with `filled` known for immediate orders) or None."""
        try:
            o = await self.x.place(it)
        except ExchangeError as e:
            self.rejected += 1
            self.store.order_event("rejected", it.tag, it.strategy, it.token_id, it.side, it.price, it.size, note=str(e))
            log.info("order_rejected", tag=it.tag, reason=str(e))
            return None
        except Exception as e:
            self.rejected += 1
            self.store.order_event("error", it.tag, it.strategy, it.token_id, it.side, it.price, it.size, note=str(e)[:200])
            log.error("order_error", tag=it.tag, error=str(e)[:200])
            return None
        self.placed += 1
        self.store.order_event("placed", o.tag, o.strategy, o.token_id, o.side, o.price, o.size, order_id=o.order_id, note=it.reason)
        log.info("order_placed", tag=o.tag, side=o.side.value, price=str(o.price), size=str(o.size), tif=o.tif.value)
        return o

    async def _cancel(self, o: Order, why: str) -> None:
        try:
            await self.x.cancel(o.order_id)
            self.cancelled += 1
            self.store.order_event("cancelled", o.tag, o.strategy, o.token_id, o.side, o.price, o.remaining, order_id=o.order_id, note=why)
            log.info("order_cancelled", tag=o.tag, why=why)
        except Exception as e:
            log.error("cancel_failed", tag=o.tag, error=str(e)[:200])
