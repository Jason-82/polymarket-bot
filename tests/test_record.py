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

    def __init__(self, events, full=None, books=None, volume=None, soon=None, page_cap=100):
        self._events, self._full, self._soon, self._cap = events, full or {}, soon, page_cap
        self.list_params: list[dict] = []
        self._books, self._volume = books or {}, volume or {}
        self.events = self
        self.markets = self
        self.calls: list[tuple[str, str]] = []

    async def list(self, params):
        self.list_params.append(params)
        rows = self._soon if ("startTimeMin" in params and self._soon is not None) else self._events
        off = params.get("offset", 0)
        return {"events": rows[off:off + min(params.get("limit", 100), self._cap)]}

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
    # 3 per event; one-sided books and near-certain listed prices left out; each event's first market first (the
    # streamed ones come from different events), then the rest in venue (volume) order
    assert out == ["g0", "ga-a", "fed-dec", "g2", "g3", "ga-b", "fed-jan"]
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


async def test_games_starting_soon_are_interleaved_with_the_busiest_events():
    from datetime import datetime, timedelta, timezone
    cfg = Config()
    cfg.as_recorder()
    cfg.record.max_markets = 6
    soon = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    futures = [{"slug": f"fut{k}", "title": f"Champion {k}", "category": "sports", "markets": [_row(f"f{k}")]}
               for k in range(5)]
    games = [{"slug": f"game{k}", "title": f"A{k} vs B{k}", "category": "sports", "startTime": soon,
              "markets": [_row(f"gm{k}")]} for k in range(3)]
    stale = {"slug": "old", "title": "C vs D", "startTime": "2020-01-01T00:00:00Z", "markets": [_row("old")]}
    v = _venue(cfg, _Fake(futures + games, soon=[stale] + games))
    out = [m.condition_id for m in await v.select_universe()]
    assert out == ["gm0", "f0", "gm1", "f1", "gm2", "f2"]               # games are not crowded out; 'old' is dropped
    p = next(p for p in v.public.list_params if "startTimeMin" in p)
    assert p["ended"] is False and p["startTimeMax"] > p["startTimeMin"]


async def test_event_listing_survives_a_small_page_cap_and_an_ignored_offset():
    cfg = Config()
    events = [{"slug": f"e{k}", "markets": []} for k in range(230)]
    v = _venue(cfg, _Fake(events, page_cap=50))
    assert len(await v.list_events_raw(400)) == 230
    v = _venue(cfg, _Fake(events[:30]))

    async def same_page(params):                                        # a venue that ignores offset
        return {"events": events[:30]}
    v.public.list = same_page
    assert len(await v.list_events_raw(400)) == 30


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
    md.connected = True                                                  # open, but nothing arrived for a yet
    await asyncio.sleep(0.1)
    fake.calls.clear()
    await asyncio.sleep(0.15)
    assert {s for k, s in fake.calls if k == "book"} == {"a", "b"}       # a silent subscription is polled too
    md._on_market_data({"marketData": {"marketSlug": "a", **_book()}})   # the stream delivers for a
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


# ------------------------------------------------------------------ review findings: the feed

async def test_a_counter_older_than_the_carry_limit_is_never_put_on_new_books():
    cfg = Config()
    cfg.venue_us.volume_poll_seconds = 60                               # carry limit max(150, 2.5 x 60) = 150 s
    md, got = _feed(cfg, None)
    md._slugs = ["x"]
    md._note_volume("x", {"marketData": {"sharesTraded": "1000"}})
    md._emit_books("x", _book())
    assert got[-1].traded_total == D(1000)                              # fresh: carried
    md._last_traded["x"] = (D(1000), time.time() - 3 * 3600)            # read before a 3 h gap (sleep, outage)
    md._emit_books("x", _book())
    assert got[-1].traded_total is None                                 # not stamped with the stale value


async def test_markets_leaving_the_universe_forget_their_counter_even_with_a_reply_in_flight(monkeypatch):
    async def idle(self):
        await asyncio.sleep(3600)
    monkeypatch.setattr(USMarketData, "_poll_loop", idle)
    monkeypatch.setattr(USMarketData, "_volume_loop", idle)
    md, got = _feed(Config(), None)
    md.start(["x", "y"])
    md._note_volume("x", {"sharesTraded": "1000"})
    await md.set_slugs(["y"])
    assert "x" not in md._last_traded
    assert md._note_volume("x", {"sharesTraded": "1000"}) is None and "x" not in md._last_traded  # late reply
    await md.set_slugs(["x", "y"])
    md._emit_books("x", _book())
    assert got[-1].traded_total is None
    await md.stop()


async def test_poll_loops_survive_bad_payloads_and_handler_errors():
    cfg = Config()
    cfg.venue_us.book_poll_seconds = cfg.venue_us.volume_poll_seconds = 0.02
    calls = {"n": 0}
    got = []

    def flaky_on_book(b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")                    # the store failing once
        got.append(b)

    class Bad(_Fake):
        async def book(self, slug):
            return ["not", "a", "dict"] if slug == "bad" else {"marketData": _book()}

        async def bbo(self, slug):
            return "garbage"

    md = USMarketData(cfg, Bad([]), flaky_on_book, lambda t: None)
    md.start(["bad", "ok"])
    await asyncio.sleep(0.3)
    assert not md._poll_task.done() and not md._volume_task.done()
    assert any(b.token_id == "ok|L" for b in got)
    md._on_market_data({"marketData": "not a dict"})                    # the stream reader survives too
    md._on_trade_msg({"trade": {"marketSlug": "ok", "price": "x"}})
    await md.stop()


async def test_a_dead_poll_loop_is_restarted_at_the_next_universe_refresh(monkeypatch):
    runs = []

    async def dies(self):
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)
    monkeypatch.setattr(USMarketData, "_poll_loop", dies)
    md, _ = _feed(Config(), _Fake([]))
    md.start(["a"])
    await asyncio.sleep(0.01)
    assert md._poll_task.done()
    await md.set_slugs(["a"])
    await asyncio.sleep(0.01)
    assert len(runs) == 2 and not md._poll_task.done()
    await md.stop()


async def test_a_slow_counter_endpoint_does_not_slow_the_books():
    cfg = Config()
    cfg.venue_us.book_poll_seconds = 0.02
    cfg.venue_us.volume_poll_seconds = 0.02

    class SlowBbo(_Fake):
        async def bbo(self, slug):
            await asyncio.sleep(10)

    fake = SlowBbo([])
    md, _ = _feed(cfg, fake)
    md.start(["a", "b", "c", "d", "e"])
    await asyncio.sleep(0.3)
    assert sum(1 for k, _ in fake.calls if k == "book") >= 15             # several book rounds went by
    await md.stop()


async def test_missing_counter_and_failing_polls_are_warned_about(caplog):
    import logging
    cfg = Config()
    cfg.venue_us.book_poll_seconds = cfg.venue_us.volume_poll_seconds = 0.02

    class Failing(_Fake):
        async def book(self, slug):
            raise RuntimeError("429 Too Many Requests")

    caplog.set_level(logging.INFO)
    md, _ = _feed(cfg, Failing([], volume={}))
    md.start([f"m{k}" for k in range(20)])
    await asyncio.sleep(0.2)
    await md.stop()
    text = caplog.text
    assert "us_volume_counter_missing" in text and "us_book_polls_failing" in text and "429" in text
    assert "sample=" in text                                            # one raw bbo reply, to check its shape


def test_feed_counts_as_connected_only_while_data_arrives():
    cfg = Config()
    v = PolymarketUSVenue.__new__(PolymarketUSVenue)
    v.cfg = cfg
    v.data, _ = _feed(cfg, None)
    v.data.last_message_ts = time.time()
    assert v.feed_connected
    v.data.last_message_ts = time.time() - 600
    assert not v.feed_connected


def test_trade_side_follows_the_takers_intent():
    from pm.models import Side
    from pm.venues.polymarket_us import trades_from_us

    def side(taker):
        return trades_from_us("x", {"price": "0.40", "quantity": "5", "taker": taker})[0].side

    assert side({"side": "ORDER_SIDE_BUY", "intent": "ORDER_INTENT_BUY_SHORT"}) is Side.SELL   # buying NO hits the bid
    assert side({"side": "ORDER_SIDE_SELL", "intent": "ORDER_INTENT_SELL_SHORT"}) is Side.BUY
    assert side({"side": "ORDER_SIDE_BUY", "intent": "ORDER_INTENT_BUY_LONG"}) is Side.BUY
    assert side({"side": "ORDER_SIDE_SELL"}) is Side.SELL                                    # no intent: side


def test_empty_counter_values_are_absent_not_zero():
    for v in ({}, {"value": None}, {"value": ""}, {"currency": "USD"}, "", "  ", True, "-5", "nan"):
        assert traded_total_from_us({"sharesTraded": v}) is None, v
    assert traded_total_from_us(["x"]) is None and traded_total_from_us("x") is None


# ------------------------------------------------------------------ review findings: the CLI and config

def test_list_settings_accept_a_bare_word_or_nothing(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("record:\n  exclude_tags: crypto\nuniverse:\n  exclude_tags:\n", encoding="utf-8")
    cfg = Config.load(p, env_path="/nonexistent/.env")
    assert cfg.record.exclude_tags == ["crypto"] and cfg.universe.exclude_tags == []
    assert cfg.as_recorder().universe.exclude_tags == ["crypto"]


def test_a_second_recorder_on_the_same_tape_is_refused(tmp_path):
    from pm.__main__ import _lock
    first = _lock(tmp_path / "t.sqlite.lock")
    assert first is not None
    import subprocess
    import sys
    code = ("import sys; from pathlib import Path; from pm.__main__ import _lock; "
            f"sys.exit(0 if _lock(Path(r'{tmp_path / 't.sqlite.lock'}')) is None else 1)")
    assert subprocess.run([sys.executable, "-c", code], cwd=".").returncode == 0     # another process: refused
    first.close()
    assert subprocess.run([sys.executable, "-c", code], cwd=".").returncode == 1     # free again


def test_record_without_keys_warns_in_the_banner(monkeypatch, capsys, tmp_path):
    import pm.__main__ as cli
    cfg = Config()
    cfg.venue = "polymarket_us"
    cfg.as_recorder()
    cfg.db_path = str(tmp_path / "m_{venue}.sqlite")
    seen = {}
    monkeypatch.setattr(cli, "_run", lambda c, banner=None: seen.setdefault("banner", banner) and 0)
    cli._record(cfg)
    assert "WARNING" in seen["banner"] and "no trade prints" in seen["banner"]
    assert "Do not run the paper bot at the same time" in seen["banner"]


# ------------------------------------------------------------------ second review

async def test_trade_prints_alone_do_not_count_as_a_delivering_book_stream(monkeypatch):
    cfg = Config()
    cfg.venue_us.book_poll_seconds = 0.03
    cfg.venue_us.volume_poll_seconds = 0
    cfg.venue_us.ws_max_markets = 1

    async def idle_ws(self):
        await asyncio.sleep(3600)
    monkeypatch.setattr(USMarketData, "_ws_loop", idle_ws)
    fake = _Fake([])
    md, _ = _feed(cfg, fake, keys=True)
    md.start(["a", "b"])
    md.connected = True
    for _ in range(5):
        md._on_trade_msg({"trade": {"marketSlug": "a", "price": {"value": "0.5"}, "quantity": "1",
                                    "taker": {"intent": "ORDER_INTENT_BUY_LONG"}}})
        await asyncio.sleep(0.04)
    assert ("book", "a") in fake.calls and not md.stream_fresh("a")
    await md.stop()


async def test_a_rest_book_overtaken_by_a_stream_book_is_dropped():
    cfg = Config()
    got = []

    class Slow(_Fake):
        async def book(self, slug):
            await asyncio.sleep(0.05)
            return {"marketData": _book(bid="0.50", ask="0.52")}

    cfg.venue_us.book_poll_seconds = 10
    cfg.venue_us.volume_poll_seconds = 0
    md = USMarketData(cfg, Slow([]), got.append, lambda t: None)
    md.start(["a"])
    await asyncio.sleep(0.02)                                           # the REST request is out
    md._on_market_data({"marketData": {"marketSlug": "a", **_book(bid="0.55", ask="0.57")}})
    await asyncio.sleep(0.1)
    await md.stop()
    longs = [b for b in got if b.token_id == "a|L"]
    assert longs and longs[-1].best_bid == D("0.55")                    # the newer stream book stays


async def test_the_not_moving_warning_needs_a_counter_that_never_moved(caplog):
    import logging
    cfg = Config()
    cfg.venue_us.book_poll_seconds = 10
    cfg.venue_us.volume_poll_seconds = 0.01
    n = {"v": 0}

    def once():
        n["v"] += 1
        return "100" if n["v"] == 1 else "107"                          # moved once, then quiet

    caplog.set_level(logging.INFO)
    md, _ = _feed(cfg, _Fake([], volume={"a": once}))
    md.start(["a"])
    await asyncio.sleep(0.4)
    await md.stop()
    assert md._volume_rounds >= 12 and "us_volume_counter_not_moving" not in caplog.text
    caplog.clear()
    md, _ = _feed(cfg, _Fake([], volume={"a": "100"}))                  # never moves
    md.start(["a"])
    await asyncio.sleep(0.4)
    await md.stop()
    assert "us_volume_counter_not_moving" in caplog.text


async def test_games_listing_without_start_times_trusts_the_venue_and_warns_when_empty(caplog):
    import logging
    cfg = Config()
    cfg.as_recorder()
    cfg.record.max_markets = 4
    games = [{"slug": f"game{k}", "title": f"A{k} vs B{k}", "markets": [_row(f"gm{k}")]} for k in range(2)]
    futures = [{"slug": f"fut{k}", "title": f"F{k}", "markets": [_row(f"f{k}")]} for k in range(2)]
    v = _venue(cfg, _Fake(futures, soon=games))
    assert [m.condition_id for m in await v.select_universe()] == ["gm0", "f0", "gm1", "f1"]
    caplog.set_level(logging.INFO)
    late = [{"slug": "late", "startTime": "2020-01-01T00:00:00Z", "markets": [_row("late")]}]
    v = _venue(cfg, _Fake(futures, soon=late))
    await v.select_universe()
    assert "us_soon_events_none_kept" in caplog.text


def test_record_banner_prints_the_tape_path_with_forward_slashes(monkeypatch, tmp_path):
    import pm.__main__ as cli
    cfg = Config()
    cfg.venue = "polymarket_us"
    cfg.as_recorder()
    cfg.db_path = "data/" + tmp_path.name + "_m_{venue}.sqlite"
    seen = {}
    monkeypatch.setattr(cli, "_run", lambda c, banner=None: seen.setdefault("banner", banner) and 0)
    cli._record(cfg)
    assert f"tape: data/{tmp_path.name}_m_polymarket_us.sqlite" in seen["banner"]
    import os
    os.remove(f"data/{tmp_path.name}_m_polymarket_us.sqlite.lock")
