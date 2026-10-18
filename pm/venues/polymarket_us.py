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

Fees: taker 0.06 x C x p(1-p). Maker rebate: 0.0125 x C x p(1-p) from 2026-07-01 until
2026-10-19 17:00 UTC; from then it is tiered by prior-month maker volume (under $10M: 0,
$10M+: 0.0035, $50M+: 0.0070, $200M+: 0.0104). The coefficient is config
(venue_us.maker_rebate_coef) and defaults to 0, the tier any account below $10M/month is in.

Market data: authenticated WebSocket for up to ws_max_markets slugs (the venue
limits streaming), REST book polling for the rest. Without API keys only REST
polling is available.

Discovery: since 2026-10-15 GET /v1/events returns only each event's primary market,
so non-sports events are re-fetched individually (GET /v1/events/slug/{slug}) to get
every market.
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
US_MAKER_REBATE_FLAT_2026_07 = Decimal("0.0125")   # the pre-2026-10-19 flat rebate, kept for reference/tests
US_TICK = Decimal("0.01")
MIN_POLL_WAIT_S = 0.5      # the REST poll loop never spins faster than this
STREAM_SILENT_S = 120.0    # a streamed market silent this long is REST-polled as well


def make_client(cfg: Config, key_id: Optional[str], secret_key: Optional[str], timeout: Optional[float] = None):
    """AsyncPolymarketUS with our timeout/retry settings (SDK >= 2.x); falls back for older SDKs.

    The trading client gets the longer order_timeout_seconds: order creation is never retried, so a short
    timeout turns a slow-but-accepted order into an apparent failure."""
    from polymarket_us import AsyncPolymarketUS
    u = cfg.venue_us
    kwargs = dict(key_id=key_id, secret_key=secret_key, gateway_base_url=u.gateway_url, api_base_url=u.api_url,
                  timeout=float(timeout if timeout is not None else u.timeout_seconds))
    try:
        return AsyncPolymarketUS(**kwargs, max_retries=int(u.max_retries))
    except TypeError:   # polymarket-us < 2.0 has no max_retries
        return AsyncPolymarketUS(**kwargs)


class Throttle:
    """Spaces REST requests so the whole process stays under the venue's per-IP limit (about 20 a second, shared by
    every endpoint). Over it the venue answers 429 and the request is lost or retried late."""

    def __init__(self, per_second: float):
        self._gap = 1.0 / per_second if per_second > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._gap <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self._gap
        if at > now:
            await asyncio.sleep(at - now)


def long_short_labels(row: dict[str, Any], outcomes: Any) -> tuple[str, str]:
    """Labels for the long and short side of the contract.

    `marketSides` says which side is long. The order of `outcomes` is not stable across responses
    (the same market was seen with "Yes" and with "No" first), so it is only a fallback.
    """
    sides = row.get("marketSides")
    if isinstance(sides, list):
        long_s = next((s for s in sides if isinstance(s, dict) and s.get("long") is True), None)
        short_s = next((s for s in sides if isinstance(s, dict) and s.get("long") is False), None)
        if long_s and long_s.get("description"):
            ll = str(long_s["description"])
            sl = str(short_s["description"]) if short_s and short_s.get("description") else f"NOT {ll}"
            return ll, sl
    outcome = row.get("outcome") or (str(outcomes[0]) if isinstance(outcomes, list) and outcomes else "Yes")
    return str(outcome), f"NOT {outcome}"


def event_is_sports(ev: dict[str, Any]) -> bool:
    tags: set[str] = set()
    for k in ("category", "categories", "tags"):
        v = ev.get(k)
        for t in (v if isinstance(v, list) else [v]):
            if t:
                tags.add(str(t.get("label") if isinstance(t, dict) else t).lower())
    return bool(ev.get("teams") or ev.get("gameId") or VS_RE.search(str(ev.get("title") or ""))
                or any(t in SPORT_WORDS or "sport" in t for t in tags))

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


def market_from_us(row: dict[str, Any], event: Optional[dict[str, Any]] = None,
                   maker_rebate_coef: Decimal = ZERO) -> Optional[Market]:
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
    long_label, short_label = long_short_labels(row, outcomes)
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
    # The rebate coefficient is quoted against the standard 0.06 taker rate; scale it if a market's
    # coefficient differs so a zero-fee market does not pretend to pay a rebate.
    rebate = (D(maker_rebate_coef) * fee_rate / US_TAKER_RATE) if US_TAKER_RATE > ZERO else ZERO
    tick = D(row.get("orderPriceMinTickSize") or US_TICK)
    long_id, short_id = token_ids(slug)
    return Market(
        condition_id=slug,
        question=question,
        slug=slug,
        tokens=[Token(long_id, long_label), Token(short_id, short_label)],
        neg_risk=False,
        synthetic_no=True,
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
    if not isinstance(data, dict):
        data = {}
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
    long_book.traded_total = short_book.traded_total = traded_total_from_us(data)   # string or {"value": ...}
    return long_book, short_book


def traded_total_from_us(data: Any) -> Optional[Decimal]:
    """The venue's traded-volume counter from a bbo, book or stream payload (`sharesTraded`, top level or in
    `stats`), or None. The REST book reply does not carry it; GET /v1/markets/{slug}/bbo does. An empty value is
    None, never 0: a 0 would read as the counter restarting."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("marketData"), dict):
        data = data["marketData"]
    v = data.get("sharesTraded")
    if v in (None, "") and isinstance(data.get("stats"), dict):
        v = data["stats"].get("sharesTraded")
    if isinstance(v, dict):
        v = v.get("value")
    if v is None or isinstance(v, bool) or (isinstance(v, str) and not v.strip()):
        return None
    try:
        out = Decimal(str(v).strip())
    except Exception:
        return None
    return out if out.is_finite() and out >= 0 else None


def trades_from_us(slug: str, payload: dict[str, Any]) -> list[Trade]:
    """A long trade at p is also a short trade at 1-p with the opposite aggressor side.

    The taker's intent decides: buying long or selling short lifts the long ask (BUY), selling long or buying
    short hits the long bid (SELL). The side field is used only when there is no intent, because it may be given
    in the short contract's own terms (buying NO reported as a BUY)."""
    price = _amount(payload.get("price"))
    qty = _amount(payload.get("quantity") or payload.get("qty") or 0)
    taker = payload.get("taker") or {}
    side_s = str(taker.get("side") or "").upper()
    intent = str(taker.get("intent") or "").upper()
    if intent.endswith(("BUY_LONG", "SELL_SHORT")):
        side = Side.BUY
    elif intent.endswith(("SELL_LONG", "BUY_SHORT")):
        side = Side.SELL
    else:
        side = Side.BUY if "BUY" in side_s else Side.SELL
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
        net = D(p.get("netPositionDecimal") or p.get("netPosition") or 0)   # *Decimal fields are exact
        cost = abs(_amount(p.get("cost") or 0))
        long_id, short_id = token_ids(str(slug))
        if net > ZERO:
            out[long_id] = (net, cost)
        elif net < ZERO:
            out[short_id] = (-net, cost)
    return out


# ------------------------------------------------------------------ market data

class USMarketData:
    """Books for the universe: the first ws_max_markets markets on the authenticated stream (with trade prints),
    the rest from REST every book_poll_seconds, and the traded-volume counter of every market from REST bbo every
    volume_poll_seconds. Every request goes through one Throttle. Nothing a payload or a handler raises may stop
    a loop: a recorder runs unattended for a day."""

    def __init__(self, cfg: Config, public_client, on_book: BookHandler, on_trade: TradeHandler,
                 throttle: Optional[Throttle] = None):
        self.cfg = cfg
        self.client = public_client
        self._on_book = on_book
        self._on_trade = on_trade
        self._throttle = throttle or Throttle(0)
        self._slugs: list[str] = []
        self._ws_slugs: list[str] = []
        self._tasks: list[asyncio.Task] = []
        self._poll_task: Optional[asyncio.Task] = None
        self._volume_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._ws = None
        self.connected = False
        self.messages = 0
        self.last_message_ts = 0.0
        self._last_traded: dict[str, tuple[Decimal, float]] = {}   # slug -> (counter, wall time it was read)
        self._last_stream_ts: dict[str, float] = {}
        self._prev_volume: dict[str, Decimal] = {}
        self._volume_rounds = 0
        self._warned_still = False
        self._ever_moved = False
        self._last_warn: dict[str, float] = {}

    # ---- helpers
    def _carry_max_s(self) -> float:
        """How long a counter value may ride on later books. Older than this (a gap: the market left the universe,
        the network dropped, the computer slept) it is not carried, or the volume traded during the gap would be
        counted as traded in the first minute after it."""
        return max(150.0, 2.5 * float(self.cfg.venue_us.volume_poll_seconds or 0))

    def _warn(self, key: str, every_s: float, event: str, **kw: Any) -> None:
        now = time.time()
        if now - self._last_warn.get(key, 0.0) >= every_s:
            self._last_warn[key] = now
            log.warning(event, **kw)

    def stream_fresh(self, slug: str, now: Optional[float] = None) -> bool:
        """A streamed market counts as streamed while the stream delivered for it in the last STREAM_SILENT_S; a
        socket that is open but silent for it (a rejected subscription) leaves it to the REST poll."""
        now = time.time() if now is None else now
        return self.connected and slug in self._ws_slugs and now - self._last_stream_ts.get(slug, 0.0) <= STREAM_SILENT_S

    def _emit_books(self, slug: str, payload: dict[str, Any]) -> None:
        """Books from one payload. A payload without the volume counter carries the last value forward while it is
        recent (_carry_max_s), so short holes do not hide traded volume from `pm measure` and gaps do not invent it."""
        for b in books_from_us(slug, payload):
            if b.traded_total is None:
                last = self._last_traded.get(slug)
                if last is not None and b.ts - last[1] <= self._carry_max_s():
                    b.traded_total = last[0]
            elif b.token_id.endswith(LONG):
                self._last_traded[slug] = (b.traded_total, b.ts)
            self._on_book(b)
        self.last_message_ts = time.time()

    def _pick_ws(self, slugs: list[str]) -> list[str]:
        """The markets to stream: the ones already streaming that are still wanted (every re-subscription loses
        trade prints), then the first of `slugs` (the universe's own order)."""
        s = self.cfg.secrets
        n = self.cfg.venue_us.ws_max_markets
        if not (s.us_key_id and s.us_secret_key and n > 0):
            return []
        wanted = set(slugs)
        keep = [x for x in self._ws_slugs if x in wanted]
        for x in slugs:
            if len(keep) >= n:
                break
            if x not in keep:
                keep.append(x)
        return keep[:n]

    # ---- lifecycle
    def start(self, slugs: list[str]) -> None:
        self._slugs = list(dict.fromkeys(slugs))
        self._ws_slugs = self._pick_ws(self._slugs)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="us-book-poll")
        self._volume_task = (asyncio.create_task(self._volume_loop(), name="us-volume-poll")
                             if self.cfg.venue_us.volume_poll_seconds > 0 else None)
        self._ws_task = asyncio.create_task(self._ws_loop(), name="us-ws") if self._ws_slugs else None
        self._refresh_tasks()

    def _refresh_tasks(self) -> None:
        self._tasks = [t for t in (self._poll_task, self._volume_task, self._ws_task) if t is not None]

    def _revive(self) -> None:
        """Belt and braces: a loop that ended anyway is logged and started again."""
        for attr, factory, name in (("_poll_task", self._poll_loop, "us-book-poll"),
                                    ("_volume_task", self._volume_loop, "us-volume-poll")):
            t = getattr(self, attr)
            if t is not None and t.done() and not self._stop.is_set():
                err = None if t.cancelled() else t.exception()
                log.error("us_feed_loop_restarted", loop=name, error=str(err)[:200])
                setattr(self, attr, asyncio.create_task(factory(), name=name))
        self._refresh_tasks()

    async def set_slugs(self, slugs: list[str]) -> None:
        """A new universe. The poll loops pick up the new list on their next round; the stream is re-subscribed
        only if the set of streamed markets changed."""
        slugs = list(dict.fromkeys(slugs))
        if self._poll_task is None:
            self.start(slugs)
            return
        self._revive()
        if slugs == self._slugs:
            return
        wanted = set(slugs)
        for d in (self._last_traded, self._last_stream_ts, self._prev_volume):
            for gone in [k for k in d if k not in wanted]:
                del d[gone]
        ws = self._pick_ws(slugs)
        self._slugs = slugs
        if set(ws) == set(self._ws_slugs):
            return
        await self._stop_ws()
        self._ws_slugs = ws
        if ws:
            self._ws_task = asyncio.create_task(self._ws_loop(), name="us-ws")
        self._refresh_tasks()

    async def _stop_ws(self) -> None:
        t, self._ws_task = self._ws_task, None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.connected = False

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
        self._poll_task = self._volume_task = self._ws_task = None
        self.connected = False

    async def _sleep_rest(self, t0: float, interval: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(MIN_POLL_WAIT_S, interval - (time.time() - t0)))
        except asyncio.TimeoutError:
            pass

    # ---- REST books
    async def _poll_loop(self) -> None:
        sem = asyncio.Semaphore(4)
        last_err: list[str] = [""]

        async def book(slug: str) -> bool:
            async with sem:
                await self._throttle.wait()
                try:
                    asked = time.time()
                    data = await self.client.markets.book(slug)
                    self.messages += 1
                    if self._last_stream_ts.get(slug, 0.0) < asked:   # a stream book overtook it: it is older
                        self._emit_books(slug, data or {})
                    return True
                except Exception as e:
                    last_err[0] = f"{type(e).__name__}: {e}"[:160]
                    log.debug("us_book_poll_failed", slug=slug, error=last_err[0])
                    return False

        while not self._stop.is_set():
            t0 = time.time()
            try:
                # streamed markets come from the socket while it delivers for them; otherwise they are polled too
                targets = [s for s in self._slugs if not self.stream_fresh(s, t0)]
                got = await asyncio.gather(*(book(s) for s in targets), return_exceptions=True)
                failed = sum(1 for g in got if g is not True)
                if targets and failed > max(2, len(targets) // 10):
                    self._warn("books", 600, "us_book_polls_failing", failed=failed, of=len(targets),
                               error=last_err[0], hint="rate limited? is another pm process running?")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._warn("book_loop", 60, "us_book_poll_round_failed", error=str(e)[:200])
            await self._sleep_rest(t0, self.cfg.venue_us.book_poll_seconds)

    # ---- REST volume counter
    def _note_volume(self, slug: str, payload: dict[str, Any]) -> Optional[Decimal]:
        """The counter from a bbo reply. It reaches the tape with the market's next book (see _emit_books)."""
        if slug not in self._slugs:
            return None                                   # it left the universe while the reply was in flight
        v = traded_total_from_us(payload or {})
        if v is not None:
            self._last_traded[slug] = (v, time.time())
        return v

    async def _volume_loop(self) -> None:
        """Its own loop, so a slow counter endpoint never stretches the book cadence."""
        sem = asyncio.Semaphore(2)
        last_err: list[str] = [""]
        sample: list[str] = [""]

        async def volume(slug: str) -> tuple[str, Optional[Decimal], bool]:
            async with sem:
                await self._throttle.wait()
                try:
                    data = await self.client.markets.bbo(slug)
                    if not sample[0]:
                        sample[0] = str(data)[:240]
                    return slug, self._note_volume(slug, data or {}), True
                except Exception as e:
                    last_err[0] = f"{type(e).__name__}: {e}"[:160]
                    log.debug("us_volume_poll_failed", slug=slug, error=last_err[0])
                    return slug, None, False

        while not self._stop.is_set():
            t0 = time.time()
            try:
                slugs = list(self._slugs)
                got = [g for g in await asyncio.gather(*(volume(s) for s in slugs), return_exceptions=True)
                       if isinstance(g, tuple)]
                self._volume_rounds += 1
                n = self._volume_rounds
                found = [(s, v) for s, v, _ in got if v is not None]
                failed = sum(1 for _, _, ok in got if not ok)
                moved = sum(1 for s, v in found if s in self._prev_volume and v != self._prev_volume[s])
                self._ever_moved = self._ever_moved or moved > 0
                self._prev_volume.update(found)
                if n <= 3 or n % 60 == 0:                 # the first rounds, then hourly
                    log.info("us_volume_counter", markets=len(slugs), with_counter=len(found), moved=moved,
                             failed=failed, round=n, **({"sample": sample[0]} if n == 1 else {}))
                if slugs and len(found) < len(slugs) / 2 and (n <= 3 or n % 10 == 0):
                    log.warning("us_volume_counter_missing",
                                note=f"the venue's volume counter is missing on {len(slugs) - len(found)} of "
                                     f"{len(slugs)} markets ({failed} requests failed); measure will have no "
                                     f"trading flow for them", error=last_err[0], sample=sample[0])
                if n == 10 and found and not any(v for v in self._prev_volume.values()) and not self._warned_still:
                    self._warned_still = True
                    log.warning("us_volume_counter_zero", note="the volume counter is 0 on every market")
                if n >= 10 and found and not self._ever_moved and not self._warned_still:
                    self._warned_still = True
                    log.warning("us_volume_counter_not_moving",
                                note=f"no market's volume counter has changed since the start ({n} rounds, "
                                     f"{len(found)} markets)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._warn("volume_loop", 60, "us_volume_round_failed", error=str(e)[:200])
            await self._sleep_rest(t0, self.cfg.venue_us.volume_poll_seconds)

    # ---- stream
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
                if self._ws is ws:
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
        try:
            payload = msg.get("marketData") or {}
            slug = payload.get("marketSlug")
            if not slug:
                return
            self.messages += 1
            self._last_stream_ts[slug] = time.time()
            self._emit_books(slug, payload)
        except Exception as e:                            # never let one message end the SDK's reader
            self._warn("ws_book", 60, "us_ws_book_failed", error=f"{type(e).__name__}: {e}"[:200])

    def _on_trade_msg(self, msg: dict[str, Any]) -> None:
        try:
            payload = msg.get("trade") or {}
            slug = payload.get("marketSlug")
            if not slug:
                return
            self.messages += 1
            self.last_message_ts = time.time()      # only book messages prove the book stream delivers
            for t in trades_from_us(slug, payload):
                self._on_trade(t)
        except Exception as e:
            self._warn("ws_trade", 60, "us_ws_trade_failed", error=f"{type(e).__name__}: {e}"[:200])


def _slugs_in_order(token_ids_: list[str]) -> list[str]:
    """Market slugs in the universe's order (the first ones are streamed), each once."""
    return list(dict.fromkeys(split_token(t)[0] for t in token_ids_))


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
        self._client = make_client(self.cfg, s.us_key_id, s.us_secret_key,
                                   timeout=self.cfg.venue_us.order_timeout_seconds)
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
            # Order creation is never retried by the SDK, and a timeout does not mean the venue rejected it.
            # Look for it among open orders before reporting failure, so it is tracked, not orphaned.
            adopted = await self._adopt(intent, params)
            if adopted is not None:
                log.warning("us_order_adopted_after_error", tag=intent.tag, order_id=adopted.order_id, error=str(e)[:120])
                return adopted
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
        # Poll even with nothing tracked (at the slow cadence) so orphaned venue orders are swept.
        fast = self._orders and not self.private_connected
        interval = self.cfg.execution.fill_poll_seconds if fast else self.cfg.execution.reconcile_seconds
        if now - self._last_poll >= interval:
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
    async def _adopt(self, intent: Intent, params: dict[str, Any]) -> Optional[Order]:
        """Find an untracked open order matching what we just tried to create."""
        slug = params["marketSlug"]
        try:
            resp = await self._client.orders.list({"slugs": [slug]})
        except Exception as e:
            log.warning("us_adopt_lookup_failed", slug=slug, error=str(e)[:120])
            return None
        want_px, want_qty = D(params["price"]["value"]), D(params["quantity"])
        for r in (resp or {}).get("orders") or []:
            oid = str(r.get("id") or "")
            if not oid or oid in self._orders:
                continue
            if (r.get("marketSlug") in (None, slug) and r.get("intent") == params["intent"]
                    and _amount(r.get("price")) == want_px and D(r.get("quantity") or 0) == want_qty):
                order = Order(order_id=oid, tag=intent.tag, strategy=intent.strategy, token_id=intent.token_id,
                              side=intent.side, price=intent.price, size=intent.size, tif=intent.tif,
                              post_only=intent.post_only)
                self._orders[oid] = order
                self._apply_order_row(order, r)
                return order
        return None

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
            # Orphans: resting on the venue but unknown to us. Risk limits cannot see them, so cancel.
            # The bot assumes it is the only trader on this account (it cancels everything on start/stop).
            for oid, r in live.items():
                if oid and oid not in self._orders:
                    try:
                        await self._client.orders.cancel(oid, {"marketSlug": r.get("marketSlug") or ""})
                        log.warning("us_orphan_order_cancelled", order_id=oid, slug=r.get("marketSlug"))
                    except Exception as e:
                        log.error("us_orphan_cancel_failed", order_id=oid, error=str(e)[:200])

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
        try:
            import polymarket_us  # noqa: F401
        except ImportError as e:
            raise SystemExit(
                "The Polymarket US SDK is not installed for THIS Python interpreter.\n"
                "In this same terminal run:\n\n    python -m pip install -r requirements.txt\n\n"
                "(use `python -m pip`, not bare `pip`, so it installs into the interpreter that runs the bot)"
            ) from e
        s = cfg.secrets
        self.public = make_client(cfg, s.us_key_id or None, s.us_secret_key or None)
        self.data: Optional[USMarketData] = None
        self._exchange: Optional[USExchange] = None

    @property
    def throttle(self) -> Throttle:
        t = self.__dict__.get("_throttle")
        if t is None:
            t = self.__dict__["_throttle"] = Throttle(self.cfg.venue_us.max_requests_per_second)
        return t

    async def check_access(self) -> tuple[bool, str]:
        s = self.cfg.secrets
        if not (s.us_key_id and s.us_secret_key):
            return True, "no US API key configured (read-only access)"
        try:
            await self.throttle.wait()
            bal = await self.public.account.balances()
        except Exception as e:
            return False, f"US API auth failed: {str(e)[:120]}"
        rows = (bal or {}).get("balances") or []
        bp = rows[0].get("buyingPower") if rows else None
        return True, f"US API auth ok buying_power={bp}"

    async def list_events_raw(self, limit: int = 300, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """Active events by volume. Events carry endTime, tags and their markets.

        Pages advance by what came back and stop on an empty page or one with nothing new, so a venue page size
        below 100 does not cut the listing short and an ignored offset does not loop."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset, page = 0, 100
        while len(out) < limit:
            await self.throttle.wait()
            resp = await self.public.events.list({"limit": page, "offset": offset, "active": True, "closed": False,
                                                  "orderBy": ["volume"], "orderDirection": "desc", **(params or {})})
            rows = [r for r in (resp or {}).get("events") or [] if isinstance(r, dict)]
            fresh = [r for r in rows if str(r.get("slug") or id(r)) not in seen]
            if not fresh:
                break
            seen.update(str(r.get("slug") or id(r)) for r in fresh)
            out.extend(fresh)
            offset += len(rows)
        return out[:limit]

    async def expand_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace each non-sports event with its full version (all markets), in the original order.

        The list endpoint returns only the primary market per event since 2026-10-15. Sports events are
        excluded later anyway, so they are not re-fetched. Failures keep the listed version.
        """
        u = self.cfg.venue_us
        cap = int(u.max_event_details)
        sports_excluded = "sports" in {t.lower() for t in self.cfg.universe.exclude_tags}
        targets = [i for i, ev in enumerate(events)
                   if ev.get("slug") and not (sports_excluded and event_is_sports(ev))]
        skipped = targets[cap:]
        targets = targets[:cap]
        out = list(events)
        sem = asyncio.Semaphore(max(1, int(u.expand_concurrency)))
        stats = {"fetched": 0, "failed": 0}

        async def one(i: int) -> None:
            async with sem:
                try:
                    await self.throttle.wait()
                    resp = await self.public.events.retrieve_by_slug(events[i]["slug"])
                    full = resp.get("event") if isinstance(resp, dict) and isinstance(resp.get("event"), dict) else resp
                    if not (isinstance(full, dict) and isinstance(full.get("markets"), list)):
                        raise ValueError(f"unexpected event payload type {type(resp).__name__}")
                except Exception as e:
                    stats["failed"] += 1
                    log.debug("us_event_detail_failed", slug=events[i].get("slug"), error=str(e)[:120])
                    return
            out[i] = full
            stats["fetched"] += 1

        # Bounded: the engine waits for the universe refresh, so a slow venue must not stall quoting.
        # Events not fetched in time keep their listed (primary-market-only) version.
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.gather(*(one(i) for i in targets)), timeout=float(u.expand_timeout_seconds))
        except asyncio.TimeoutError:
            timed_out = True
        log.info("us_events_expanded", listed=len(events), fetched=stats["fetched"], failed=stats["failed"],
                 timed_out=timed_out, not_fetched_over_cap=len(skipped),
                 sports_skipped=sum(1 for ev in events if sports_excluded and event_is_sports(ev)),
                 markets_before=sum(len(e.get("markets") or []) for e in events),
                 markets_after=sum(len(e.get("markets") or []) for e in out))
        return out

    async def all_markets(self, limit_events: int = 300) -> list[tuple[Market, Optional[Decimal], Optional[Decimal]]]:
        """(market, hinted best bid, hinted best ask) in event order, which is the venue's volume order."""
        return self._markets_from_events(await self.expand_events(await self.list_events_raw(limit_events)))

    def _markets_from_events(self, events: list[dict[str, Any]]) -> list[tuple[Market, Optional[Decimal], Optional[Decimal]]]:
        out: list[tuple[Market, Optional[Decimal], Optional[Decimal]]] = []
        seen: set[str] = set()
        coef = self.cfg.venue_us.maker_rebate_coef
        for ev in events:
            for row in ev.get("markets") or []:
                m = market_from_us(row, ev, maker_rebate_coef=coef)
                if m and m.accepting_orders and m.condition_id not in seen:
                    seen.add(m.condition_id)
                    bid, ask = quote_hints(row)
                    out.append((m, bid, ask))
        return out

    async def select_universe(self) -> list[Market]:
        if self.cfg.recording:
            return await self._select_for_recording()
        return await self._select_for_trading()

    async def _record_events(self) -> list[dict[str, Any]]:
        """The busiest events, interleaved with the busiest events starting soon (games). Ranked by volume alone,
        long-running politics and season futures would crowd out the games being played today."""
        r = self.cfg.record
        busiest = await self.list_events_raw(r.max_events)
        soon: list[dict[str, Any]] = []
        if r.soon_days > 0:
            now = datetime.now(timezone.utc)
            lo, hi = now.timestamp() - 6 * 3600, now.timestamp() + r.soon_days * 86400
            iso = "%Y-%m-%dT%H:%M:%SZ"
            try:
                listed = await self.list_events_raw(r.max_events, {
                    "startTimeMin": datetime.fromtimestamp(lo, timezone.utc).strftime(iso),
                    "startTimeMax": datetime.fromtimestamp(hi, timezone.utc).strftime(iso), "ended": False})
            except Exception as e:
                log.warning("us_soon_events_failed", error=str(e)[:200])
                listed = []
            # checked here too, in case the venue ignores the filter; trusted only if no row says when it starts
            starts = [_dt(ev.get("startTime") or ev.get("startDate")) for ev in listed]
            trust = all(st is None for st in starts)
            for ev, st in zip(listed, starts):
                if (st is None and trust) or (st is not None and lo <= st.timestamp() <= hi):
                    soon.append(ev)
            if listed and not soon:
                log.warning("us_soon_events_none_kept", listed=len(listed),
                            note="no event starting soon was found; games may be missing from the recording")
        out: list[dict[str, Any]] = []
        have: set[str] = set()
        for k in range(max(len(busiest), len(soon))):
            for ev in (soon[k:k + 1] + busiest[k:k + 1]):
                key = str(ev.get("slug") or id(ev))
                if key not in have:
                    have.add(key)
                    out.append(ev)
        log.info("us_record_events", busiest=len(busiest), starting_soon=len(soon), events=len(out))
        return out

    async def _select_for_recording(self) -> list[Market]:
        """`pm record`: the busiest markets, sports included. Events come in the venue's volume order, interleaved
        with the busiest events starting within record.soon_days; their markets are kept in that order, at most
        `record.max_markets_per_event` each so one game's many markets cannot fill the list. Nothing is filtered
        on end date, spread or depth: those are what `measure` is for. Markets must quote both sides (books are
        fetched in chunks until max_markets are found) and have a listed price inside venue_us.prefilter_band.
        The first market of each event comes first, so the streamed markets (the only trade prints) come from
        as many different events as there are stream slots."""
        r = self.cfg.record
        band = self.cfg.venue_us.prefilter_band
        excluded = {t.lower() for t in self.cfg.universe.exclude_tags}
        rows = self._markets_from_events(await self.expand_events(await self._record_events()))
        cands: list[Market] = []
        for m, bid, ask in rows:
            if not m.is_binary or any(t in excluded for t in m.tags):
                continue
            if bid is not None and ask is not None and not (band[0] <= (bid + ask) / 2 <= band[1]):
                continue
            cands.append(m)

        def event_of(m: Market) -> str:
            return m.event_slug or m.condition_id

        cap = max(1, int(r.max_markets_per_event))
        order = {m.condition_id: i for i, m in enumerate(cands)}
        per_event: dict[str, int] = {}
        selected: list[Market] = []
        fetched = 0
        remaining = cands
        while remaining and len(selected) < r.max_markets:
            # Books in chunks of 50, at most what each event can still take; a market that did not fit this chunk
            # waits for the next, in case a market of its event turns out one-sided.
            chunk: list[Market] = []
            later: list[Market] = []
            in_chunk: dict[str, int] = {}
            for m in remaining:
                e = event_of(m)
                if per_event.get(e, 0) >= cap:
                    continue
                if len(chunk) < 50 and per_event.get(e, 0) + in_chunk.get(e, 0) < cap:
                    in_chunk[e] = in_chunk.get(e, 0) + 1
                    chunk.append(m)
                else:
                    later.append(m)
            if not chunk:
                break
            remaining = later
            books = await self.seed_books([m.yes.token_id for m in chunk])
            fetched += len(chunk)
            for m in chunk:
                b = books.get(m.yes.token_id)
                two_sided = b is not None and b.best_bid is not None and b.best_ask is not None and b.best_ask > b.best_bid
                if len(selected) >= r.max_markets or not two_sided or per_event.get(event_of(m), 0) >= cap:
                    continue
                per_event[event_of(m)] = per_event.get(event_of(m), 0) + 1
                m.liquidity_usd = b.bid_size_at_touch * b.best_bid + b.ask_size_at_touch * (ONE - b.best_ask)
                selected.append(m)
        selected.sort(key=lambda m: order[m.condition_id])           # venue order...
        leads = list({event_of(m): m for m in reversed(selected)}.values())[::-1]   # ...each event's first market
        lead_ids = {m.condition_id for m in leads}
        selected = leads + [m for m in selected if m.condition_id not in lead_ids]
        log.info("us_record_universe_selected", listed=len(rows), candidates=len(cands), books_fetched=fetched,
                 markets=len(selected), events=len(per_event), sports=sum(1 for m in selected if "sports" in m.tags))
        return selected

    async def _select_for_trading(self) -> list[Market]:
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
                    await self.throttle.wait()
                    data = await self.public.markets.book(slug)
                except Exception as e:
                    log.debug("us_seed_book_failed", slug=slug, error=str(e)[:120])
                    return
                for b in books_from_us(slug, data or {}):
                    out[b.token_id] = b

        await asyncio.gather(*(one(s) for s in slugs))
        return out

    def start_feed(self, token_ids_: list[str], on_book: BookHandler, on_trade: TradeHandler) -> None:
        self.data = USMarketData(self.cfg, self.public, on_book, on_trade, throttle=self.throttle)
        self.data.start(_slugs_in_order(token_ids_))

    async def set_feed_tokens(self, token_ids_: list[str]) -> None:
        if self.data is not None:
            await self.data.set_slugs(_slugs_in_order(token_ids_))

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

    def streaming_tokens(self) -> set[str]:
        """Books on the authenticated market stream are current while it is connected and delivering for them
        (the SDK's socket keeps itself alive with pings); REST-polled books are refreshed by each poll instead."""
        if not self.data or not self.data.connected:
            return set()
        now = time.time()
        return {t for slug in self.data._ws_slugs if self.data.stream_fresh(slug, now) for t in token_ids(slug)}

    @property
    def feed_connected(self) -> bool:
        """Connected = the stream is up, or market data arrived recently (not merely once since the start)."""
        if not self.data:
            return False
        recent = max(60.0, 3 * float(self.cfg.venue_us.book_poll_seconds))
        return bool(self.data.connected or time.time() - self.data.last_message_ts <= recent)

    @property
    def feed_messages(self) -> int:
        return self.data.messages if self.data else 0

    @property
    def private_feed_connected(self) -> Optional[bool]:
        return self._exchange.private_connected if self._exchange else None
