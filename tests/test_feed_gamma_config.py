import json
from decimal import Decimal
from pathlib import Path

from pm.config import Config
from pm.feed import MarketFeed
from pm.gamma import market_from_gamma
from pm.models import Mode, Side
from tests.conftest import D


def test_feed_parses_book_price_change_and_trade():
    books, trades = [], []
    f = MarketFeed("wss://x", books.append, trades.append)
    f._handle_raw(json.dumps([{
        "event_type": "book", "asset_id": "T1", "market": "0xc",
        "bids": [{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "5"}],
        "asks": [{"price": "0.45", "size": "7"}], "hash": "h", "timestamp": "1700000000000",
    }]))
    assert len(books) == 1 and books[0].best_bid == D("0.42") and books[0].best_ask == D("0.45")

    f._handle_raw(json.dumps({
        "event_type": "price_change", "market": "0xc",
        "price_changes": [{"asset_id": "T1", "price": "0.43", "size": "9", "side": "BUY", "best_bid": "0.43", "best_ask": "0.45"}],
        "timestamp": "1700000001000",
    }))
    assert books[-1].best_bid == D("0.43")

    f._handle_raw(json.dumps({
        "event_type": "last_trade_price", "asset_id": "T1", "price": "0.45", "size": "3", "side": "BUY",
        "timestamp": "1700000002000",
    }))
    assert trades[0].price == D("0.45") and trades[0].side is Side.BUY and trades[0].ts == 1700000002.0
    f._handle_raw("PONG")  # ignored
    assert f.messages == 3


def test_gamma_parses_json_string_fields():
    row = {
        "conditionId": "0xabc", "question": "Will X?", "slug": "will-x",
        "clobTokenIds": json.dumps(["111", "222"]), "outcomes": json.dumps(["Yes", "No"]),
        "negRisk": False, "orderPriceMinTickSize": 0.01, "orderMinSize": 5,
        "endDate": "2026-12-31T00:00:00Z", "liquidityNum": 12345.6, "volume24hr": 999.9,
        "events": [{"id": "77", "slug": "ev", "tags": [{"label": "Politics"}]}],
        "acceptingOrders": True,
    }
    m = market_from_gamma(row)
    assert m.condition_id == "0xabc" and [t.token_id for t in m.tokens] == ["111", "222"]
    assert m.yes.token_id == "111" and m.no.token_id == "222"
    assert m.event_id == "77" and m.tags == ["politics"]
    assert m.end_date.tzinfo is not None and m.tick_size == D("0.01")
    assert market_from_gamma({"conditionId": "x", "clobTokenIds": "[]", "outcomes": "[]"}) is None


def test_config_loads_yaml_and_env(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "mode: read_only\nrisk:\n  max_total_notional_usd: 50\nstrategies:\n  maker:\n    enabled: true\n    half_spread: 0.03\n")
    (tmp_path / ".env").write_text("LIVE_TRADING=yes\nPM_SIGNATURE_TYPE=2\n")
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.mode is Mode.READ_ONLY
    assert cfg.risk.max_total_notional_usd == D(50) and isinstance(cfg.risk.max_total_notional_usd, Decimal)
    assert cfg.strategy("maker")["half_spread"] == 0.03
    assert cfg.secrets.live_trading_ack and cfg.secrets.signature_type == 2
