"""Market mapper for matching news to relevant Polymarket markets."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from connectors.news_sources.base import NewsEvent, NewsCategory


@dataclass
class MarketMatch:
    """A potential match between news and a market."""
    market: Dict[str, Any]
    relevance_score: float      # 0-1, higher = more relevant
    match_reasons: List[str]    # Why this market matched
    category_match: bool        # Category alignment


@dataclass
class MapperConfig:
    """Configuration for market mapper."""
    # Matching thresholds
    min_relevance_score: float = 0.3
    max_markets_to_return: int = 10

    # Keyword weights
    exact_match_weight: float = 1.0
    partial_match_weight: float = 0.5
    category_match_weight: float = 0.3

    # Category mappings
    category_keywords: Dict[str, List[str]] = field(default_factory=lambda: {
        "politics": [
            "president", "congress", "senate", "election", "vote", "democrat",
            "republican", "biden", "trump", "governor", "mayor", "legislation",
            "supreme court", "executive order", "white house", "capitol",
        ],
        "world_events": [
            "war", "military", "troops", "nato", "invasion", "sanctions",
            "treaty", "united nations", "embassy", "diplomat", "summit",
            "crisis", "conflict", "peace", "ceasefire",
        ],
        "economics": [
            "fed", "federal reserve", "interest rate", "inflation", "gdp",
            "unemployment", "jobs", "recession", "tariff", "trade", "stock",
            "market", "dow", "s&p", "nasdaq", "bitcoin", "crypto",
        ],
        "crypto": [
            "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain",
            "defi", "nft", "sec", "binance", "coinbase", "altcoin",
        ],
    })


class MarketMapper:
    """
    Maps news events to relevant Polymarket markets.

    Uses keyword matching, category alignment, and entity extraction
    to find markets that might be affected by news.
    """

    def __init__(self, config: Optional[MapperConfig] = None):
        self.config = config or MapperConfig()
        self._markets: List[Dict[str, Any]] = []
        self._market_keywords: Dict[str, Set[str]] = {}  # market_id -> keywords
        self._entity_to_markets: Dict[str, List[str]] = {}  # entity -> market_ids

    def update_markets(self, markets: List[Dict[str, Any]]) -> None:
        """
        Update the list of active markets.

        Call this periodically as markets change.
        """
        self._markets = markets
        self._build_index()

    def _build_index(self) -> None:
        """Build search index for markets."""
        self._market_keywords.clear()
        self._entity_to_markets.clear()

        for market in self._markets:
            market_id = market.get("condition_id", market.get("id", ""))

            # Extract keywords from question and description
            question = market.get("question", market.get("title", ""))
            description = market.get("description", "")

            keywords = self._extract_keywords(f"{question} {description}")
            self._market_keywords[market_id] = keywords

            # Extract entities (people, places, organizations)
            entities = self._extract_entities(question)
            for entity in entities:
                entity_lower = entity.lower()
                if entity_lower not in self._entity_to_markets:
                    self._entity_to_markets[entity_lower] = []
                self._entity_to_markets[entity_lower].append(market_id)

    def _extract_keywords(self, text: str) -> Set[str]:
        """Extract searchable keywords from text."""
        text = text.lower()

        # Remove punctuation
        text = re.sub(r'[^\w\s]', ' ', text)

        # Split into words
        words = text.split()

        # Filter stopwords and short words
        stopwords = {
            "the", "a", "an", "is", "are", "was", "were", "will", "be",
            "in", "on", "at", "to", "for", "of", "by", "with", "from",
            "and", "or", "if", "then", "that", "this", "it", "as",
            "yes", "no", "what", "when", "where", "who", "how", "why",
        }

        keywords = {w for w in words if len(w) > 2 and w not in stopwords}

        return keywords

    def _extract_entities(self, text: str) -> List[str]:
        """Extract named entities (simple heuristic approach)."""
        entities = []

        # Find capitalized words/phrases (likely proper nouns)
        # Match sequences of capitalized words
        pattern = r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b'
        matches = re.findall(pattern, text)

        for match in matches:
            # Filter out common non-entities
            if match.lower() not in {"yes", "no", "the", "will", "what", "when"}:
                entities.append(match)

        return entities

    def find_relevant_markets(
        self,
        news_event: NewsEvent,
        top_n: Optional[int] = None,
    ) -> List[MarketMatch]:
        """
        Find markets relevant to a news event.

        Args:
            news_event: The news to match
            top_n: Maximum number of markets to return (default from config)

        Returns:
            List of MarketMatch objects, sorted by relevance
        """
        if not self._markets:
            return []

        top_n = top_n or self.config.max_markets_to_return
        matches: List[Tuple[float, MarketMatch]] = []

        # Extract news keywords
        news_text = news_event.full_text()
        news_keywords = self._extract_keywords(news_text)
        news_entities = set(e.lower() for e in self._extract_entities(news_text))

        # Also use any pre-extracted keywords/entities
        news_keywords.update(kw.lower() for kw in news_event.keywords)
        news_entities.update(e.lower().lstrip('@#') for e in news_event.entities)

        for market in self._markets:
            market_id = market.get("condition_id", market.get("id", ""))
            market_keywords = self._market_keywords.get(market_id, set())

            # Calculate relevance score
            score, reasons = self._calculate_relevance(
                news_keywords=news_keywords,
                news_entities=news_entities,
                news_categories=news_event.categories,
                market=market,
                market_keywords=market_keywords,
            )

            if score >= self.config.min_relevance_score:
                match = MarketMatch(
                    market=market,
                    relevance_score=score,
                    match_reasons=reasons,
                    category_match=bool(news_event.categories),
                )
                matches.append((score, match))

        # Sort by score descending
        matches.sort(key=lambda x: x[0], reverse=True)

        return [m for _, m in matches[:top_n]]

    def _calculate_relevance(
        self,
        news_keywords: Set[str],
        news_entities: Set[str],
        news_categories: List[NewsCategory],
        market: Dict[str, Any],
        market_keywords: Set[str],
    ) -> Tuple[float, List[str]]:
        """Calculate relevance score between news and market."""
        score = 0.0
        reasons = []

        # Keyword overlap
        keyword_overlap = news_keywords & market_keywords
        if keyword_overlap:
            overlap_score = len(keyword_overlap) / max(len(market_keywords), 1)
            score += overlap_score * self.config.exact_match_weight
            reasons.append(f"keywords: {', '.join(list(keyword_overlap)[:5])}")

        # Entity match (higher weight)
        question = market.get("question", market.get("title", "")).lower()
        entity_matches = [e for e in news_entities if e in question]
        if entity_matches:
            score += 0.5 * len(entity_matches)
            reasons.append(f"entities: {', '.join(entity_matches[:3])}")

        # Category alignment
        market_tags = market.get("tags", [])
        market_category = market.get("category", "")

        for news_cat in news_categories:
            cat_keywords = self.config.category_keywords.get(news_cat.value, [])

            # Check if market tags align with news category
            if any(tag.lower() in cat_keywords for tag in market_tags):
                score += self.config.category_match_weight
                reasons.append(f"category: {news_cat.value}")
                break

            # Check market category
            if market_category.lower() == news_cat.value:
                score += self.config.category_match_weight
                reasons.append(f"category: {news_cat.value}")
                break

        # Normalize score to 0-1 range
        score = min(score, 1.0)

        return score, reasons

    def get_markets_by_entity(self, entity: str) -> List[Dict[str, Any]]:
        """Get all markets mentioning a specific entity."""
        entity_lower = entity.lower()
        market_ids = self._entity_to_markets.get(entity_lower, [])

        return [
            m for m in self._markets
            if m.get("condition_id", m.get("id", "")) in market_ids
        ]

    def get_markets_by_category(self, category: NewsCategory) -> List[Dict[str, Any]]:
        """Get all markets in a category."""
        cat_keywords = self.config.category_keywords.get(category.value, [])

        matching = []
        for market in self._markets:
            question = market.get("question", market.get("title", "")).lower()
            if any(kw in question for kw in cat_keywords):
                matching.append(market)

        return matching

    @property
    def market_count(self) -> int:
        return len(self._markets)
