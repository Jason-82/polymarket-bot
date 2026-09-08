"""Shared in-memory state: markets, books, portfolio; and the read-only Context strategies see."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from .models import ZERO, Book, Fill, Market, Order, Position, Side, utcnow


@dataclass
class Portfolio:
    cash: Decimal = ZERO                       # free collateral (paper: simulated; live: reported)
    positions: dict[str, Position] = field(default_factory=dict)
    realised_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    day_start_equity: Optional[Decimal] = None
    day_key: str = ""

    def position(self, token_id: str) -> Position:
        return self.positions.setdefault(token_id, Position(token_id))

    def shares(self, token_id: str) -> Decimal:
        p = self.positions.get(token_id)
        return p.shares if p else ZERO

    def apply_fill(self, fill: Fill) -> None:
        realised = self.position(fill.token_id).apply(fill)
        self.realised_pnl += realised
        self.fees_paid += fill.fee
        if fill.side is Side.BUY:
            self.cash -= fill.notional + fill.fee
        else:
            self.cash += fill.notional - fill.fee

    def unrealised(self, books: dict[str, Book]) -> Decimal:
        total = ZERO
        for tid, pos in self.positions.items():
            if pos.shares <= ZERO:
                continue
            b = books.get(tid)
            mark = b.mid if b and b.mid is not None else pos.avg_cost
            total += (mark - pos.avg_cost) * pos.shares
        return total

    def equity(self, books: dict[str, Book]) -> Decimal:
        inv = sum((p.cost for p in self.positions.values()), ZERO)
        return self.cash + inv + self.unrealised(books)

    def roll_day(self, books: dict[str, Book]) -> None:
        key = utcnow().strftime("%Y-%m-%d")
        if key != self.day_key:
            self.day_key = key
            self.day_start_equity = self.equity(books)

    def day_pnl(self, books: dict[str, Book]) -> Decimal:
        if self.day_start_equity is None:
            return ZERO
        return self.equity(books) - self.day_start_equity


@dataclass
class MarketState:
    markets: dict[str, Market] = field(default_factory=dict)   # condition_id -> Market
    books: dict[str, Book] = field(default_factory=dict)       # token_id -> Book
    token_to_market: dict[str, str] = field(default_factory=dict)
    geoblock_ok: bool = False
    geoblock_country: str = "?"
    last_trade: dict[str, tuple[Decimal, float]] = field(default_factory=dict)

    def set_markets(self, markets: list[Market]) -> None:
        self.markets = {m.condition_id: m for m in markets}
        self.token_to_market = {t.token_id: m.condition_id for m in markets for t in m.tokens}
        live = set(self.token_to_market)
        self.books = {t: b for t, b in self.books.items() if t in live}

    @property
    def token_ids(self) -> list[str]:
        return list(self.token_to_market)

    def market_for(self, token_id: str) -> Optional[Market]:
        cid = self.token_to_market.get(token_id)
        return self.markets.get(cid) if cid else None

    def update_book(self, book: Book) -> None:
        self.books[book.token_id] = book

    def markets_by_event(self) -> dict[str, list[Market]]:
        out: dict[str, list[Market]] = {}
        for m in self.markets.values():
            if m.event_id:
                out.setdefault(m.event_id, []).append(m)
        return out


@dataclass(frozen=True)
class Context:
    """Immutable view handed to strategies each tick. No I/O is possible from here."""

    now: datetime
    now_ts: float
    markets: dict[str, Market]
    books: dict[str, Book]
    token_to_market: dict[str, str]
    portfolio: Portfolio
    open_orders: list[Order]
    params: dict[str, Any]

    def market_for(self, token_id: str) -> Optional[Market]:
        cid = self.token_to_market.get(token_id)
        return self.markets.get(cid) if cid else None

    def book(self, token_id: str) -> Optional[Book]:
        return self.books.get(token_id)

    def shares(self, token_id: str) -> Decimal:
        return self.portfolio.shares(token_id)

    def open_orders_for(self, token_id: str) -> list[Order]:
        return [o for o in self.open_orders if o.token_id == token_id]

    def resting_notional(self, condition_id: str) -> Decimal:
        tids = {t.token_id for t in self.markets[condition_id].tokens} if condition_id in self.markets else set()
        return sum((o.remaining_notional for o in self.open_orders if o.token_id in tids), ZERO)

    def position_cost(self, condition_id: str) -> Decimal:
        m = self.markets.get(condition_id)
        if not m:
            return ZERO
        return sum((self.portfolio.position(t.token_id).cost for t in m.tokens), ZERO)


def build_context(ms: MarketState, pf: Portfolio, open_orders: list[Order], params: dict[str, Any]) -> Context:
    return Context(
        now=utcnow(),
        now_ts=time.time(),
        markets=ms.markets,
        books=ms.books,
        token_to_market=ms.token_to_market,
        portfolio=pf,
        open_orders=list(open_orders),
        params=params,
    )
