"""Pytest configuration and fixtures."""

import pytest
from decimal import Decimal
from datetime import datetime
from typing import Dict, List

from storage.models import (
    Market, Token, OrderBook, OrderBookLevel, Position,
    OrderSide, TradeMode
)


@pytest.fixture
def sample_token() -> Token:
    """Create a sample token for testing."""
    return Token(
        token_id="0x123abc",
        market_id="market_001",
        outcome="Yes",
        winner=None,
    )


@pytest.fixture
def sample_market(sample_token: Token) -> Market:
    """Create a sample market for testing."""
    no_token = Token(
        token_id="0x456def",
        market_id="market_001",
        outcome="No",
        winner=None,
    )
    return Market(
        market_id="market_001",
        event_id="event_001",
        condition_id="condition_001",
        title="Will it rain tomorrow?",
        slug="will-it-rain-tomorrow",
        description="Resolves YES if it rains in NYC tomorrow.",
        active=True,
        closed=False,
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 12, 31),
        tokens=[sample_token, no_token],
        category="weather",
        liquidity=Decimal("10000"),
        volume=Decimal("50000"),
        last_updated=datetime.utcnow(),
    )


@pytest.fixture
def sample_orderbook(sample_token: Token) -> OrderBook:
    """Create a sample orderbook for testing."""
    return OrderBook(
        token_id=sample_token.token_id,
        timestamp=datetime.utcnow(),
        bids=[
            OrderBookLevel(price=Decimal("0.50"), size=Decimal("100")),
            OrderBookLevel(price=Decimal("0.49"), size=Decimal("200")),
            OrderBookLevel(price=Decimal("0.48"), size=Decimal("300")),
        ],
        asks=[
            OrderBookLevel(price=Decimal("0.52"), size=Decimal("100")),
            OrderBookLevel(price=Decimal("0.53"), size=Decimal("200")),
            OrderBookLevel(price=Decimal("0.54"), size=Decimal("300")),
        ],
    )


@pytest.fixture
def sample_position(sample_token: Token) -> Position:
    """Create a sample position for testing."""
    return Position(
        token_id=sample_token.token_id,
        shares=Decimal("100"),
        avg_cost=Decimal("0.45"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("5"),  # Current price ~0.50
        timestamp=datetime.utcnow(),
    )
