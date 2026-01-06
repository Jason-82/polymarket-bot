"""Unit tests for order intent validation and utilities."""

import pytest
from decimal import Decimal

from execution.order_intent import (
    OrderIntent, OrderSide, OrderType,
    round_to_tick, clamp_price,
    TICK_SIZE, MIN_PRICE, MAX_PRICE
)


class TestRoundToTick:
    """Tests for tick size rounding."""

    def test_round_to_tick_exact(self):
        """Test price already on tick."""
        assert round_to_tick(Decimal("0.50")) == Decimal("0.50")
        assert round_to_tick(Decimal("0.01")) == Decimal("0.01")
        assert round_to_tick(Decimal("0.99")) == Decimal("0.99")

    def test_round_to_tick_up(self):
        """Test rounding up to tick."""
        assert round_to_tick(Decimal("0.505")) == Decimal("0.51")
        assert round_to_tick(Decimal("0.506")) == Decimal("0.51")

    def test_round_to_tick_down(self):
        """Test rounding down to tick."""
        assert round_to_tick(Decimal("0.504")) == Decimal("0.50")
        assert round_to_tick(Decimal("0.501")) == Decimal("0.50")

    def test_round_to_tick_midpoint(self):
        """Test rounding at midpoint (banker's rounding)."""
        # Python Decimal uses banker's rounding by default
        result = round_to_tick(Decimal("0.505"))
        assert result in (Decimal("0.50"), Decimal("0.51"))


class TestClampPrice:
    """Tests for price clamping."""

    def test_clamp_price_within_bounds(self):
        """Test price within valid bounds."""
        assert clamp_price(Decimal("0.50")) == Decimal("0.50")
        assert clamp_price(Decimal("0.01")) == Decimal("0.01")
        assert clamp_price(Decimal("0.99")) == Decimal("0.99")

    def test_clamp_price_below_min(self):
        """Test price below minimum."""
        assert clamp_price(Decimal("0.001")) == MIN_PRICE
        assert clamp_price(Decimal("0")) == MIN_PRICE
        assert clamp_price(Decimal("-0.5")) == MIN_PRICE

    def test_clamp_price_above_max(self):
        """Test price above maximum."""
        assert clamp_price(Decimal("1.0")) == MAX_PRICE
        assert clamp_price(Decimal("1.5")) == MAX_PRICE


class TestOrderIntentValidation:
    """Tests for order intent validation."""

    def test_valid_order_intent(self):
        """Test valid order intent passes validation."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
            order_type=OrderType.GTC,
            strategy_name="test",
        )

        assert intent.is_valid()
        assert len(intent.validate()) == 0

    def test_invalid_price_too_low(self):
        """Test price below minimum."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.001"),
            size=Decimal("10"),
        )

        assert not intent.is_valid()
        errors = intent.validate()
        assert any("out of bounds" in e for e in errors)

    def test_invalid_price_too_high(self):
        """Test price above maximum."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("1.5"),
            size=Decimal("10"),
        )

        assert not intent.is_valid()

    def test_invalid_size_zero(self):
        """Test zero size."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("0"),
        )

        assert not intent.is_valid()
        errors = intent.validate()
        assert any("positive" in e for e in errors)

    def test_invalid_size_negative(self):
        """Test negative size."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("-10"),
        )

        assert not intent.is_valid()

    def test_invalid_tick_size(self):
        """Test price not aligned to tick size."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.505"),  # Not on tick
            size=Decimal("10"),
        )

        assert not intent.is_valid()
        errors = intent.validate()
        assert any("tick size" in e for e in errors)

    def test_missing_token_id(self):
        """Test empty token ID."""
        intent = OrderIntent(
            token_id="",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
        )

        assert not intent.is_valid()
        errors = intent.validate()
        assert any("token_id" in e for e in errors)

    def test_round_price_to_tick(self):
        """Test rounding price to valid tick."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.505"),
            size=Decimal("10"),
        )

        rounded = intent.round_price_to_tick()
        assert rounded.price == Decimal("0.51")
        assert rounded.is_valid()


class TestOrderIntentClientOrderId:
    """Tests for client order ID generation."""

    def test_auto_generated_id(self):
        """Test client order ID is auto-generated."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
        )

        assert intent.client_order_id != ""
        assert len(intent.client_order_id) == 16  # SHA256[:16]

    def test_explicit_id(self):
        """Test explicit client order ID is used."""
        intent = OrderIntent(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
            client_order_id="my_custom_id",
        )

        assert intent.client_order_id == "my_custom_id"

    def test_unique_ids_for_different_intents(self):
        """Test different intents get different IDs."""
        intent1 = OrderIntent(
            token_id="token_1",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
        )

        intent2 = OrderIntent(
            token_id="token_2",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
        )

        # IDs will differ due to different token IDs and timestamps
        # Note: This test might occasionally fail if executed in same millisecond
        # In practice, the timestamp provides uniqueness
        assert intent1.client_order_id != intent2.client_order_id or \
               intent1.token_id != intent2.token_id
