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
from ..gamma import SPORT_WORDS, VS_RE
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


def _quote_px(v: Any) -> Optional[Decimal]:
    """bestBidQuote / bestAskQuote / outcomePrices entries come in several shapes; extract a price."""
    if v is None or v == "":
        return None
    if isinstance(v, dict):
        for k in ("px", "price", "value"):
            if v.get(k) is not None:
                return _quote_px(v[k])
        return None
    if isinstance(v, (list, tuple)):
        return _quote_px(v[0]) if v else None
    try:
        p = D(v)
    except Exception:
        return None
    if p > ONE and p <= 100:      # cents
        p = p / 100
    return p if ZERO < p < ONE else None


def quote_hints(row: dict[str, Any]) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """(best_bid, best_ask) for the long side from the listing row, if the venue provides them."""
    bid = _quote_px(row.get("bestBidQuote"))
    ask = _quote_px(row.get("bestAskQuote"))
    if bid is None and ask is None:
        prices = row.get("outcomePrices")
        if isinstance(prices, str):
            try:
                import json
                prices = json.loads(prices)
            except ValueError:
                prices = None
        if isinstance(prices, list) and prices:
            p = _quote_px(prices[0])
            if p is not None:
                bid = ask = p
    return bid, ask


def market_from_us(row: dict[str, Any], event: Optional[dict[str, Any]] = None) -> Optional[Market]:
    slug = row.get("slug")
    if not slug:
        return None
    ev = event or {}
    ev_title = ev.get("title") or ""
    sub = row.get("subject") or {}
    sub_name = sub.get("name") if isinstance(sub, dict) else None
    label = row.get("title") or row.get("question") or sub_name or slug
    question = f"{ev_title}: {label}" if ev_title and label and label != ev_title else (label or ev_title)
    outcomes = row.get("outcomes")
    if isinstance(outcomes, str):
        try:
            import json
            outcomes = json.loads(outcomes)
        except ValueError:
            outcomes = None
    outcome = row.get("outcome") or (str(outcomes[0]) if isinstance(outcomes, list) and outcomes else "Yes")
    # Market endDate is often a trading-close buffer months after the event; the event endDate is the
    # resolution-relevant one. Use the earlier of the two.
    dates = []
    for src in (row, ev):
        for k in ("endDate", "endTime", "closeTime", "closeDate", "expirationTime", "resolutionTime",
                  "settlementTime", "expiresAt", "endsAt"):
            d = _dt(src.get(k))
            if d is not None:
                dates.append(d)
    end = min(dates) if dates else None
    tags: set[str] = set()
    for src in (row, ev):
        for k in ("category", "categories", "tags"):
            v = src.get(k)
            for t in (v if isinstance(v, list) else [v]):
                if t:
                    tags.add(str(t.get("label") if isinstance(t, dict) else t).lower())
    mt = str(row.get("marketType") or "").lower()
    if mt:
        tags.add(f"type:{mt}")
    text = f"{label} {ev_title} {row.get('eventSlug', '')}"
    # NOTE: sportsMarketType / gameStartTime are populated on non-sports markets too (e.g. "election"),
    # so they are NOT used as sports signals.
    sport_types = {"moneyline", "spread", "total", "totals", "over_under", "props", "player_prop", "parlay"}
    if (row.get("team") or ev.get("teams") or ev.get("gameId") or row.get("gameId")
            or mt in sport_types or VS_RE.search(text)
            or any(t in SPORT_WORDS or "sport" in t for t in tags)):
        tags.add("sports")
    status = str(row.get("status") or row.get("state") or "").lower()
    closed_like = any(w in status for w in ("closed", "resolved", "settled", "halted", "suspended", "expired", "terminated"))
    fee_coef = row.get("feeCoefficient")
    fee_rate = D(fee_coef) if fee_coef is not None else US_TAKER_RATE
    # The published rebate is exchange-wide (0.0125 against a 0.06 taker rate); scale it if a market's
    # coefficient differs so a zero-fee market does not pretend to pay a rebate.
    rebate = (US_MAKER_REBATE * fee_rate / US_TAKER_RATE) if US_TAKER_RATE > ZERO else ZERO
    tick = D(row.get("orderPriceMinTickSize") or US_TICK)
    long_id, short_id = token_ids(slug)
    return Market(
        condition_id=slug,
        question=question,
        slug=slug,
        tokens=[Token(long_id, outcome), Token(short_id, f"NOT {outcome}")],
        neg_risk=False,
        event_id=str(row.get("eventSlug") or ev.get("slug") or ""),
        event_slug=str(row.get("eventSlug") or ev.get("slug") or ""),
        tick_size=tick if ZERO < tick < ONE else US_TICK,
        min_order_size=D(row.get("minimumTradeQty") or 1),
        fee_rate=fee_rate,
        fee_exponent=ONE,
        fee_known=True,
        maker_rebate_rate=rebate,
        end_date=end,
        liquidity_usd=D(row.get("liquidity") or row.get("liquidityNum") or 0),
        volume_24h_usd=D(row.get("volume24h") or row.get("volume24hr") or row.get("volume") or 0),
        tags=sorted(tags),
        accepting_orders=bool(row.get("active", True)) and not bool(row.get("closed", False))
        and not bool(row.get("hidden", False)) and not bool(row.get("archived", False)) and not closed_like,
    )


def books_from_us(slug: str, data: dict[str, Any], ts: Optional[float] = None) -> tuple[Book, Book]:
    """Long book from the venue payload, plus the mirrored short book."""
    ts = ts or time.time()
    if isinstance(data.get("marketData"), dict):      # REST wraps the payload; the WebSocket does not
        data = data["marketData"]
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


def format_price(price: Decimal, tick: Decimal = US_TICK) -> str:
    """Price string at the market's tick precision (ticks are 0.01 or 0.001 on this venue)."""
    exp = tick.normalize().as_tuple().exponent
    decimals = max(2, -exp) if isinstance(exp, int) else 2
    return f"{price.quantize(Decimal(1).scaleb(-decimals)):.{decimals}f}"


def order_params(intent: Intent, tick: Decimal = US_TICK) -> dict[str, Any]:
    slug, short = split_token(intent.token_id)
    params: dict[str, Any] = {
        "marketSlug": slug,
        "intent": INTENT[(short, intent.side)],
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": format_price(intent.price, tick), "currency": "USD"},
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
        m = self._market_for(intent.token_id)
        params = order_params(intent, m.tick_size if m else US_TICK)
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

    async def all_markets(self, limit_events: int = 300) -> list[tuple[Market, Optional[Decimal], Optional[Decimal]]]:
        """(market, hinted best bid, hinted best ask) in event order, which is the venue's volume order."""
        out: list[tuple[Market, Optional[Decimal], Optional[Decimal]]] = []
        for ev in await self.list_events_raw(limit_events):
            for row in ev.get("markets") or []:
                m = market_from_us(row, ev)
                if m and m.accepting_orders:
                    bid, ask = quote_hints(row)
                    out.append((m, bid, ask))
        return out

    async def select_universe(self) -> list[Market]:
        """The venue lists no volume/liquidity per market, so:
        1. filter on tags / end date and on the listing's quoted spread and price band,
        2. fetch real books for the first max_book_candidates survivors (event order ≈ volume order),
        3. rank by dollar depth at the touch and keep max_markets.
        """
        c = self.cfg.universe
        u = self.cfg.venue_us
        rows = await self.all_markets(limit_events=max(200, c.max_markets * 5))
        pre: list[Market] = []
        for m, bid, ask in rows:
            if not passes_filters(m, c, require_end_date=False, require_liquidity=False):
                continue
            if bid is not None and ask is not None:
                mid, spread = (bid + ask) / 2, ask - bid
                if spread > u.prefilter_max_spread or not (u.prefilter_band[0] <= mid <= u.prefilter_band[1]):
                    continue
            pre.append(m)
            if len(pre) >= u.max_book_candidates:
                break

        books = await self.seed_books([m.yes.token_id for m in pre])
        ranked: list[tuple[Decimal, Market]] = []
        for m in pre:
            b = books.get(m.yes.token_id)
            if not b or b.best_bid is None or b.best_ask is None:
                continue
            depth = b.bid_size_at_touch * b.best_bid + b.ask_size_at_touch * (ONE - b.best_ask)
            m.liquidity_usd = depth
            if c.min_liquidity_usd > 0 and depth < c.min_liquidity_usd:
                continue
            ranked.append((depth, m))
        ranked.sort(key=lambda x: x[0], reverse=True)
        selected = [m for _, m in ranked[: c.max_markets]]
        log.info("us_universe_selected", listed=len(rows), prefiltered=len(pre), with_books=len(ranked),
                 markets=len(selected), with_end_date=sum(1 for m in selected if m.end_date))
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
