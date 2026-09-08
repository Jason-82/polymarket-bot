"""WebSocket market feed -> Book updates and trade prints.

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
Subscribe:  {"assets_ids": [...], "type": "market", "custom_feature_enabled": true}
Keepalive:  send "PING" every 10s.
Events:     book, price_change, last_trade_price, tick_size_change, best_bid_ask
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

import websockets

from .log import get_logger
from .models import D, Book, Level, Side

log = get_logger(__name__)


@dataclass(frozen=True)
class Trade:
    token_id: str
    price: Decimal
    size: Decimal
    side: Side          # aggressor side as reported by the venue
    ts: float


BookHandler = Callable[[Book], Any]
TradeHandler = Callable[[Trade], Any]


def _ts(v: Any) -> float:
    try:
        t = float(v)
        return t / 1000.0 if t > 1e12 else t
    except (TypeError, ValueError):
        return time.time()


class MarketFeed:
    def __init__(self, url: str, on_book: BookHandler, on_trade: TradeHandler):
        self.url = url
        self._on_book = on_book
        self._on_trade = on_trade
        self._tokens: set[str] = set()
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.connected = False
        self.messages = 0
        self.last_message_ts = 0.0
        self._books: dict[str, Book] = {}

    # ---- lifecycle
    def start(self, token_ids: list[str]) -> None:
        self._tokens = set(token_ids)
        self._task = asyncio.create_task(self._run(), name="market-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def set_tokens(self, token_ids: list[str]) -> None:
        """Change the subscription set; reconnects (the market channel has no unsubscribe)."""
        new = set(token_ids)
        if new == self._tokens:
            return
        self._tokens = new
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ---- loop
    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._tokens:
                await asyncio.sleep(1.0)
                continue
            try:
                async with websockets.connect(self.url, ping_interval=None, max_size=2**24) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({
                        "assets_ids": sorted(self._tokens),
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))
                    self.connected = True
                    backoff = 1.0
                    log.info("feed_connected", tokens=len(self._tokens))
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        async for raw in ws:
                            self._handle_raw(raw)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                break
            except Exception as e:  # network errors, 404s, etc.
                log.warning("feed_disconnected", error=str(e)[:200], retry_in=backoff)
            finally:
                self.connected = False
                self._ws = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _pinger(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                await ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # ---- parsing
    def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if raw == "PONG" or not raw:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        self.messages += 1
        self.last_message_ts = time.time()
        for msg in (data if isinstance(data, list) else [data]):
            if isinstance(msg, dict):
                try:
                    self._handle(msg)
                except Exception as e:
                    log.warning("feed_bad_message", error=str(e)[:200], event=msg.get("event_type"))

    def _handle(self, msg: dict[str, Any]) -> None:
        et = msg.get("event_type")
        if et == "book":
            tid = str(msg.get("asset_id") or "")
            if not tid:
                return
            book = self._books.setdefault(tid, Book(token_id=tid))
            book.replace(
                bids=[Level(D(l["price"]), D(l["size"])) for l in msg.get("bids") or []],
                asks=[Level(D(l["price"]), D(l["size"])) for l in msg.get("asks") or []],
                ts=time.time(),
                hash=str(msg.get("hash") or ""),
            )
            self._on_book(book)

        elif et == "price_change":
            changes = msg.get("price_changes") or msg.get("changes") or []
            touched: set[str] = set()
            for ch in changes:
                tid = str(ch.get("asset_id") or msg.get("asset_id") or "")
                if not tid:
                    continue
                book = self._books.setdefault(tid, Book(token_id=tid))
                side = Side.BUY if str(ch.get("side", "")).upper() == "BUY" else Side.SELL
                book.set_level(side, D(ch["price"]), D(ch["size"]), ts=time.time())
                touched.add(tid)
            for tid in touched:
                self._on_book(self._books[tid])

        elif et == "last_trade_price":
            tid = str(msg.get("asset_id") or "")
            if not tid:
                return
            self._on_trade(Trade(
                token_id=tid,
                price=D(msg.get("price")),
                size=D(msg.get("size") or 0),
                side=Side.BUY if str(msg.get("side", "")).upper() == "BUY" else Side.SELL,
                ts=_ts(msg.get("timestamp")),
            ))
        # best_bid_ask / tick_size_change / new_market / market_resolved: informational; books carry the truth.
