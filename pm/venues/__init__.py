"""Venue abstraction: everything venue-specific lives behind this interface.

A venue provides market discovery, market data (books + trades) and an
Exchange for the chosen mode. Strategies, risk, OMS, store and backtest never
see which venue they are on; they only see Markets, Books and Intents.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from ..execution import Exchange
from ..feed import Trade
from ..models import Book, Market, Mode

BookHandler = Callable[[Book], Any]
TradeHandler = Callable[[Trade], Any]


class Venue(Protocol):
    name: str

    async def check_access(self) -> tuple[bool, str]: ...
    async def select_universe(self) -> list[Market]: ...
    async def seed_books(self, token_ids: list[str]) -> dict[str, Book]: ...
    def start_feed(self, token_ids: list[str], on_book: BookHandler, on_trade: TradeHandler) -> None: ...
    async def set_feed_tokens(self, token_ids: list[str]) -> None: ...
    def make_exchange(self, mode: Mode, market_for: Callable[[str], Optional[Market]]) -> Exchange: ...
    async def after_exchange_start(self, exchange: Exchange) -> None: ...
    async def stop(self) -> None: ...

    @property
    def feed_connected(self) -> bool: ...
    @property
    def feed_messages(self) -> int: ...
    @property
    def private_feed_connected(self) -> Optional[bool]: ...


VENUES = ("polymarket", "polymarket_us")


def make_venue(cfg) -> Venue:
    name = (cfg.venue or "polymarket").lower().replace("-", "_")
    if name in ("us", "polymarket_us", "polymarketus"):
        from .polymarket_us import PolymarketUSVenue
        return PolymarketUSVenue(cfg)
    if name in ("polymarket", "intl", "clob"):
        from .polymarket import PolymarketVenue
        return PolymarketVenue(cfg)
    raise ValueError(f"unknown venue '{cfg.venue}' (known: {VENUES})")
