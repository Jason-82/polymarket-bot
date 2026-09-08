"""Order management: reconcile desired intents (by tag) with open orders.

Resting intents (GTC): keep if an open order with the same tag is within
`requote_epsilon` and same size; otherwise cancel + replace, rate-limited.
Immediate intents (FAK/FOK): fire once, then cool down for the same tag.
"""

from __future__ import annotations

import time
from decimal import Decimal

from .config import ExecutionConfig
from .execution import Exchange, ExchangeError
from .log import get_logger
from .models import Fill, Intent, Order, TimeInForce
from .store import Store

log = get_logger(__name__)


class OMS:
    def __init__(self, exchange: Exchange, cfg: ExecutionConfig, store: Store):
        self.x = exchange
        self.cfg = cfg
        self.store = store
        self._last_replace: dict[str, float] = {}
        self._last_taker: dict[str, float] = {}
        self.placed = 0
        self.cancelled = 0
        self.rejected = 0

    async def sync(self, intents: list[Intent]) -> None:
        now = time.time()
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
            size_changed = cur.remaining != it.size
            if not price_moved and not size_changed:
                continue
            if now - self._last_replace.get(tag, 0.0) < self.cfg.min_seconds_between_requotes:
                continue
            await self._cancel(cur, f"requote {cur.price}->{it.price}")
            if await self._place(it):
                self._last_replace[tag] = now

        # 3. immediates
        for it in immediates:
            if now - self._last_taker.get(it.tag, 0.0) < self.cfg.taker_retry_seconds:
                continue
            self._last_taker[it.tag] = now
            await self._place(it)

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
    async def _place(self, it: Intent) -> bool:
        try:
            o = await self.x.place(it)
        except ExchangeError as e:
            self.rejected += 1
            self.store.order_event("rejected", it.tag, it.strategy, it.token_id, it.side, it.price, it.size, note=str(e))
            log.info("order_rejected", tag=it.tag, reason=str(e))
            return False
        except Exception as e:
            self.rejected += 1
            self.store.order_event("error", it.tag, it.strategy, it.token_id, it.side, it.price, it.size, note=str(e)[:200])
            log.error("order_error", tag=it.tag, error=str(e)[:200])
            return False
        self.placed += 1
        self.store.order_event("placed", o.tag, o.strategy, o.token_id, o.side, o.price, o.size, order_id=o.order_id, note=it.reason)
        log.info("order_placed", tag=o.tag, side=o.side.value, price=str(o.price), size=str(o.size), tif=o.tif.value)
        return True

    async def _cancel(self, o: Order, why: str) -> None:
        try:
            await self.x.cancel(o.order_id)
            self.cancelled += 1
            self.store.order_event("cancelled", o.tag, o.strategy, o.token_id, o.side, o.price, o.remaining, order_id=o.order_id, note=why)
            log.info("order_cancelled", tag=o.tag, why=why)
        except Exception as e:
            log.error("cancel_failed", tag=o.tag, error=str(e)[:200])
