"""`pm record`: the measurement profile, its universe, the volume counter poll and the stream handling."""

import asyncio
import sqlite3
import time

import pytest

from pm.config import Config
from pm.models import Mode
from pm.venues.polymarket_us import (
    PolymarketUSVenue, Throttle, USMarketData, _slugs_in_order, books_from_us, traded_total_from_us,
)
from tests.conftest import D


def _book(bid="0.50", ask="0.52", qty="100"):
    return {"bids": [{"px": {"value": bid}, "qty": qty}] if bid else [],
            "offers": [{"px": {"value": ask}, "qty": qty}] if ask else []}


class _Fake:
    """The public client: events.list / retrieve_by_slug, markets.book / bbo, account.balances."""

    def __init__(self, events, full=None, books=None, volume=None):
        self._events, self._full = events, full or {}
        self._books, self._volume = books or {}, volume or {}
        self.events = self
        self.markets = self
        self.calls: list[tuple[str, str]] = []

    async def list(self, params):
        return {"events": self._events if params.get("offset", 0) == 0 else []}

    async def retrieve_by_slug(self, slug):
        self.calls.append(("event", slug))
        if slug not in self._full:
            raise RuntimeError("404")
        return {"event": self._full[slug]}

    async def book(self, slug):
        self.calls.append(("book", slug))
        return {"marketData": self._books.get(slug, _book())}

    async def bbo(self, slug):
        self.calls.append(("bbo", slug))
        v = self._volume.get(slug)
        if callable(v):
            v = v()
        return {"marketData": {"marketSlug": slug, "sharesTraded": v} if v is not None else {"marketSlug": slug}}

    async def close(self):
        return None


def _row(slug, title="X", **kw):
    return {"slug": slug, "title": title, "outcomes": "[\"Yes\",\"No\"]", "feeCoefficient": 0.06,
            "minimumTradeQty": 1, "orderPriceMinTickSize": 0.01, "active": True, **kw}


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    import pm.venues.polymarket_us
    monkeypatch.setattr(pm.venues.polymarket_us, "MIN_POLL_WAIT_S", 0.01)


def _venue(cfg, public):
    cfg.venue_us.max_requests_per_second = 0                   # the fake venue has no rate limit
    v = PolymarketUSVenue.__new__(PolymarketUSVenue)
    v.cfg, v.data, v._exchange, v.public = cfg, None, None, public
    return v


# ------------------------------------------------------------------ the profile

def test_shipped_config_records_sports_on_its_own_tape_and_trades_nothing():
    cfg = Config.load("config.yaml", env_path="/nonexistent/.env")
    cfg.venue = "polymarket_us"
    paper_tape = cfg.db_file
    cfg.as_recorder()
    assert cfg.recording and cfg.mode is Mode.READ_ONLY and cfg.strategies == {}
    assert cfg.db_file == "data/measure_polymarket_us.sqlite" != paper_tape
    assert "sports" not in [t.lower() for t in cfg.universe.exclude_tags]
    assert cfg.universe.max_markets == cfg.record.max_markets >= 100
    assert cfg.venue_us.book_poll_seconds == cfg.record.book_poll_seconds
    # the request budget stays under the venue's ~20/s per IP: books + volume counter for every market
    u, r = cfg.venue_us, cfg.record
    assert r.max_markets / r.book_poll_seconds + r.max_markets / u.volume_poll_seconds < u.max_requests_per_second
    assert u.max_requests_per_second <= 18


def test_record_command_refuses_the_international_venue(capsys):
    from pm.__main__ import main
    assert main(["--venue", "polymarket", "record"]) == 2
    assert "--venue us record" in capsys.readouterr().out


def test_engine_in_record_mode_writes_fewer_book_rows(tmp_path):
    from pm.engine import Engine
    cfg = Config()
    cfg.venue = "polymarket_us"
    cfg.as_recorder()
    cfg.db_path = str(tmp_path / "m_{venue}.sqlite")
    e = Engine(cfg)
    try:
        assert e.strategies == [] and e.store._heartbeat == cfg.record.book_heartbeat_seconds
        assert e.store._snap_interval == cfg.record.book_snapshot_seconds
    finally:
        e.store.close()


# ------------------------------------------------------------------ the universe

async def test_record_universe_takes_sports_in_venue_order_capped_per_event():
    cfg = Config()
    cfg.as_recorder()
    cfg.record.max_markets = 8
    cfg.record.max_markets_per_event = 3
    game = {"slug": "nyk-bos", "title": "Knicks vs Celtics", "category": "sports", "teams": [{"id": 1}],
            "endDate": "2020-01-01T00:00:00Z",                       # past end date: still trading, still kept
            "markets": [_row("ml")]}
    game_full = {**game, "markets": [_row(f"g{k}") for k in range(6)]}
    politics = {"slug": "ga-senate", "title": "Georgia Senate", "category": "politics",
                "markets": [_row("ga-a"), _row("ga-b"), _row("ga-sure", bestBidQuote={"value": "0.98"},
                                                                bestAskQuote={"value": "0.99"})]}
    other = {"slug": "fed", "title": "Fed", "category": "economics",
             "markets": [_row("fed-onesided"), _row("fed-dec"), _row("fed-jan")]}
    books = {"g1": _book(bid=None), "fed-onesided": _book(ask=None)}
    v = _venue(cfg, _Fake([game, politics, other], full={"nyk-bos": game_full}, books=books))
    out = [m.condition_id for m in await v.select_universe()]
    # venue (volume) order; 3 per event; one-sided books and near-certain listed prices left out
    assert out == ["g0", "g2", "g3", "ga-a", "ga-b", "fed-dec", "fed-jan"]
    assert ("event", "nyk-bos") in v.public.calls                       # sports events are expanded too
    sel = {m.condition_id: m for m in await v.select_universe()}
    assert "sports" in sel["g0"].tags and "sports" not in sel["ga-a"].tags


async def test_record_universe_stops_fetching_books_once_full_and_honours_exclude_tags():
    cfg = Config()
    cfg.record.max_markets = 3
    cfg.record.exclude_tags = ["crypto"]
    cfg.as_recorder()
    events = [{"slug": f"e{k}", "title": f"E{k}", "category": "crypto" if k == 0 else "politics",
               "markets": [_row(f"m{k}")]} for k in range(120)]
    v = _venue(cfg, _Fake(events))
    out = [m.condition_id for m in await v.select_universe()]
    assert out == ["m1", "m2", "m3"]
    assert sum(1 for kind, _ in v.public.calls if kind == "book") == 50     # one chunk, not all 119


async def test_trading_universe_is_unchanged_by_the_record_profile_existing():
    cfg = Config()                                                      # not recording: sports still excluded
    game = {"slug": "nyk-bos", "title": "Knicks vs Celtics", "category": "sports", "markets": [_row("ml")]}
    v = _venue(cfg, _Fake([game]))
    assert await v.select_universe() == []


# ------------------------------------------------------------------ the feed

def test_slugs_keep_the_universe_order():
    assert _slugs_in_order(["b|L", "b|S", "a|L", "a|S", "c|L"]) == ["b", "a", "c"]


def test_volume_counter_parsing():
    assert traded_total_from_us({"marketData": {"sharesTraded": "1234"}}) == D(1234)
    assert traded_total_from_us({"stats": {"sharesTraded": {"value": "5"}}}) == D(5)
    assert traded_total_from_us({"marketData": {"bestBid": {"value": "0.5"}}}) is None
    assert traded_total_from_us({"sharesTraded": "n/a"}) is None
    long_b, short_b = books_from_us("x", {"marketData": {**_book(), "stats": {"sharesTraded": "9"}}})
    assert long_b.traded_total == short_b.traded_total == D(9)


def _feed(cfg, client, keys=False):
    got = []
    if keys:
        cfg.secrets.us_key_id, cfg.secrets.us_secret_key = "k", "s"
    md = USMarketData(cfg, client, got.append, lambda t: None)
    return md, got


async def test_feed_polls_the_volume_counter_and_carries_it_onto_books():
    cfg = Config()
    cfg.venue_us.book_poll_seconds = 0.05
    cfg.venue_us.volume_poll_seconds = 0.05
    n = {"a": 100}

    def grow():
        n["a"] += 7
        return str(n["a"])

    md, got = _feed(cfg, _Fake([], volume={"a": grow, "b": None}))
    md.start(["a", "b"])
    await asyncio.sleep(0.4)
    await md.stop()
    a = [b.traded_total for b in got if b.token_id == "a|L"]
    assert a[-1] is not None and a[-1] > D(100) and a == sorted(a, key=lambda x: x or 0)
    assert all(b.traded_total is None for b in got if b.token_id == "b|L")   # no counter: nothing invented


async def test_streamed_markets_are_polled_only_while_the_stream_is_down(monkeypatch):
    cfg = Config()
    cfg.venue_us.book_poll_seconds = 0.05
    cfg.venue_us.volume_poll_seconds = 0
    cfg.venue_us.ws_max_markets = 1

    async def idle_ws(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(USMarketData, "_ws_loop", idle_ws)
    fake = _Fake([])
    md, _ = _feed(cfg, fake, keys=True)
    md.start(["a", "b"])
    await asyncio.sleep(0.15)                                            # stream not connected: both polled
    assert {s for k, s in fake.calls if k == "book"} == {"a", "b"}
    md.connected = True
    await asyncio.sleep(0.1)
    fake.calls.clear()
    await asyncio.sleep(0.15)
    assert {s for k, s in fake.calls if k == "book"} == {"b"}
    await md.stop()


async def test_new_universe_resubscribes_the_stream_only_when_its_markets_change(monkeypatch):
    cfg = Config()
    cfg.venue_us.book_poll_seconds = 10
    cfg.venue_us.volume_poll_seconds = 0
    cfg.venue_us.ws_max_markets = 2
    starts = []

    async def fake_ws(self):
        starts.append(list(self._ws_slugs))
        await asyncio.sleep(3600)

    monkeypatch.setattr(USMarketData, "_ws_loop", fake_ws)
    md, _ = _feed(cfg, _Fake([]), keys=True)
    md.start(["a", "b", "c"])
    await asyncio.sleep(0.01)
    poll = md._poll_task
    await md.set_slugs(["b", "a", "d"])                                  # same streamed set: no reconnect
    await asyncio.sleep(0.01)
    assert starts == [["a", "b"]] and md._slugs == ["b", "a", "d"] and md._poll_task is poll
    await md.set_slugs(["d", "e", "a"])                                  # b left: keep a, add d
    await asyncio.sleep(0.01)
    assert starts == [["a", "b"], ["a", "d"]] and md._poll_task is poll and not poll.done()
    await md.stop()
    assert md._tasks == [] and poll.done()


async def test_throttle_spaces_requests():
    t = Throttle(50)
    t0 = time.monotonic()
    await asyncio.gather(*(t.wait() for _ in range(6)))
    assert time.monotonic() - t0 >= 5 / 50 * 0.9
    free = Throttle(0)
    t0 = time.monotonic()
    await asyncio.gather(*(free.wait() for _ in range(100)))
    assert time.monotonic() - t0 < 0.05


# ------------------------------------------------------------------ end to end

async def test_record_run_end_to_end_writes_counters_for_every_market(tmp_path):
    from pm.engine import Engine
    cfg = Config()
    cfg.venue = "polymarket_us"
    cfg.as_recorder()
    cfg.db_path = str(tmp_path / "m_{venue}.sqlite")
    cfg.tick_seconds = 0.05
    cfg.record.book_poll_seconds = cfg.venue_us.book_poll_seconds = 0.05
    cfg.venue_us.volume_poll_seconds = 0.05
    cfg.venue_us.max_requests_per_second = 0                  # the fake venue has no rate limit
    cfg.secrets.us_key_id = cfg.secrets.us_secret_key = ""
    game = {"slug": "nyk-bos", "title": "Knicks vs Celtics", "category": "sports", "teams": [{"id": 1}],
            "markets": [_row("ml"), _row("spread")]}
    politics = {"slug": "ga", "title": "Georgia", "category": "politics", "markets": [_row("ga-a")]}
    counter = {"v": 0}

    def grow():
        counter["v"] += 3
        return str(counter["v"])

    e = Engine(cfg)
    fake = _Fake([game, politics], full={"nyk-bos": game}, volume={"ml": grow, "spread": grow, "ga-a": grow})
    e.venue.public = fake
    task = asyncio.create_task(e.run())
    await asyncio.sleep(1.0)
    e.request_stop()
    await task
    con = sqlite3.connect(cfg.db_file)
    try:
        tokens = {r[0] for r in con.execute("SELECT DISTINCT token_id FROM books WHERE traded_total IS NOT NULL")}
        assert {"ml|L", "spread|L", "ga-a|L"} <= tokens
        assert con.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        kinds = [r[0] for r in con.execute("SELECT kind FROM events ORDER BY ts")]
        assert kinds[0] == "start" and kinds[-1] == "stop"
        assert "mode=read_only" in con.execute("SELECT detail FROM events WHERE kind='start'").fetchone()[0]
        tags = [r[0] for r in con.execute("SELECT json FROM markets WHERE condition_id='ml'")]
        assert '"sports"' in tags[0]
    finally:
        con.close()
