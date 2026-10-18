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
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Optional

from ..feed import Trade
from ..fees import maker_rebate, taker_fee
from ..log import get_logger
from ..models import ONE, ZERO, Book, Fill, Intent, Market, Order, OrderStatus, Side, TimeInForce
from . import ExchangeError

log = get_logger(__name__)


PENDING_WINDOW_S = 2.0   # a print that arrives this soon after the book that already showed it is the same volume


@dataclass
class _Queue:
    ahead: Decimal                       # displayed size in front of us at our price (excludes our own orders)
    level_seen: Decimal                  # displayed size at our price the last time we looked
    worse_seen: Optional[Decimal] = None # best price on our side strictly worse than ours, last time we looked
    pending_shrink: Decimal = ZERO       # level shrink seen in a book update and not yet matched to a print
    pending_credit: Decimal = ZERO       # how much `ahead` was reduced (pro rata) for that shrink
    pending_ts: float = 0.0


def level_size(book: Optional[Book], side: Side, price: Decimal) -> Decimal:
    """Displayed size at exactly `price` on our side of the book (bids for BUY, asks for SELL)."""
    if book is None:
        return ZERO
    for lvl in (book.bids if side is Side.BUY else book.asks):
        if lvl.price == price:
            return lvl.size
    return ZERO


def best_worse(book: Book, side: Side, price: Decimal) -> Optional[Decimal]:
    """Best price on our side strictly worse than `price` (the levels a sweep would reach after us)."""
    if side is Side.BUY:
        c = [l.price for l in book.bids if l.price < price]
        return max(c) if c else None
    c = [l.price for l in book.asks if l.price > price]
    return min(c) if c else None


def _worse(a: Decimal, b: Decimal, side: Side) -> bool:
    """Is price `a` worse than `b` for a resting order on `side`?"""
    return a < b if side is Side.BUY else a > b


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
        # An empty or crossed snapshot (a failed poll, a halt, a mid-update book) says nothing about where the
        # market traded. It must neither fill us nor move our place in the queue.
        if (not book.bids and not book.asks) or book.crossed:
            return
        self._books[book.token_id] = book
        now = self._clock()
        for o in list(self._orders.values()):
            if o.token_id != book.token_id or o.status is not OrderStatus.OPEN:
                continue
            q = self._queues.setdefault(o.order_id, _Queue(ZERO, ZERO))
            buy = o.side is Side.BUY
            opposite = book.best_ask if buy else book.best_bid
            if opposite is not None and (opposite <= o.price if buy else opposite >= o.price):
                self._fill_resting(o, o.price, o.remaining)          # the other side reached us
                continue
            best_same = book.best_bid if buy else book.best_ask
            if best_same is None:
                continue                                             # our side is empty: no evidence either way
            now_level = level_size(book, o.side, o.price)
            new_worse = best_worse(book, o.side, o.price)
            moved_away = _worse(best_same, o.price, o.side)
            if now_level <= ZERO and moved_away:
                if q.level_seen > ZERO and q.ahead > ZERO:
                    # Everyone in front of us vanished and the price moved on: swept (we would have filled) or
                    # pulled. Assume the sweep: the adverse case.
                    self._fill_resting(o, o.price, o.remaining)
                    continue
                if q.worse_seen is not None and _worse(best_same, q.worse_seen, o.side):
                    # We are alone at our price and the levels BEHIND us were taken out: a seller (buyer) sweeping
                    # them would have hit us first.
                    self._fill_resting(o, o.price, o.remaining)
                    continue
            shrink = q.level_seen - now_level
            if shrink > ZERO and q.level_seen > ZERO:
                credit = shrink * q.ahead / q.level_seen             # unexplained shrink: pro-rata cancels
                q.ahead -= credit
                if now - q.pending_ts > PENDING_WINDOW_S:
                    q.pending_shrink, q.pending_credit = ZERO, ZERO
                q.pending_shrink += shrink                           # ...unless a print for it shows up next
                q.pending_credit += credit
                q.pending_ts = now
            q.ahead = max(ZERO, min(q.ahead, now_level)).quantize(Decimal("0.01"), rounding=ROUND_UP)
            q.level_seen = now_level
            q.worse_seen = new_worse

    def _real(self, o: Order) -> tuple[str, Side, Decimal]:
        """The venue queue an order really sits in. On a mirrored venue a short-side order at q is an order on the
        long book at 1 - q on the opposite side, so it shares a queue with our long-side orders at that price."""
        m = self._market_for(o.token_id)
        if m is not None and m.synthetic_no and o.token_id == m.no.token_id and m.yes.token_id != m.no.token_id:
            return m.yes.token_id, o.side.opposite, ONE - o.price
        return o.token_id, o.side, o.price

    def on_trade(self, trade: Trade) -> None:
        m = self._market_for(trade.token_id)
        if m is not None and m.synthetic_no and trade.token_id == m.no.token_id and m.yes.token_id != m.no.token_id:
            return                                                   # mirrored venues: each print once, long copy
        now = self._clock()
        at_price: dict[Side, list[Order]] = {}
        for o in list(self._orders.values()):                       # dict order == placement order
            if o.status is not OrderStatus.OPEN:
                continue
            real_tok, real_side, real_px = self._real(o)
            if real_tok != trade.token_id:
                continue
            buy = real_side is Side.BUY
            if (trade.price < real_px) if buy else (trade.price > real_px):
                self._fill_resting(o, o.price, o.remaining)          # traded past our price: level emptied
            elif trade.price == real_px:
                at_price.setdefault(real_side, []).append(o)
        for orders in at_price.values():
            size = trade.size
            own_front = ZERO                                         # our earlier orders in this queue
            own_filled = ZERO                                        # what they took from this print
            for o in orders:
                if o.status is not OrderStatus.OPEN:
                    continue
                q = self._queues.setdefault(o.order_id, _Queue(ZERO, ZERO))
                already = ZERO
                if q.pending_shrink > ZERO and now - q.pending_ts <= PENDING_WINDOW_S:
                    # The book already showed (part of) this print and we counted it as cancellations.
                    # Undo that and count it as trading instead, FIFO.
                    already = min(size, q.pending_shrink)
                    undo = q.pending_credit * already / q.pending_shrink
                    q.ahead += undo
                    q.pending_shrink -= already
                    q.pending_credit -= undo
                elif now - q.pending_ts > PENDING_WINDOW_S:
                    q.pending_shrink, q.pending_credit = ZERO, ZERO
                before = o.remaining
                avail = size - q.ahead - own_front
                q.ahead -= min(q.ahead, max(ZERO, size - own_filled))
                q.level_seen = max(ZERO, q.level_seen - (size - already))
                got = ZERO
                if avail > ZERO:
                    got = self._fill_resting(o, o.price, avail)
                own_front += before
                own_filled += got

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
            # The real book still shows the liquidity this order just took (paper orders do not change it), so a
            # resting remainder would immediately "fill" again against the same size. Cancel it instead.
            log.info("paper_taker_remainder_cancelled", tag=order.tag, remaining=str(intent.size - filled))
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
        self._queues[order.order_id] = _Queue(
            ahead=size, level_seen=size,
            worse_seen=best_worse(book, order.side, order.price) if book is not None else None)

    def _fill_resting(self, o: Order, price: Decimal, available: Decimal) -> Decimal:
        qty = min(o.remaining, available)
        m = self._market_for(o.token_id)
        if m is not None and m.synthetic_no:
            qty = qty.to_integral_value(rounding=ROUND_DOWN)          # Polymarket US trades whole contracts
        if qty <= ZERO:
            return ZERO
        o.filled += qty
        if o.remaining <= ZERO:
            o.status = OrderStatus.FILLED
            self._orders.pop(o.order_id, None)
            self._queues.pop(o.order_id, None)
        rebate = maker_rebate(price, qty, m) if m else ZERO
        self._fills.append(Fill(
            order_id=o.order_id, tag=o.tag, strategy=o.strategy, token_id=o.token_id,
            side=o.side, price=price, size=qty, fee=-rebate, maker=True, ts=self._clock(),
        ))
        log.info("paper_maker_fill", tag=o.tag, side=o.side.value, size=str(qty), price=str(price))
        return qty
