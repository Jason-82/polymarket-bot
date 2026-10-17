"""Paper exchange: simulates fills against the live tape.

Fill model
- A taker order (post_only=False) that crosses is filled immediately by walking
  the book; FOK requires the full size, FAK takes what is there. Taker fees apply.
- A post_only order that would cross is rejected, as the venue would.
- A resting order joins the BACK of the queue at its price: `ahead` starts at the
  displayed size at that price (0 if the order improves the best price).
  * A trade printing at our price consumes `ahead` first; only volume beyond it
    fills us. A trade printing through our price (lower for a bid, higher for an
    ask) means our level was emptied: we fill completely.
  * When the displayed size at our price shrinks by more than the trades we saw,
    the difference is treated as cancellations spread evenly through the queue,
    so `ahead` shrinks pro rata.
  * When our level disappears and the price moves away from us, or the opposite
    side reaches our price, we fill completely. These are the fills that happen
    as the market moves against a maker, so this choice is deliberately
    pessimistic: it is where adverse selection shows up.
Our paper orders are not in the real book, so the trades we see are what the
market did without us. Paper fills are still an approximation; they are no
longer an upper bound that ignores the queue.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from ..feed import Trade
from ..fees import maker_rebate, taker_fee
from ..log import get_logger
from ..models import ZERO, Book, Fill, Intent, Market, Order, OrderStatus, Side, TimeInForce
from . import ExchangeError

log = get_logger(__name__)


@dataclass
class _Queue:
    ahead: Decimal        # displayed size in front of us at our price
    level_seen: Decimal   # displayed size at our price the last time we looked


def level_size(book: Optional[Book], side: Side, price: Decimal) -> Decimal:
    """Displayed size at exactly `price` on our side of the book (bids for BUY, asks for SELL)."""
    if book is None:
        return ZERO
    for lvl in (book.bids if side is Side.BUY else book.asks):
        if lvl.price == price:
            return lvl.size
    return ZERO


class PaperExchange:
    def __init__(self, market_for: Callable[[str], Optional[Market]], starting_cash: Decimal,
                 clock: Callable[[], float] = time.time):
        self._market_for = market_for
        self._orders: dict[str, Order] = {}
        self._queues: dict[str, _Queue] = {}
        self._fills: list[Fill] = []
        self._books: dict[str, Book] = {}
        self._clock = clock
        self.cash_hint = starting_cash

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.cancel_all()

    # ------------------------------------------------------------------ market data hooks
    def on_book(self, book: Book) -> None:
        self._books[book.token_id] = book
        for o in list(self._orders.values()):
            if o.token_id != book.token_id or o.status is not OrderStatus.OPEN:
                continue
            q = self._queues.setdefault(o.order_id, _Queue(ZERO, ZERO))
            buy = o.side is Side.BUY
            opposite = book.best_ask if buy else book.best_bid
            if opposite is not None and (opposite <= o.price if buy else opposite >= o.price):
                self._fill_resting(o, o.price, o.remaining)          # the other side reached us
                continue
            now_level = level_size(book, o.side, o.price)
            best_same = book.best_bid if buy else book.best_ask
            moved_away = best_same is None or (best_same < o.price if buy else best_same > o.price)
            if now_level <= ZERO and moved_away and q.level_seen > ZERO and q.ahead > ZERO:
                # The orders in front of us vanished and the price moved on: swept (we would have filled) or
                # pulled (we would now be alone at a stale price). Assume the sweep: the adverse case.
                self._fill_resting(o, o.price, o.remaining)
                continue
            shrink = q.level_seen - now_level
            if shrink > ZERO and q.level_seen > ZERO:
                q.ahead -= shrink * q.ahead / q.level_seen           # unexplained shrink: pro-rata cancels
            q.ahead = max(ZERO, min(q.ahead, now_level))
            q.level_seen = now_level

    def on_trade(self, trade: Trade) -> None:
        for o in list(self._orders.values()):
            if o.token_id != trade.token_id or o.status is not OrderStatus.OPEN:
                continue
            buy = o.side is Side.BUY
            through = trade.price < o.price if buy else trade.price > o.price
            at = trade.price == o.price
            if through:
                self._fill_resting(o, o.price, o.remaining)          # traded past our price: level emptied
                continue
            if not at:
                continue
            q = self._queues.setdefault(o.order_id, _Queue(ZERO, ZERO))
            used = min(q.ahead, trade.size)
            q.ahead -= used
            q.level_seen = max(ZERO, q.level_seen - trade.size)      # the real book shrinks by the whole print
            beyond = trade.size - used
            if beyond > ZERO:
                self._fill_resting(o, o.price, beyond)

    # ------------------------------------------------------------------ exchange API
    async def place(self, intent: Intent) -> Order:
        book = self._books.get(intent.token_id)
        order = Order(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            tag=intent.tag, strategy=intent.strategy, token_id=intent.token_id,
            side=intent.side, price=intent.price, size=intent.size,
            tif=intent.tif, post_only=intent.post_only,
        )
        crosses = self._crosses(intent, book)

        if intent.post_only:
            if crosses:
                raise ExchangeError("post_only order would cross the book")
            self._rest(order, book)
            return order

        # Taker path
        if not crosses or book is None:
            if intent.tif is TimeInForce.GTC:
                self._rest(order, book)
                return order
            raise ExchangeError("no liquidity at limit price")

        # Walk the book but only through levels within the limit price
        filled, notional = self._walk_limited(book, intent.side, intent.size, intent.price)
        if filled <= ZERO:
            raise ExchangeError("no liquidity at limit price")
        if intent.tif is TimeInForce.FOK and filled < intent.size:
            raise ExchangeError("FOK could not be fully filled")

        m = self._market_for(intent.token_id)
        avg = notional / filled
        fee = taker_fee(avg, filled, m) if m else ZERO
        order.filled = filled
        order.status = OrderStatus.FILLED if filled >= intent.size else OrderStatus.CANCELLED
        self._fills.append(Fill(
            order_id=order.order_id, tag=order.tag, strategy=order.strategy, token_id=order.token_id,
            side=order.side, price=avg, size=filled, fee=fee, maker=False, ts=self._clock(),
        ))
        if intent.tif is TimeInForce.GTC and filled < intent.size:
            order.status = OrderStatus.OPEN
            self._rest(order, book)
        log.info("paper_taker_fill", tag=order.tag, side=order.side.value, size=str(filled), avg=str(avg), fee=str(fee))
        return order

    async def cancel(self, order_id: str) -> None:
        o = self._orders.pop(order_id, None)
        self._queues.pop(order_id, None)
        if o and o.status is OrderStatus.OPEN:
            o.status = OrderStatus.CANCELLED

    async def cancel_all(self) -> None:
        for o in self._orders.values():
            if o.status is OrderStatus.OPEN:
                o.status = OrderStatus.CANCELLED
        self._orders.clear()
        self._queues.clear()

    def queue_ahead(self, order_id: str) -> Optional[Decimal]:
        q = self._queues.get(order_id)
        return q.ahead if q else None

    async def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status is OrderStatus.OPEN]

    async def drain_fills(self) -> list[Fill]:
        out, self._fills = self._fills, []
        return out

    async def balance(self) -> Optional[Decimal]:
        return None  # the portfolio tracks paper cash itself

    async def positions(self) -> Optional[dict[str, tuple[Decimal, Decimal]]]:
        return None

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _crosses(intent: Intent, book: Optional[Book]) -> bool:
        if book is None:
            return False
        if intent.side is Side.BUY:
            return book.best_ask is not None and intent.price >= book.best_ask
        return book.best_bid is not None and intent.price <= book.best_bid

    @staticmethod
    def _walk_limited(book: Book, side: Side, shares: Decimal, limit: Decimal) -> tuple[Decimal, Decimal]:
        levels = book.asks if side is Side.BUY else book.bids
        remaining, filled, notional = shares, ZERO, ZERO
        for lvl in levels:
            if (side is Side.BUY and lvl.price > limit) or (side is Side.SELL and lvl.price < limit):
                break
            take = min(remaining, lvl.size)
            filled += take
            notional += take * lvl.price
            remaining -= take
            if remaining <= ZERO:
                break
        return filled, notional

    def _rest(self, order: Order, book: Optional[Book]) -> None:
        """Put an order on the (simulated) book behind everything already displayed at its price."""
        size = level_size(book, order.side, order.price)
        self._orders[order.order_id] = order
        self._queues[order.order_id] = _Queue(ahead=size, level_seen=size)

    def _fill_resting(self, o: Order, price: Decimal, available: Decimal) -> None:
        qty = min(o.remaining, available)
        if qty <= ZERO:
            return
        o.filled += qty
        if o.remaining <= ZERO:
            o.status = OrderStatus.FILLED
            self._orders.pop(o.order_id, None)
            self._queues.pop(o.order_id, None)
        m = self._market_for(o.token_id)
        rebate = maker_rebate(price, qty, m) if m else ZERO
        self._fills.append(Fill(
            order_id=o.order_id, tag=o.tag, strategy=o.strategy, token_id=o.token_id,
            side=o.side, price=price, size=qty, fee=-rebate, maker=True, ts=self._clock(),
        ))
        log.info("paper_maker_fill", tag=o.tag, side=o.side.value, size=str(qty), price=str(price))
