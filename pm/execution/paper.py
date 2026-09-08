"""Paper exchange: simulates fills against the live tape.

Fill model
- A taker order (post_only=False) that crosses is filled immediately by walking
  the book; FOK requires the full size, FAK takes what is there. Taker fees apply.
- A post_only order that would cross is rejected, as the venue would.
- A resting order fills when (a) a real trade prints at or through its price, or
  (b) the opposite side of the book moves through it. Maker fees are zero.
Queue position is ignored, so resting fills are somewhat optimistic. Treat
paper P&L as an upper bound.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Callable, Optional

from ..feed import Trade
from ..fees import taker_fee
from ..log import get_logger
from ..models import ZERO, Book, Fill, Intent, Market, Order, OrderStatus, Side, TimeInForce
from . import ExchangeError

log = get_logger(__name__)


class PaperExchange:
    def __init__(self, market_for: Callable[[str], Optional[Market]], starting_cash: Decimal):
        self._market_for = market_for
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._books: dict[str, Book] = {}
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
            if o.side is Side.BUY and book.best_ask is not None and book.best_ask <= o.price:
                self._fill_resting(o, o.price, book.ask_size_at_touch)
            elif o.side is Side.SELL and book.best_bid is not None and book.best_bid >= o.price:
                self._fill_resting(o, o.price, book.bid_size_at_touch)

    def on_trade(self, trade: Trade) -> None:
        for o in list(self._orders.values()):
            if o.token_id != trade.token_id or o.status is not OrderStatus.OPEN:
                continue
            if o.side is Side.BUY and trade.price <= o.price:
                self._fill_resting(o, o.price, trade.size)
            elif o.side is Side.SELL and trade.price >= o.price:
                self._fill_resting(o, o.price, trade.size)

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
            self._orders[order.order_id] = order
            return order

        # Taker path
        if not crosses or book is None:
            if intent.tif is TimeInForce.GTC:
                self._orders[order.order_id] = order
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
            side=order.side, price=avg, size=filled, fee=fee, maker=False,
        ))
        if intent.tif is TimeInForce.GTC and filled < intent.size:
            order.status = OrderStatus.OPEN
            self._orders[order.order_id] = order
        log.info("paper_taker_fill", tag=order.tag, side=order.side.value, size=str(filled), avg=str(avg), fee=str(fee))
        return order

    async def cancel(self, order_id: str) -> None:
        o = self._orders.pop(order_id, None)
        if o and o.status is OrderStatus.OPEN:
            o.status = OrderStatus.CANCELLED

    async def cancel_all(self) -> None:
        for o in self._orders.values():
            if o.status is OrderStatus.OPEN:
                o.status = OrderStatus.CANCELLED
        self._orders.clear()

    async def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status is OrderStatus.OPEN]

    async def drain_fills(self) -> list[Fill]:
        out, self._fills = self._fills, []
        return out

    async def balance(self) -> Optional[Decimal]:
        return None  # the portfolio tracks paper cash itself

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

    def _fill_resting(self, o: Order, price: Decimal, available: Decimal) -> None:
        qty = min(o.remaining, available if available > ZERO else o.remaining)
        if qty <= ZERO:
            return
        o.filled += qty
        if o.remaining <= ZERO:
            o.status = OrderStatus.FILLED
            self._orders.pop(o.order_id, None)
        self._fills.append(Fill(
            order_id=o.order_id, tag=o.tag, strategy=o.strategy, token_id=o.token_id,
            side=o.side, price=price, size=qty, fee=ZERO, maker=True, ts=time.time(),
        ))
        log.info("paper_maker_fill", tag=o.tag, side=o.side.value, size=str(qty), price=str(price))
