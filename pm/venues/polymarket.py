"""Polymarket (international, CLOB V2) venue."""

from __future__ import annotations

from typing import Callable, Optional

from ..clob import Clob, Geoblock
from ..config import Config
from ..execution import Exchange, Recorder
from ..execution.paper import PaperExchange
from ..feed import MarketFeed, UserFeed
from ..gamma import Gamma
from ..log import get_logger
from ..models import Book, Market, Mode
from ..universe import Universe
from . import BookHandler, TradeHandler

log = get_logger(__name__)


class PolymarketVenue:
    name = "polymarket"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.gamma = Gamma(cfg.gamma_url)
        self.clob = Clob(cfg.clob_url)
        self.geo = Geoblock(cfg.geoblock_url)
        self.universe = Universe(self.gamma, self.clob, cfg.universe)
        self.feed: Optional[MarketFeed] = None
        self.user_feed: Optional[UserFeed] = None

    async def check_access(self) -> tuple[bool, str]:
        ok, country = await self.geo.check()
        return ok, f"geoblock country={country}"

    async def select_universe(self) -> list[Market]:
        return await self.universe.select()

    async def seed_books(self, token_ids: list[str]) -> dict[str, Book]:
        return await self.clob.books(token_ids)

    def start_feed(self, token_ids: list[str], on_book: BookHandler, on_trade: TradeHandler) -> None:
        self.feed = MarketFeed(self.cfg.ws_url, on_book, on_trade)
        self.feed.start(token_ids)

    async def set_feed_tokens(self, token_ids: list[str]) -> None:
        if self.feed is not None:
            await self.feed.set_tokens(token_ids)

    def make_exchange(self, mode: Mode, market_for: Callable[[str], Optional[Market]]) -> Exchange:
        if mode is Mode.PAPER:
            return PaperExchange(market_for, self.cfg.execution.paper_starting_cash_usd)
        if mode is Mode.LIVE:
            from ..execution.live import LiveExchange
            return LiveExchange(self.cfg, market_for)
        return Recorder()

    async def after_exchange_start(self, exchange: Exchange) -> None:
        creds = getattr(exchange, "creds", None)
        if creds and creds[0] and hasattr(exchange, "on_user_order"):
            key, secret, passphrase = creds
            self.user_feed = UserFeed(self.cfg.ws_user_url, key, secret, passphrase,
                                      exchange.on_user_order, exchange.on_user_trade)
            self.user_feed.start()

    async def stop(self) -> None:
        if self.feed is not None:
            await self.feed.stop()
        if self.user_feed is not None:
            await self.user_feed.stop()
        await self.gamma.aclose()
        await self.clob.aclose()

    @property
    def feed_connected(self) -> bool:
        return bool(self.feed and self.feed.connected)

    @property
    def feed_messages(self) -> int:
        return self.feed.messages if self.feed else 0

    @property
    def private_feed_connected(self) -> Optional[bool]:
        return self.user_feed.connected if self.user_feed else None
