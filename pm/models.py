"""Core data model. Everything is Decimal; floats appear only at the SDK boundary."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from enum import Enum
from typing import Iterable, Optional

ZERO = Decimal("0")
ONE = Decimal("1")


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class TimeInForce(str, Enum):
    GTC = "GTC"  # rest on the book
    FAK = "FAK"  # fill what is available now, cancel the rest
    FOK = "FOK"  # all or nothing, now


class Mode(str, Enum):
    READ_ONLY = "read_only"
    PAPER = "paper"
    LIVE = "live"


def D(x) -> Decimal:
    """Lenient Decimal constructor for API strings/floats."""
    if isinstance(x, Decimal):
        return x
    if x is None or x == "":
        return ZERO
    return Decimal(str(x))


def round_to_tick(price: Decimal, tick: Decimal, side: Side) -> Decimal:
    """Round a price to the tick grid conservatively for the maker:
    bids round down, asks round up (never accidentally cross)."""
    q = (price / tick).to_integral_value(rounding=ROUND_DOWN if side is Side.BUY else ROUND_UP)
    return (q * tick).quantize(tick)


def clamp_price(price: Decimal, tick: Decimal) -> Decimal:
    return max(tick, min(ONE - tick, price))


# --------------------------------------------------------------------------- book

@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass
class Book:
    """Order book for one token. bids sorted descending, asks ascending."""

    token_id: str
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    ts: float = field(default_factory=time.time)  # local receipt time (epoch s)
    hash: str = ""

    # ---- top of book
    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[Decimal]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> Optional[Decimal]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def bid_size_at_touch(self) -> Decimal:
        return self.bids[0].size if self.bids else ZERO

    @property
    def ask_size_at_touch(self) -> Decimal:
        return self.asks[0].size if self.asks else ZERO

    def age(self, now: Optional[float] = None) -> float:
        return (now or time.time()) - self.ts

    # ---- mutation
    @staticmethod
    def _sorted(levels: Iterable[Level], descending: bool) -> list[Level]:
        return sorted((l for l in levels if l.size > ZERO), key=lambda l: l.price, reverse=descending)

    def replace(self, bids: Iterable[Level], asks: Iterable[Level], ts: Optional[float] = None, hash: str = "") -> None:
        self.bids = self._sorted(bids, descending=True)
        self.asks = self._sorted(asks, descending=False)
        self.ts = ts or time.time()
        self.hash = hash

    def set_level(self, side: Side, price: Decimal, size: Decimal, ts: Optional[float] = None) -> None:
        """Apply a price_change: size is the new absolute size at that price (0 removes)."""
        levels = self.bids if side is Side.BUY else self.asks
        kept = [l for l in levels if l.price != price]
        if size > ZERO:
            kept.append(Level(price, size))
        if side is Side.BUY:
            self.bids = self._sorted(kept, descending=True)
        else:
            self.asks = self._sorted(kept, descending=False)
        self.ts = ts or time.time()

    # ---- walking
    def walk(self, side: Side, shares: Decimal) -> tuple[Decimal, Decimal]:
        """Cost/proceeds of taking `shares` from the opposite side of the book.

        BUY walks the asks, SELL walks the bids.
        Returns (filled_shares, total_notional). Partial if the book is thin.
        """
        levels = self.asks if side is Side.BUY else self.bids
        remaining = shares
        notional = ZERO
        filled = ZERO
        for lvl in levels:
            take = min(remaining, lvl.size)
            notional += take * lvl.price
            filled += take
            remaining -= take
            if remaining <= ZERO:
                break
        return filled, notional

    def vwap(self, side: Side, shares: Decimal) -> Optional[Decimal]:
        filled, notional = self.walk(side, shares)
        if filled < shares or filled == ZERO:
            return None
        return notional / filled

    def depth_within(self, side: Side, distance: Decimal) -> Decimal:
        """Shares resting within `distance` of the touch on `side` (BUY=bids, SELL=asks)."""
        levels = self.bids if side is Side.BUY else self.asks
        if not levels:
            return ZERO
        touch = levels[0].price
        return sum((l.size for l in levels if abs(l.price - touch) <= distance), ZERO)

    def top(self, n: int = 5) -> dict:
        return {
            "bids": [[str(l.price), str(l.size)] for l in self.bids[:n]],
            "asks": [[str(l.price), str(l.size)] for l in self.asks[:n]],
        }


# --------------------------------------------------------------------------- markets

@dataclass(frozen=True)
class Token:
    token_id: str
    outcome: str  # "Yes" / "No" / candidate name


@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    tokens: list[Token]
    neg_risk: bool = False
    neg_risk_augmented: bool = False
    event_id: str = ""
    event_slug: str = ""
    event_market_count: int = 0        # active markets in the event (for neg-risk completeness)
    tick_size: Decimal = Decimal("0.01")
    min_order_size: Decimal = Decimal("5")
    fee_rate: Decimal = ZERO          # taker fee rate r  (fee = shares * r * (p(1-p))^e)
    fee_exponent: Decimal = ONE
    fee_known: bool = False
    end_date: Optional[datetime] = None
    liquidity_usd: Decimal = ZERO
    volume_24h_usd: Decimal = ZERO
    tags: list[str] = field(default_factory=list)
    accepting_orders: bool = True

    @property
    def yes(self) -> Token:
        for t in self.tokens:
            if t.outcome.lower() == "yes":
                return t
        return self.tokens[0]

    @property
    def no(self) -> Token:
        for t in self.tokens:
            if t.outcome.lower() == "no":
                return t
        return self.tokens[1] if len(self.tokens) > 1 else self.tokens[0]

    @property
    def is_binary(self) -> bool:
        return len(self.tokens) == 2

    def seconds_to_resolution(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.end_date is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.end_date - now).total_seconds()

    def other_token(self, token_id: str) -> Optional[Token]:
        for t in self.tokens:
            if t.token_id != token_id:
                return t
        return None


# --------------------------------------------------------------------------- orders

@dataclass
class Intent:
    """What a strategy wants to exist on the book. Declarative and idempotent by `tag`."""

    strategy: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal                      # shares
    tif: TimeInForce = TimeInForce.GTC
    post_only: bool = True             # maker by default; taker must opt in
    reason: str = ""
    tag: str = ""                      # stable key, e.g. "maker:<token>:BUY"

    def __post_init__(self) -> None:
        if not self.tag:
            self.tag = f"{self.strategy}:{self.token_id}:{self.side.value}"

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    order_id: str
    tag: str
    strategy: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    filled: Decimal = ZERO
    status: OrderStatus = OrderStatus.OPEN
    tif: TimeInForce = TimeInForce.GTC
    post_only: bool = True
    created_ts: float = field(default_factory=time.time)

    @property
    def remaining(self) -> Decimal:
        return self.size - self.filled

    @property
    def remaining_notional(self) -> Decimal:
        return self.remaining * self.price


@dataclass
class Fill:
    order_id: str
    tag: str
    strategy: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    fee: Decimal = ZERO
    maker: bool = True
    ts: float = field(default_factory=time.time)

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass
class Position:
    token_id: str
    shares: Decimal = ZERO
    cost: Decimal = ZERO  # total dollars paid for current shares (avg cost * shares)

    @property
    def avg_cost(self) -> Decimal:
        return self.cost / self.shares if self.shares > ZERO else ZERO

    def apply(self, fill: Fill) -> Decimal:
        """Update with a fill; return realised P&L (non-zero only on sells)."""
        if fill.side is Side.BUY:
            self.shares += fill.size
            self.cost += fill.notional + fill.fee
            return ZERO
        # sell: realise against average cost
        avg = self.avg_cost
        sold = min(fill.size, self.shares)
        realised = (fill.price - avg) * sold - fill.fee
        self.shares -= sold
        self.cost -= avg * sold
        if self.shares <= ZERO:
            self.shares, self.cost = ZERO, ZERO
        return realised


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
