"""Unit tests for data models."""

import pytest
from decimal import Decimal
from datetime import datetime

from storage.models import (
    Token, Market, OrderBook, OrderBookLevel, Position,
    OrderIntent, Order, Fill, OrderSide, OrderType, TradeMode
)


class TestOrderBook:
    """Tests for OrderBook model."""

    def test_empty_orderbook(self):
        """Test empty orderbook properties."""
        ob = OrderBook(
            token_id="test_token",
            timestamp=datetime.utcnow(),
            bids=[],
            asks=[],
        )

        assert ob.best_bid is None
        assert ob.best_ask is None
        assert ob.midpoint is None
        assert ob.spread is None

    def test_orderbook_with_levels(self, sample_orderbook):
        """Test orderbook with bid/ask levels."""
        assert sample_orderbook.best_bid == Decimal("0.50")
        assert sample_orderbook.best_ask == Decimal("0.52")
        assert sample_orderbook.midpoint == Decimal("0.51")
        assert sample_orderbook.spread == Decimal("0.02")

    def test_orderbook_best_sizes(self, sample_orderbook):
        """Test best bid/ask sizes."""
        assert sample_orderbook.best_bid_size == Decimal("100")
        assert sample_orderbook.best_ask_size == Decimal("100")

    def test_spread_percentage(self, sample_orderbook):
        """Test spread as percentage of midpoint."""
        spread_pct = sample_orderbook.spread_pct
        assert spread_pct is not None
        # 0.02 / 0.51 ≈ 0.0392
        assert Decimal("0.03") < spread_pct < Decimal("0.05")


class TestPosition:
    """Tests for Position model."""

    def test_cost_basis(self, sample_position):
        """Test cost basis calculation."""
        # 100 shares * 0.45 avg cost = 45
        assert sample_position.cost_basis == Decimal("45")

    def test_market_value(self, sample_position):
        """Test market value calculation."""
        # cost_basis + unrealized_pnl = 45 + 5 = 50
        assert sample_position.market_value == Decimal("50")

    def test_update_unrealized_pnl(self):
        """Test updating unrealized PnL."""
        position = Position(
            token_id="test",
            shares=Decimal("100"),
            avg_cost=Decimal("0.45"),
            timestamp=datetime.utcnow(),
        )

        position.update_unrealized_pnl(Decimal("0.50"))
        # (0.50 - 0.45) * 100 = 5
        assert position.unrealized_pnl == Decimal("5")

        position.update_unrealized_pnl(Decimal("0.40"))
        # (0.40 - 0.45) * 100 = -5
        assert position.unrealized_pnl == Decimal("-5")


class TestOrderIntent:
    """Tests for OrderIntent model."""

    def test_order_intent_creation(self):
        """Test creating an order intent."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
            order_type=OrderType.GTC,
            strategy_name="test_strategy",
        )

        assert intent.token_id == "test_token"
        assert intent.side == OrderSide.BUY
        assert intent.price == Decimal("0.50")
        assert intent.size == Decimal("10")
        assert intent.client_order_id != ""  # Auto-generated

    def test_order_intent_notional(self):
        """Test notional value calculation."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
        )

        assert intent.notional == Decimal("5.00")  # 0.50 * 10


class TestMarket:
    """Tests for Market model."""

    def test_yes_no_tokens(self, sample_market):
        """Test getting YES/NO tokens."""
        yes_token = sample_market.yes_token
        no_token = sample_market.no_token

        assert yes_token is not None
        assert no_token is not None
        assert yes_token.outcome.lower() == "yes"
        assert no_token.outcome.lower() == "no"

    def test_market_hash(self, sample_market):
        """Test market can be hashed (for sets/dicts)."""
        markets = {sample_market}
        assert sample_market in markets


class TestOrder:
    """Tests for Order model."""

    def test_filled_size(self):
        """Test filled size calculation."""
        order = Order(
            order_id="order_1",
            client_order_id="client_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            original_size=Decimal("100"),
            remaining_size=Decimal("40"),
            status="open",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        assert order.filled_size == Decimal("60")
        assert order.is_open is True

    def test_fully_filled(self):
        """Test fully filled order."""
        order = Order(
            order_id="order_1",
            client_order_id="client_1",
            token_id="token_1",
            side=OrderSide.SELL,
            price=Decimal("0.50"),
            original_size=Decimal("100"),
            remaining_size=Decimal("0"),
            status="filled",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        assert order.filled_size == Decimal("100")
        assert order.is_open is False


class TestFill:
    """Tests for Fill model."""

    def test_fill_notional(self):
        """Test fill notional calculation."""
        fill = Fill(
            fill_id="fill_1",
            order_id="order_1",
            client_order_id="client_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("100"),
            fee=Decimal("0.50"),
            timestamp=datetime.utcnow(),
            mode=TradeMode.PAPER,
        )

        assert fill.notional == Decimal("50")  # 0.50 * 100
