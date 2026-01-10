"""Tests for news alpha strategy components."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from connectors.news_sources.base import NewsEvent, NewsCategory
from connectors.news_sources.aggregator import NewsAggregator, AggregatorConfig
from connectors.news_sources.twitter_client import MockTwitterSource
from reasoning.llm_analyzer import (
    TradeSignal,
    SignalDirection,
    AnalysisResult,
    LLMConfig,
    MockLLMAnalyzer,
)
from reasoning.market_mapper import MarketMapper, MapperConfig


class TestNewsEvent:
    """Tests for NewsEvent model."""

    def test_news_event_creation(self):
        event = NewsEvent(
            id="test123",
            headline="Breaking: Major political development",
            source="twitter",
            timestamp=datetime.utcnow(),
            categories=[NewsCategory.POLITICS],
        )

        assert event.id == "test123"
        assert event.source == "twitter"
        assert NewsCategory.POLITICS in event.categories

    def test_news_event_age(self):
        # Create an event from 2 minutes ago
        old_time = datetime.utcnow() - timedelta(minutes=2)
        event = NewsEvent(
            id="old1",
            headline="Old news",
            source="rss",
            timestamp=old_time,
        )

        age = event.age_seconds()
        assert 110 < age < 130  # About 2 minutes

    def test_news_event_is_stale(self):
        # Fresh event
        fresh = NewsEvent(
            id="fresh1",
            headline="Fresh news",
            source="twitter",
            timestamp=datetime.utcnow(),
        )
        assert not fresh.is_stale(max_age_seconds=300)

        # Stale event (10 minutes old)
        stale_time = datetime.utcnow() - timedelta(minutes=10)
        stale = NewsEvent(
            id="stale1",
            headline="Stale news",
            source="twitter",
            timestamp=stale_time,
        )
        assert stale.is_stale(max_age_seconds=300)

    def test_full_text(self):
        event = NewsEvent(
            id="test1",
            headline="Breaking news",
            body="This is the full story with more details.",
            source="rss",
        )

        full = event.full_text()
        assert "Breaking news" in full
        assert "full story" in full


class TestTradeSignal:
    """Tests for TradeSignal model."""

    def test_trade_signal_creation(self):
        signal = TradeSignal(
            market_id="market123",
            token_id="token456",
            direction=SignalDirection.BUY,
            confidence=85.0,
            fair_value_estimate=Decimal("0.65"),
            current_price=Decimal("0.55"),
            edge=0.10,
            reasoning="News suggests higher probability",
        )

        assert signal.direction == SignalDirection.BUY
        assert signal.confidence == 85.0
        assert signal.edge == 0.10

    def test_trade_signal_to_dict(self):
        signal = TradeSignal(
            market_id="market123",
            token_id="token456",
            direction=SignalDirection.SELL,
            confidence=70.0,
            reasoning="Negative development",
        )

        d = signal.to_dict()
        assert d["market_id"] == "market123"
        assert d["direction"] == "sell"
        assert d["confidence"] == 70.0


class TestMarketMapper:
    """Tests for market mapper."""

    @pytest.fixture
    def mapper(self):
        return MarketMapper()

    @pytest.fixture
    def sample_markets(self):
        return [
            {
                "condition_id": "market1",
                "question": "Will Biden win the 2024 election?",
                "description": "Presidential election outcome",
                "tokens": [
                    {"token_id": "token1yes", "outcome": "YES"},
                    {"token_id": "token1no", "outcome": "NO"},
                ],
                "tags": ["politics", "election"],
            },
            {
                "condition_id": "market2",
                "question": "Will Bitcoin exceed $100,000 by end of 2024?",
                "description": "Crypto price prediction",
                "tokens": [
                    {"token_id": "token2yes", "outcome": "YES"},
                    {"token_id": "token2no", "outcome": "NO"},
                ],
                "tags": ["crypto"],
            },
            {
                "condition_id": "market3",
                "question": "Will the Fed raise interest rates in January?",
                "description": "Federal Reserve policy decision",
                "tokens": [
                    {"token_id": "token3yes", "outcome": "YES"},
                    {"token_id": "token3no", "outcome": "NO"},
                ],
                "tags": ["economics", "fed"],
            },
        ]

    def test_update_markets(self, mapper, sample_markets):
        mapper.update_markets(sample_markets)
        assert mapper.market_count == 3

    def test_find_relevant_markets_politics(self, mapper, sample_markets):
        mapper.update_markets(sample_markets)

        # News about Biden should match election market
        event = NewsEvent(
            id="news1",
            headline="Biden announces major policy initiative",
            source="twitter",
            categories=[NewsCategory.POLITICS],
            keywords=["biden", "policy"],
        )

        matches = mapper.find_relevant_markets(event)

        # Should find the Biden election market
        assert len(matches) >= 1
        market_ids = [m.market["condition_id"] for m in matches]
        assert "market1" in market_ids

    def test_find_relevant_markets_crypto(self, mapper, sample_markets):
        mapper.update_markets(sample_markets)

        # News about Bitcoin
        event = NewsEvent(
            id="news2",
            headline="Bitcoin surges past $80,000 on ETF approval news",
            source="twitter",
            categories=[NewsCategory.CRYPTO],
            keywords=["bitcoin", "etf"],
        )

        matches = mapper.find_relevant_markets(event)

        # Should find Bitcoin market
        market_ids = [m.market["condition_id"] for m in matches]
        assert "market2" in market_ids

    def test_find_relevant_markets_economics(self, mapper, sample_markets):
        mapper.update_markets(sample_markets)

        # News about Fed
        event = NewsEvent(
            id="news3",
            headline="Federal Reserve signals potential rate increase",
            source="rss",
            categories=[NewsCategory.ECONOMICS],
            keywords=["fed", "interest rate"],
        )

        matches = mapper.find_relevant_markets(event)

        # Should find Fed market
        market_ids = [m.market["condition_id"] for m in matches]
        assert "market3" in market_ids

    def test_no_matching_markets(self, mapper, sample_markets):
        mapper.update_markets(sample_markets)

        # Completely unrelated news
        event = NewsEvent(
            id="news4",
            headline="Local sports team wins championship",
            source="rss",
            categories=[NewsCategory.SPORTS],
            keywords=["sports", "championship"],
        )

        matches = mapper.find_relevant_markets(event)

        # Should not match any markets (all are politics/crypto/economics)
        # May still get weak matches from keyword overlap
        high_relevance = [m for m in matches if m.relevance_score > 0.5]
        assert len(high_relevance) == 0


class TestMockLLMAnalyzer:
    """Tests for mock LLM analyzer."""

    @pytest.mark.asyncio
    async def test_mock_analyzer_returns_set_signals(self):
        analyzer = MockLLMAnalyzer()

        # Set up mock signals
        mock_signal = TradeSignal(
            market_id="market123",
            token_id="token456",
            direction=SignalDirection.BUY,
            confidence=80.0,
            reasoning="Test signal",
        )
        analyzer.set_mock_signals([mock_signal])

        # Analyze
        event = NewsEvent(
            id="test1",
            headline="Test news",
            source="test",
        )

        result = await analyzer.analyze(event, [], None)

        assert len(result.signals) == 1
        assert result.signals[0].market_id == "market123"
        assert result.has_actionable_signals

    @pytest.mark.asyncio
    async def test_mock_analyzer_empty_signals(self):
        analyzer = MockLLMAnalyzer()

        event = NewsEvent(
            id="test2",
            headline="Irrelevant news",
            source="test",
        )

        result = await analyzer.analyze(event, [], None)

        assert len(result.signals) == 0
        assert not result.has_actionable_signals


class TestMockTwitterSource:
    """Tests for mock Twitter source."""

    @pytest.mark.asyncio
    async def test_mock_twitter_stream(self):
        source = MockTwitterSource()
        await source.connect()

        # Inject test events
        source.inject_event("Breaking: Test headline 1")
        source.inject_event("Breaking: Test headline 2")

        # Collect events
        events = []
        async for event in source.stream():
            events.append(event)
            if len(events) >= 2:
                break

        await source.disconnect()

        assert len(events) == 2
        assert "Test headline 1" in events[0].headline


class TestNewsAggregator:
    """Tests for news aggregator."""

    @pytest.mark.asyncio
    async def test_aggregator_deduplication(self):
        config = AggregatorConfig(
            dedup_window_seconds=60,
            similarity_threshold=0.7,
        )
        aggregator = NewsAggregator(config)

        # Track emitted events
        emitted = []

        def on_news(event):
            emitted.append(event)

        aggregator.on_news(on_news)

        # Create two very similar events
        event1 = NewsEvent(
            id="dup1",
            headline="Biden announces major economic policy",
            source="twitter",
            categories=[NewsCategory.POLITICS],
        )
        event2 = NewsEvent(
            id="dup2",
            headline="Biden announces major economic policy today",
            source="rss",
            categories=[NewsCategory.POLITICS],
        )

        # Process through aggregator's dedup logic
        # Note: Need to test the _is_duplicate method directly
        assert not aggregator._is_duplicate(event1)
        assert aggregator._is_duplicate(event2)  # Should be caught as duplicate


class TestSignalDirection:
    """Tests for signal direction enum."""

    def test_signal_directions(self):
        assert SignalDirection.BUY.value == "buy"
        assert SignalDirection.SELL.value == "sell"
        assert SignalDirection.HOLD.value == "hold"


class TestNewsCategory:
    """Tests for news category enum."""

    def test_news_categories(self):
        assert NewsCategory.POLITICS.value == "politics"
        assert NewsCategory.WORLD_EVENTS.value == "world_events"
        assert NewsCategory.ECONOMICS.value == "economics"
        assert NewsCategory.CRYPTO.value == "crypto"
