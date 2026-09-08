"""Polymarket US venue (CFTC-regulated, USD, Ed25519-keyed API).

Model mapping
- One US market = one contract with a unified book. We expose it as a binary
  Market with two synthetic tokens: "<slug>|L" (long / the listed outcome) and
  "<slug>|S" (short). The short book is the long book mirrored: a short bid at q
  is a long offer at 1 - q. Every strategy therefore runs unchanged.
- Intent(token |L, BUY)  -> ORDER_INTENT_BUY_LONG      Intent(|L, SELL) -> SELL_LONG
  Intent(token |S, BUY)  -> ORDER_INTENT_BUY_SHORT     Intent(|S, SELL) -> SELL_SHORT
  Short orders are priced as the short contract (q), which is what the strategy
  already computes for the NO side.
- The venue nets long and short into cash; local positions are reconciled from
  the portfolio endpoint so equity never double counts.

Fees (effective 2026-07-01): taker 0.06 x C x p(1-p); maker REBATE 0.0125 x C x p(1-p).

Market data: authenticated WebSocket for up to ws_max_markets slugs (the venue
limits streaming), REST book polling for the rest. Without API keys only REST
polling is available.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from ..config import Config
from ..execution import Exchange, ExchangeError, Recorder
from ..execution.paper import PaperExchange
from ..feed import Trade
from ..fees import maker_rebate, taker_fee
from ..gamma import VS_RE
from ..log import get_logger
from ..models import ONE, ZERO, D, Book, Fill, Intent, Level, Market, Mode, Order, OrderStatus, Side, TimeInForce, Token
from ..universe import passes_filters
from . import BookHandler, TradeHandler

log = get_logger(__name__)

LONG = "|L"
SHORT = "|S"
US_TAKER_RATE = Decimal("0.06")
US_MAKER_REBATE = Decimal("0.0125")
US_TICK = Decimal("0.01")

INTENT = {
    (False, Side.BUY): "ORDER_INTENT_BUY_LONG",
    (False, Side.SELL): "ORDER_INTENT_SELL_LONG",
    (True, Side.BUY): "ORDER_INTENT_BUY_SHORT",
    (True, Side.SELL): "ORDER_INTENT_SELL_SHORT",
}
TIF = {
    TimeInForce.GTC: "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    TimeInForce.FAK: "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    TimeInForce.FOK: "TIME_IN_FORCE_FILL_OR_KILL",
}
DONE_STATES = {"ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED",
               "ORDER_STATE_REPLACED"}
OPEN_MARKET_STATES = {"MARKET_STATE_OPEN", ""}


# ------------------------------------------------------------------ pure mapping helpers

def token_ids(slug: str) -> tuple[str, str]:
    return slug + LONG, slug + SHORT


def split_token(token_id: str) -> tuple[str, bool]:
    if token_id.endswith(SHORT):
        return token_id[:-len(SHORT)], True
    if token_id.endswith(LONG):
        return token_id[:-len(LONG)], False
    return token_id, False


def _amount(v: Any) -> Decimal:
    if isinstance(v, dict):
        v = v.get("value")
    return D(v)


def _dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def market_from_us(row: dict[str, Any], event: Optional[dict[str, Any]] = None) -> Optional[Market]:
    slug = row.get("slug")
    if not slug:
        return None
    ev = event or {}
    title = row.get("title") or ev.get("title") or slug
    outcome = row.get("outcome") or "Yes"
    end = None
    for src in (row, ev):
        for k in ("endDate", "endTime", "closeTime", "closeDate", "expirationTime", "resolutionTime",
                  "settlementTime", "expiresAt", "endsAt"):
            end = end or _dt(src.get(k))
    tags: set[str] = set()
    for src in (row, ev):
        for k in ("category", "categories", "tags"):
            v = src.get(k)
            for t in (v if isinstance(v, list) else [v]):
                if t:
                    tags.add(str(t.get("label") if isinstance(t, dict) else t).lower())
    text = f"{title} {ev.get('title', '')} {row.get('eventSlug', '')}"
    if row.get("team") or ev.get("team") or ev.get("gameId") or row.get("gameId") or VS_RE.search(text) \
            or any("sport" in t for t in tags):
        tags.add("sports")
    state = str(row.get("state") or "")
    long_id, short_id = token_ids(slug)
    return Market(
        condition_id=slug,
        question=f"{title} [{outcome}]",
        slug=slug,
        tokens=[Token(long_id, outcome), Token(short_id, f"NOT {outcome}")],
        neg_risk=False,
        event_id=str(row.get("eventSlug") or ev.get("slug") or ""),
        event_slug=str(row.get("eventSlug") or ev.get("slug") or ""),
        tick_size=US_TICK,
        min_order_size=Decimal("1"),
        fee_rate=US_TAKER_RATE,
        fee_exponent=ONE,
        fee_known=True,
        maker_rebate_rate=US_MAKER_REBATE,
        end_date=end,
        liquidity_usd=D(row.get("liquidity") or 0),
        volume_24h_usd=D(row.get("volume24h") or row.get("volume24hr") or row.get("volume") or 0),
        tags=sorted(tags),
        accepting_orders=bool(row.get("active", True)) and not bool(row.get("closed", False))
        and state in OPEN_MARKET_STATES,
    )


def books_from_us(slug: str, data: dict[str, Any], ts: Optional[float] = None) -> tuple[Book, Book]:
    """Long book from the venue payload, plus the mirrored short book."""
    ts = ts or time.time()
    bids = [Level(_amount(l.get("px")), D(l.get("qty"))) for l in data.get("bids") or []]
    offers = [Level(_amount(l.get("px")), D(l.get("qty") or l.get("quantity"))) for l in data.get("offers") or data.get("asks") or []]
    long_id, short_id = token_ids(slug)
    long_book = Book(token_id=long_id)
    long_book.replace(bids, offers, ts=ts)
    short_book = Book(token_id=short_id)
    short_book.replace(
        bids=[Level(ONE - l.price, l.size) for l in offers],
        asks=[Level(ONE - l.price, l.size) for l in bids],
        ts=ts,
    )
    return long_book, short_book


def trades_from_us(slug: str, payload: dict[str, Any]) -> list[Trade]:
    """A long trade at p is also a short trade at 1-p with the opposite aggressor side."""
    price = _amount(payload.get("price"))
    qty = _amount(payload.get("quantity") or payload.get("qty") or 0)
    taker = payload.get("taker") or {}
    side_s = str(taker.get("side") or "").upper()
    intent = str(taker.get("intent") or "").upper()
    if "BUY" in side_s or intent.endswith("BUY_LONG") or intent.endswith("SELL_SHORT"):
        side = Side.BUY
    else:
        side = Side.SELL
    ts = time.time()
    t = _dt(payload.get("tradeTime"))
    if t is not None:
        ts = t.timestamp()
    long_id, short_id = token_ids(slug)
    return [
        Trade(long_id, price, qty, side, ts),
        Trade(short_id, ONE - price, qty, side.opposite, ts),
    ]


def order_params(intent: Intent) -> dict[str, Any]:
    slug, short = split_token(intent.token_id)
    params: dict[str, Any] = {
        "marketSlug": slug,
        "intent": INTENT[(short, intent.side)],
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": f"{intent.price:.2f}", "currency": "USD"},
        "quantity": int(intent.size),
        "tif": TIF[intent.tif],
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    if intent.post_only and intent.tif is TimeInForce.GTC:
        params["participateDontInitiate"] = True
    return params


def positions_from_us(payload: dict[str, Any]) -> dict[str, tuple[Decimal, Decimal]]:
    """{token_id: (shares, cost)} from the portfolio positions response. Net > 0 = long."""
    out: dict[str, tuple[Decimal, Decimal]] = {}
    positions = payload.get("positions") or {}
    items = positions.items() if isinstance(positions, dict) else [(p.get("marketSlug") or (p.get("marketMetadata") or {}).get("slug"), p) for p in positions]
    for slug, p in items:
        if not slug or not isinstance(p, dict):
            continue
        slug = (p.get("marketMetadata") or {}).get("slug") or slug
        net = D(p.get("netPosition") or 0)
        cost = abs(_amount(p.get("cost") or 0))
        long_id, short_id = token_ids(str(slug))
        if net > ZERO:
            out[long_id] = (net, cost)
        elif net < ZERO:
            out[short_id] = (-net, cost)
    return out


# ------------------------------------------------------------------ market data

class USMarketData:
    def __init__(self, cfg: Config, public_client, on_book: BookHandler, on_trade: TradeHandler):
        self.cfg = cfg
        self.client = public_client
        self._on_book = on_book
        self._on_trade = on_trade
        self._slugs: list[str] = []
        self._ws_slugs: list[str] = []
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._ws = None
        self.connected = False
        self.messages = 0

    def start(self, slugs: list[str]) -> None:
        self._slugs = list(slugs)
        s = self.cfg.secrets
        n = self.cfg.venue_us.ws_max_markets
        self._ws_slugs = self._slugs[:n] if (s.us_key_id and s.us_secret_key and n > 0) else []
        self._tasks = [asyncio.create_task(self._poll_loop(), name="us-book-poll")]
        if self._ws_slugs:
            self._tasks.append(asyncio.create_task(self._ws_loop(), name="us-ws"))

    async def set_slugs(self, slugs: list[str]) -> None:
        if list(slugs) == self._slugs:
            return
        await self.stop()
        self._stop = asyncio.Event()
        self.start(slugs)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        self.connected = False

    async def _poll_loop(self) -> None:
        interval = self.cfg.venue_us.book_poll_seconds
        sem = asyncio.Semaphore(4)

        async def one(slug: str) -> None:
            async with sem:
                try:
                    data = await self.client.markets.book(slug)
                except Exception as e:
                    log.debug("us_book_poll_failed", slug=slug, error=str(e)[:120])
                    return
                self.messages += 1
                for b in books_from_us(slug, data or {}):
                    self._on_book(b)

        while not self._stop.is_set():
            t0 = time.time()
            targets = [s for s in self._slugs if s not in self._ws_slugs] or self._slugs
            await asyncio.gather(*(one(s) for s in targets))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.5, interval - (time.time() - t0)))
            except asyncio.TimeoutError:
                pass

    async def _ws_loop(self) -> None:
        from polymarket_us.websocket import MarketsWebSocket

        s = self.cfg.secrets
        backoff = 1.0
        while not self._stop.is_set():
            ws = MarketsWebSocket(key_id=s.us_key_id, secret_key=s.us_secret_key, base_url=self.cfg.venue_us.ws_url)
            ws.on("market_data", self._on_market_data)
            ws.on("trade", self._on_trade_msg)
            ws.on("error", lambda e: log.warning("us_ws_error", error=str(e)[:200]))
            try:
                await ws.connect()
                self._ws = ws
                await ws.subscribe_market_data("md", self._ws_slugs)
                await ws.subscribe_trades("tr", self._ws_slugs)
                self.connected = True
                backoff = 1.0
                log.info("us_ws_connected", markets=len(self._ws_slugs))
                while not self._stop.is_set() and ws.is_connected:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("us_ws_disconnected", error=str(e)[:200], retry_in=backoff)
            finally:
                self.connected = False
                self._ws = None
                try:
                    await ws.close()
                except Exception:
                    pass
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _on_market_data(self, msg: dict[str, Any]) -> None:
        payload = msg.get("marketData") or {}
        slug = payload.get("marketSlug")
        if not slug:
            return
        self.messages += 1
        for b in books_from_us(slug, payload):
            self._on_book(b)

    def _on_trade_msg(self, msg: dict[str, Any]) -> None:
        payload = msg.get("trade") or {}
        slug = payload.get("marketSlug")
        if not slug:
            return
        self.messages += 1
        for t in trades_from_us(slug, payload):
            self._on_trade(t)


# ------------------------------------------------------------------ live exchange

class USExchange:
    def __init__(self, cfg: Config, market_for: Callable[[str], Optional[Market]]):
        self.cfg = cfg
        self._market_for = market_for
        self._client = None
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._last_poll = 0.0
        self._lock = asyncio.Lock()
        self._private_task: Optional[asyncio.Task] = None
        self._private_ws = None
        self._stop = asyncio.Event()
        self.private_connected = False
        self.user_feed_ok = False

    async def start(self) -> None:
        s = self.cfg.secrets
        if not s.live_trading_ack:
            raise ExchangeError("LIVE_TRADING=yes is required in .env for live mode")
        if not (s.us_key_id and s.us_secret_key):
            raise ExchangeError("PM_US_KEY_ID and PM_US_SECRET_KEY are required for Polymarket US live mode")
        from polymarket_us import AsyncPolymarketUS
        self._client = AsyncPolymarketUS(key_id=s.us_key_id, secret_key=s.us_secret_key,
                                         gateway_base_url=self.cfg.venue_us.gateway_url,
                                         api_base_url=self.cfg.venue_us.api_url)
        await self._client.orders.cancel_all()
        bal = await self.balance()
        self._private_task = asyncio.create_task(self._private_loop(), name="us-private-ws")
        log.info("us_exchange_started", key_id=s.us_key_id[:8] + "…", buying_power=str(bal))

    async def stop(self) -> None:
        self._stop.set()
        if self._private_ws is not None:
            try:
                await self._private_ws.close()
            except Exception:
                pass
        if self._private_task:
            self._private_task.cancel()
            try:
                await self._private_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            try:
                await self._client.orders.cancel_all()
            except Exception as e:
                log.error("us_cancel_all_failed", error=str(e)[:200])
            try:
                await self._client.close()
            except Exception:
                pass

    # ---- exchange API
    async def place(self, intent: Intent) -> Order:
        params = order_params(intent)
        try:
            resp = await self._client.orders.create(params)
        except Exception as e:
            raise ExchangeError(str(e)[:300]) from e
        if not isinstance(resp, dict) or not resp.get("id"):
            raise ExchangeError(f"unexpected response: {resp!r}"[:300])
        order = Order(
            order_id=str(resp["id"]), tag=intent.tag, strategy=intent.strategy, token_id=intent.token_id,
            side=intent.side, price=intent.price, size=intent.size, tif=intent.tif, post_only=intent.post_only,
        )
        self._orders[order.order_id] = order
        for exe in resp.get("executions") or []:
            self._apply_execution(order, exe)
            if order.status is OrderStatus.REJECTED:
                reason = exe.get("orderRejectReason") or exe.get("text") or "rejected"
                raise ExchangeError(str(reason)[:300])
        return order

    async def cancel(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        slug = split_token(o.token_id)[0] if o else ""
        try:
            await self._client.orders.cancel(order_id, {"marketSlug": slug})
        except Exception as e:
            raise ExchangeError(str(e)[:300]) from e
        if o is not None:
            await self._reconcile_order(o)

    async def cancel_all(self) -> None:
        await self._client.orders.cancel_all()
        for o in list(self._orders.values()):
            await self._reconcile_order(o)

    async def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status is OrderStatus.OPEN]

    async def drain_fills(self) -> list[Fill]:
        now = time.time()
        interval = self.cfg.execution.reconcile_seconds if self.private_connected else self.cfg.execution.fill_poll_seconds
        if now - self._last_poll >= interval and self._orders:
            self._last_poll = now
            await self._poll()
        out, self._fills = self._fills, []
        return out

    async def balance(self) -> Optional[Decimal]:
        try:
            resp = await self._client.account.balances()
        except Exception as e:
            log.warning("us_balance_failed", error=str(e)[:200])
            return None
        rows = (resp or {}).get("balances") or []
        if not rows:
            return None
        b = rows[0]
        v = b.get("buyingPower", b.get("currentBalance"))
        return D(v) if v is not None else None

    async def positions(self) -> Optional[dict[str, tuple[Decimal, Decimal]]]:
        try:
            resp = await self._client.portfolio.positions()
        except Exception as e:
            log.warning("us_positions_failed", error=str(e)[:200])
            return None
        return positions_from_us(resp or {})

    # ---- internals
    async def _poll(self) -> None:
        async with self._lock:
            try:
                resp = await self._client.orders.list()
            except Exception as e:
                log.warning("us_open_orders_failed", error=str(e)[:200])
                return
            live = {str(r.get("id")): r for r in (resp or {}).get("orders") or []}
            for oid, o in list(self._orders.items()):
                if o.status is not OrderStatus.OPEN:
                    continue
                row = live.get(oid)
                if row is not None:
                    self._apply_order_row(o, row)
                else:
                    await self._reconcile_order(o)

    async def _reconcile_order(self, o: Order) -> None:
        try:
            resp = await self._client.orders.retrieve(o.order_id)
        except Exception as e:
            log.warning("us_order_fetch_failed", order_id=o.order_id, error=str(e)[:200])
            return
        row = (resp or {}).get("order") or {}
        if not row:
            o.status = OrderStatus.CANCELLED
            self._orders.pop(o.order_id, None)
            return
        self._apply_order_row(o, row)

    def _apply_order_row(self, o: Order, row: dict[str, Any], fill_price: Optional[Decimal] = None,
                         aggressor: Optional[bool] = None) -> None:
        cum = D(row.get("cumQuantity") or 0)
        state = str(row.get("state") or "")
        delta = cum - o.filled
        if delta > ZERO:
            m = self._market_for(o.token_id)
            px = fill_price if fill_price is not None else (_amount(row["avgPx"]) if row.get("avgPx") else o.price)
            is_maker = (not aggressor) if aggressor is not None else o.post_only
            if m is None:
                fee = ZERO
            elif is_maker:
                fee = -maker_rebate(px, delta, m)
            else:
                fee = taker_fee(px, delta, m)
            self._fills.append(Fill(order_id=o.order_id, tag=o.tag, strategy=o.strategy, token_id=o.token_id,
                                    side=o.side, price=px, size=delta, fee=fee, maker=is_maker))
            o.filled = cum
            log.info("us_fill", tag=o.tag, side=o.side.value, size=str(delta), price=str(px), maker=is_maker)
        leaves = row.get("leavesQuantity")
        done = state in DONE_STATES or (leaves is not None and D(leaves) <= ZERO)
        if state == "ORDER_STATE_REJECTED":
            o.status = OrderStatus.REJECTED
            self._orders.pop(o.order_id, None)
        elif o.remaining <= ZERO:
            o.status = OrderStatus.FILLED
            self._orders.pop(o.order_id, None)
        elif done:
            o.status = OrderStatus.CANCELLED
            self._orders.pop(o.order_id, None)

    def _apply_execution(self, o: Order, exe: dict[str, Any]) -> None:
        row = exe.get("order") or {}
        px = _amount(exe["lastPx"]) if exe.get("lastPx") else None
        aggressor = exe.get("aggressor")
        venue_fee = exe.get("commissionNotionalCollected")
        if venue_fee is not None:
            log.debug("us_venue_fee", order_id=o.order_id, fee=_amount(venue_fee))
        self._apply_order_row(o, row, fill_price=px, aggressor=aggressor)

    def on_order_update(self, msg: dict[str, Any]) -> None:
        upd = msg.get("orderSubscriptionUpdate") or msg.get("orderUpdate") or {}
        exe = upd.get("execution") or {}
        row = exe.get("order") or {}
        oid = str(row.get("id") or "")
        o = self._orders.get(oid)
        if o is None or o.status is not OrderStatus.OPEN:
            return
        self._apply_execution(o, exe)

    async def _private_loop(self) -> None:
        from polymarket_us.websocket import PrivateWebSocket

        s = self.cfg.secrets
        backoff = 1.0
        while not self._stop.is_set():
            ws = PrivateWebSocket(key_id=s.us_key_id, secret_key=s.us_secret_key, base_url=self.cfg.venue_us.ws_url)
            ws.on("order_update", self.on_order_update)
            ws.on("error", lambda e: log.warning("us_private_ws_error", error=str(e)[:200]))
            try:
                await ws.connect()
                self._private_ws = ws
                await ws.subscribe_orders("orders")
                self.private_connected = True
                backoff = 1.0
                log.info("us_private_ws_connected")
                while not self._stop.is_set() and ws.is_connected:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("us_private_ws_disconnected", error=str(e)[:200], retry_in=backoff)
            finally:
                self.private_connected = False
                self._private_ws = None
                try:
                    await ws.close()
                except Exception:
                    pass
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# ------------------------------------------------------------------ venue

class PolymarketUSVenue:
    name = "polymarket_us"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        from polymarket_us import AsyncPolymarketUS
        s = cfg.secrets
        self.public = AsyncPolymarketUS(
            key_id=s.us_key_id or None, secret_key=s.us_secret_key or None,
            gateway_base_url=cfg.venue_us.gateway_url, api_base_url=cfg.venue_us.api_url,
        )
        self.data: Optional[USMarketData] = None
        self._exchange: Optional[USExchange] = None

    async def check_access(self) -> tuple[bool, str]:
        s = self.cfg.secrets
        if not (s.us_key_id and s.us_secret_key):
            return True, "no US API key configured (read-only access)"
        try:
            bal = await self.public.account.balances()
        except Exception as e:
            return False, f"US API auth failed: {str(e)[:120]}"
        rows = (bal or {}).get("balances") or []
        bp = rows[0].get("buyingPower") if rows else None
        return True, f"US API auth ok buying_power={bp}"

    async def list_events_raw(self, limit: int = 300) -> list[dict[str, Any]]:
        """Active events by volume. Events carry endTime, tags and their markets."""
        out: list[dict[str, Any]] = []
        offset, page = 0, 100
        while len(out) < limit:
            resp = await self.public.events.list({"limit": page, "offset": offset, "active": True, "closed": False,
                                                  "orderBy": ["volume"], "orderDirection": "desc"})
            rows = (resp or {}).get("events") or []
            out.extend(rows)
            if len(rows) < page:
                break
            offset += page
        return out[:limit]

    async def all_markets(self, limit_events: int = 300) -> list[Market]:
        markets: list[Market] = []
        for ev in await self.list_events_raw(limit_events):
            for row in ev.get("markets") or []:
                m = market_from_us(row, ev)
                if m and m.accepting_orders:
                    markets.append(m)
        markets.sort(key=lambda m: (m.volume_24h_usd, m.liquidity_usd), reverse=True)
        return markets

    async def select_universe(self) -> list[Market]:
        c = self.cfg.universe
        markets = await self.all_markets(limit_events=max(200, c.max_markets * 5))
        selected = [m for m in markets if passes_filters(m, c, require_end_date=False)][: c.max_markets]
        log.info("us_universe_selected", candidates=len(markets), markets=len(selected),
                 with_end_date=sum(1 for m in selected if m.end_date))
        return selected

    async def seed_books(self, token_ids_: list[str]) -> dict[str, Book]:
        out: dict[str, Book] = {}
        slugs = sorted({split_token(t)[0] for t in token_ids_})
        sem = asyncio.Semaphore(4)

        async def one(slug: str) -> None:
            async with sem:
                try:
                    data = await self.public.markets.book(slug)
                except Exception as e:
                    log.debug("us_seed_book_failed", slug=slug, error=str(e)[:120])
                    return
                for b in books_from_us(slug, data or {}):
                    out[b.token_id] = b

        await asyncio.gather(*(one(s) for s in slugs))
        return out

    def start_feed(self, token_ids_: list[str], on_book: BookHandler, on_trade: TradeHandler) -> None:
        self.data = USMarketData(self.cfg, self.public, on_book, on_trade)
        self.data.start(sorted({split_token(t)[0] for t in token_ids_}))

    async def set_feed_tokens(self, token_ids_: list[str]) -> None:
        if self.data is not None:
            await self.data.set_slugs(sorted({split_token(t)[0] for t in token_ids_}))

    def make_exchange(self, mode: Mode, market_for: Callable[[str], Optional[Market]]) -> Exchange:
        if mode is Mode.PAPER:
            return PaperExchange(market_for, self.cfg.execution.paper_starting_cash_usd)
        if mode is Mode.LIVE:
            self._exchange = USExchange(self.cfg, market_for)
            return self._exchange
        return Recorder()

    async def after_exchange_start(self, exchange: Exchange) -> None:
        return None

    async def stop(self) -> None:
        if self.data is not None:
            await self.data.stop()
        try:
            await self.public.close()
        except Exception:
            pass

    @property
    def feed_connected(self) -> bool:
        return bool(self.data and (self.data.connected or self.data.messages > 0))

    @property
    def feed_messages(self) -> int:
        return self.data.messages if self.data else 0

    @property
    def private_feed_connected(self) -> Optional[bool]:
        return self._exchange.private_connected if self._exchange else None
