"""`pm measure`: queue depth vs trading flow per market, checked against hand-computed values."""

import math
import sqlite3
from decimal import Decimal

import pytest

from pm.measure import MAX_GAP_S, measure_tape, measure_token, print_report
from pm.models import Side
from pm.store import Store
from pm.venues.polymarket_us import books_from_us, market_from_us
from tests.conftest import D, mk_market


def _us(slug="x", tick=0.01):
    return market_from_us({"slug": slug, "title": slug.upper(), "outcomes": "[\"Yes\",\"No\"]", "feeCoefficient": 0.06,
                           "minimumTradeQty": 1, "orderPriceMinTickSize": tick, "endDate": "2099-01-01T00:00:00Z",
                           "active": True}, {"slug": "e", "title": "E", "category": "politics"})


def _row(s, ts, tok, bid, ask, bsz, asz, traded=None):
    s._q(s._BOOK_SQL_VOL, (ts, tok, bid, ask, str(bsz), str(asz), "{}", traded))


def test_spread_depth_moves_and_observed_time():
    s = Store(":memory:")
    m = _us()
    s.market(m)
    _row(s, 0, "x|L", "0.50", "0.51", 1000, 2000)     # 1 tick for 100 s
    _row(s, 100, "x|L", "0.50", "0.52", 3000, 2000)   # 2 ticks for 100 s (mid moved)
    _row(s, 200, "x|L", "0.49", "0.53", 5000, 2000)   # 4 ticks for 300 s (mid unchanged)
    _row(s, 300, "x|L", "0.49", "0.53", 5000, 2000)
    _row(s, 500, "x|L", "0.49", "0.53", 5000, 2000)
    _row(s, 500 + MAX_GAP_S + 1, "x|L", "0.10", "0.90", 1, 1)   # after a gap: not observed time
    r = measure_token(s, m, "x|L", 0, 20)
    assert r.hours == pytest.approx(500 / 3600)
    assert (r.spread_1, r.spread_2, r.spread_3plus) == pytest.approx((0.2, 0.2, 0.6))
    assert r.bid_depth == 5000 and r.ask_depth == 2000          # time-weighted medians
    assert r.mid_moves_h == pytest.approx(1 / (500 / 3600))
    assert r.can_improve == pytest.approx(0.8)
    s.close()


def test_flow_from_trades_by_side_counts_only_observed_time():
    s = Store(":memory:")
    m = _us()
    s.market(m)
    for k in range(0, 3601, 60):
        _row(s, k, "x|L", "0.50", "0.51", 980, 480)               # an hour, observed
    _row(s, 3600 + 10_000, "x|L", "0.50", "0.51", 980, 480)       # after a long unobserved gap
    s.trade("x|L", D("0.50"), D(100), Side.SELL, 100.0)           # sellers hit the bid
    s.trade("x|L", D("0.51"), D(25), Side.BUY, 200.0)             # buyers lift the ask
    s.trade("x|S", D("0.49"), D(100), Side.BUY, 100.0)            # mirror copy: the short book is not measured
    s.trade("x|L", D("0.50"), D(999), Side.SELL, 5000.0)          # inside the unobserved gap: ignored
    r = measure_token(s, m, "x|L", 0, 20)
    assert r.flow_source == "trades" and r.trades == 2
    assert r.sell_flow_h == pytest.approx(100) and r.buy_flow_h == pytest.approx(25)
    assert r.fill_join_bid_h == pytest.approx((980 + 20) / 100)   # 10 h to work through the bid queue
    assert r.fill_join_ask_h == pytest.approx((480 + 20) / 25)
    assert r.best_join_h == pytest.approx(10)
    assert math.isinf(r.best_improve_h)                            # 1-tick spread: no room to improve
    s.close()


def test_flow_from_venue_counter_handles_resets():
    s = Store(":memory:")
    m = _us()
    s.market(m)
    counter = 1000
    for k in range(91):                                            # 90 minutes, a row a minute
        if k == 45:
            counter = 50                                           # counter reset (e.g. daily): not negative
        _row(s, 60 * k, "x|L", "0.50", "0.52", 100, 100, str(counter))
        counter += 10 if k < 30 else 0                             # +300 in the first 30 min...
        counter += 5 if k >= 70 and k < 90 else 0                  # ...and +100 near the end
    r = measure_token(s, m, "x|L", 0, 20)
    assert r.flow_source == "venue"
    assert r.sell_flow_h == pytest.approx(400 / 2 / 1.5) and r.buy_flow_h == r.sell_flow_h
    assert r.fill_improve_bid_h == pytest.approx(20 / (400 / 2 / 1.5))   # 2-tick spread: can improve
    s.close()


def test_book_only_flow_is_labelled_an_upper_bound():
    s = Store(":memory:")
    m = _us()
    s.market(m)
    for k in range(30):
        _row(s, 60 * k, "x|L", "0.50", "0.51", 1000, 500)
    for k in range(30, 60):
        _row(s, 60 * k, "x|L", "0.50", "0.51", 700, 500)    # 300 left the bid (traded or cancelled)
    _row(s, 3600, "x|L", "0.49", "0.51", 400, 500)         # the 0.50 level (700) vanished
    r = measure_token(s, m, "x|L", 0, 20)
    assert r.flow_source == "<=book"
    assert r.sell_flow_h == pytest.approx(1000) and r.buy_flow_h == 0
    s.close()


def test_mirrored_short_books_are_not_measured_twice_and_old_tapes_migrate(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)                        # a tape written before the traded_total column existed
    con.execute("CREATE TABLE books (ts REAL, token_id TEXT, best_bid TEXT, best_ask TEXT, bid_sz TEXT, "
                "ask_sz TEXT, top TEXT)")
    con.commit()
    con.close()
    s = Store(str(path))
    assert "traded_total" in {r[1] for r in s._db.execute("PRAGMA table_info(books)")}
    m = _us()
    s.market(m)
    intl = mk_market("0xc", "Y1", "N1")
    s.market(intl)
    for b in books_from_us("x", {"bids": [{"px": {"value": "0.5"}, "qty": "10"}],
                                 "offers": [{"px": {"value": "0.51"}, "qty": "10"}]}):
        _row(s, 0, b.token_id, str(b.best_bid), str(b.best_ask), 10, 10)
        _row(s, 60, b.token_id, str(b.best_bid), str(b.best_ask), 10, 10)
    for tok in ("Y1", "N1"):
        _row(s, 0, tok, "0.40", "0.42", 10, 10)
        _row(s, 60, tok, "0.40", "0.42", 10, 10)
    rows, info = measure_tape(s)
    assert sorted(r.token_id for r in rows) == ["N1", "Y1", "x|L"]
    s.close()


def test_us_book_payload_carries_the_volume_counter_onto_the_tape():
    long_b, short_b = books_from_us("x", {"marketData": {
        "bids": [{"px": {"value": "0.5"}, "qty": "10"}], "offers": [{"px": {"value": "0.51"}, "qty": "10"}],
        "stats": {"sharesTraded": "12345", "openInterest": "999"}}})
    assert long_b.traded_total == D(12345) and short_b.traded_total == D(12345)
    s = Store(":memory:", book_snapshot_interval=0.0)
    s.book(long_b)
    assert s.query("SELECT traded_total FROM books")[0][0] == "12345"
    s.close()


def test_report_is_plain_ascii_for_windows_redirection(capsys):
    s = Store(":memory:")
    m = _us()
    s.market(m)
    _row(s, 0, "x|L", "0.50", "0.51", 1000, 500)
    _row(s, 60, "x|L", "0.50", "0.51", 700, 500)
    rows, info = measure_tape(s)
    print_report(rows, info)
    out = capsys.readouterr().out
    out.encode("cp1252")                              # a redirected Windows stdout uses the locale code page
    assert ">=" in out and "lower bounds" in out
    s.close()
