"""Cross-market analyzer for finding logically related markets.

When news hits, this module uses LLM reasoning to:
1. Find all markets affected by the same news event
2. Identify logical dependencies between markets
3. Detect markets where prices may lag (second-order effects)

This is inspired by the research showing $40M extracted via cross-market arbitrage,
but focused on enhancing our news-driven edge rather than pure arbitrage.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from connectors.news_sources.base import NewsEvent
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RelatedMarket:
    """A market related to the primary news-affected market."""
    market_id: str
    token_id: str
    title: str
    relationship: str          # "direct", "correlated", "second_order", "inverse"
    dependency_type: str       # "implies", "implied_by", "mutual", "inverse"
    confidence: float          # 0-100
    expected_direction: str    # "same", "opposite", "uncertain"
    lag_estimate_seconds: int  # Expected time for price to adjust
    reasoning: str


@dataclass
class MarketDependency:
    """Logical dependency between two markets.

    Example: If "Trump wins PA" is true, then "GOP wins PA by 5+" could be true,
    but if "GOP wins PA by 5+" is true, "Trump wins PA" MUST be true.
    """
    market_a_id: str
    market_b_id: str
    dependency_type: str       # "a_implies_b", "b_implies_a", "mutual", "exclusive"
    confidence: float
    reasoning: str


@dataclass
class CrossMarketAnalysis:
    """Result of cross-market analysis for a news event."""
    primary_market_id: str
    related_markets: List[RelatedMarket]
    dependencies: List[MarketDependency]
    trading_opportunities: List[Dict[str, Any]]  # Prioritized list of trades
    analysis_time_ms: float
    model_used: str


CROSS_MARKET_PROMPT = """You are a quantitative analyst specializing in prediction market correlations.

Your task is to identify ALL markets that could be affected by a news event, including:
1. DIRECT effects (market explicitly mentioned in news)
2. CORRELATED effects (markets that typically move together)
3. SECOND-ORDER effects (downstream consequences that take time to play out)
4. INVERSE effects (markets that move in opposite direction)

IMPORTANT:
- Consider LOGICAL DEPENDENCIES: If A implies B, and A becomes more likely, B must also become more likely
- Consider TIME LAGS: Second-order effects take time - these are trading opportunities
- Consider ALREADY PRICED IN: Some correlated markets may have already moved
- Be specific about the DIRECTION and CONFIDENCE for each related market

For each related market, specify:
- relationship: "direct", "correlated", "second_order", or "inverse"
- dependency_type: "implies" (if primary→this), "implied_by" (if this→primary), "mutual", or "independent"
- expected_direction: "same" (move with primary), "opposite" (move against), or "uncertain"
- lag_estimate_seconds: How long before this market fully prices in the news (0 for immediate)
- confidence: 0-100 how confident you are in this relationship

Respond in JSON format only."""


class CrossMarketAnalyzer:
    """
    Analyzes cross-market relationships and dependencies.

    Uses LLM reasoning to identify:
    1. Markets that should move together based on logical dependencies
    2. Markets where price updates may lag (trading opportunities)
    3. Potential arbitrage conditions (for defensive checks)
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        base_url: str = "https://api.anthropic.com/v1",
        timeout_seconds: float = 45.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None

    async def connect(self) -> None:
        """Initialize HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=self.timeout_seconds,
        )

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def find_related_markets(
        self,
        news_event: NewsEvent,
        primary_market: Dict[str, Any],
        all_markets: List[Dict[str, Any]],
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> CrossMarketAnalysis:
        """
        Find all markets related to the primary market affected by news.

        Args:
            news_event: The triggering news event
            primary_market: The directly affected market
            all_markets: All active markets to search
            current_prices: Current prices by token ID

        Returns:
            CrossMarketAnalysis with related markets and trading opportunities
        """
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")

        start_time = datetime.utcnow()
        primary_id = primary_market.get("condition_id", primary_market.get("id", ""))

        # Filter to relevant subset (same category, related entities, etc.)
        candidate_markets = self._filter_candidates(primary_market, all_markets)

        if not candidate_markets:
            return CrossMarketAnalysis(
                primary_market_id=primary_id,
                related_markets=[],
                dependencies=[],
                trading_opportunities=[],
                analysis_time_ms=0,
                model_used=self.model,
            )

        # Build prompt
        prompt = self._build_prompt(news_event, primary_market, candidate_markets, current_prices)

        try:
            response = await self._call_llm(prompt)
            related_markets, dependencies = self._parse_response(response, primary_id, candidate_markets)

            # Prioritize trading opportunities
            opportunities = self._prioritize_opportunities(
                related_markets, current_prices, primary_market
            )

            elapsed_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            logger.info(
                "cross_market_analysis_complete",
                primary_market=primary_id,
                related_count=len(related_markets),
                opportunities=len(opportunities),
                elapsed_ms=elapsed_ms,
            )

            return CrossMarketAnalysis(
                primary_market_id=primary_id,
                related_markets=related_markets,
                dependencies=dependencies,
                trading_opportunities=opportunities,
                analysis_time_ms=elapsed_ms,
                model_used=self.model,
            )

        except Exception as e:
            logger.error("cross_market_analysis_error", error=str(e))
            elapsed_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            return CrossMarketAnalysis(
                primary_market_id=primary_id,
                related_markets=[],
                dependencies=[],
                trading_opportunities=[],
                analysis_time_ms=elapsed_ms,
                model_used=self.model,
            )

    def _filter_candidates(
        self,
        primary_market: Dict[str, Any],
        all_markets: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Filter markets to relevant candidates for cross-market analysis."""
        primary_id = primary_market.get("condition_id", primary_market.get("id", ""))
        primary_title = primary_market.get("question", primary_market.get("title", "")).lower()
        primary_category = primary_market.get("category", "").lower()

        # Extract key entities from primary market
        primary_entities = self._extract_entities(primary_title)

        candidates = []
        for market in all_markets:
            market_id = market.get("condition_id", market.get("id", ""))
            if market_id == primary_id:
                continue

            title = market.get("question", market.get("title", "")).lower()
            category = market.get("category", "").lower()

            # Check for relevance
            score = 0

            # Same category
            if category == primary_category:
                score += 1

            # Shared entities
            market_entities = self._extract_entities(title)
            shared = primary_entities & market_entities
            if shared:
                score += len(shared) * 2

            # Related keywords
            related_keywords = self._get_related_keywords(primary_title)
            if any(kw in title for kw in related_keywords):
                score += 1

            if score > 0:
                candidates.append(market)

        # Limit to top 50 candidates to keep prompt manageable
        return candidates[:50]

    def _extract_entities(self, text: str) -> Set[str]:
        """Extract named entities from text."""
        # Simple extraction of capitalized words and key terms
        words = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text)
        entities = {w.lower() for w in words if len(w) > 2}

        # Also extract key political/economic terms
        key_terms = ["trump", "biden", "fed", "inflation", "election", "recession"]
        for term in key_terms:
            if term in text.lower():
                entities.add(term)

        return entities

    def _get_related_keywords(self, title: str) -> List[str]:
        """Get keywords related to the market title."""
        keywords = []

        # Political correlations
        if any(term in title for term in ["trump", "republican", "gop"]):
            keywords.extend(["republican", "conservative", "red state"])
        if any(term in title for term in ["biden", "democrat", "democratic"]):
            keywords.extend(["democrat", "liberal", "blue state"])

        # Economic correlations
        if any(term in title for term in ["fed", "interest rate", "inflation"]):
            keywords.extend(["recession", "gdp", "unemployment", "stock", "bond"])
        if "recession" in title:
            keywords.extend(["unemployment", "gdp", "fed"])

        # Geographic correlations
        states = ["pennsylvania", "georgia", "michigan", "wisconsin", "arizona", "nevada"]
        for state in states:
            if state in title:
                keywords.extend(["electoral", "swing state", "election"])
                break

        return keywords

    def _build_prompt(
        self,
        news_event: NewsEvent,
        primary_market: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> str:
        """Build the LLM prompt for cross-market analysis."""
        primary_title = primary_market.get("question", primary_market.get("title", ""))
        primary_id = primary_market.get("condition_id", primary_market.get("id", ""))

        # Get primary market price
        primary_price = "unknown"
        tokens = primary_market.get("tokens", [])
        for token in tokens:
            tid = token.get("token_id", "")
            if current_prices and tid in current_prices:
                primary_price = f"${current_prices[tid]:.2f}"
                break

        # Format candidate markets
        candidates_text = []
        for i, market in enumerate(candidates[:30], 1):
            mid = market.get("condition_id", market.get("id", ""))
            title = market.get("question", market.get("title", ""))

            # Get price if available
            price = "?"
            mtokens = market.get("tokens", [])
            for token in mtokens:
                tid = token.get("token_id", "")
                if current_prices and tid in current_prices:
                    price = f"${current_prices[tid]:.2f}"
                    break

            candidates_text.append(f"{i}. [{mid[:12]}] {title} (YES: {price})")

        prompt = f"""NEWS EVENT:
{news_event.headline}
{news_event.content[:500] if news_event.content else ""}

PRIMARY AFFECTED MARKET:
ID: {primary_id}
Question: {primary_title}
Current YES price: {primary_price}

CANDIDATE RELATED MARKETS:
{chr(10).join(candidates_text)}

---

Analyze which of these markets are logically related to the primary market given this news.
For each related market, identify:
1. The type of relationship
2. Whether there's a logical dependency (if A then B, etc.)
3. Expected price direction relative to primary market
4. How long before this market fully prices in the news

Return JSON:
{{
  "related_markets": [
    {{
      "market_id": "...",
      "relationship": "direct|correlated|second_order|inverse",
      "dependency_type": "implies|implied_by|mutual|independent",
      "expected_direction": "same|opposite|uncertain",
      "lag_estimate_seconds": 0-3600,
      "confidence": 0-100,
      "reasoning": "one sentence"
    }}
  ],
  "dependencies": [
    {{
      "market_a_id": "primary_market_id",
      "market_b_id": "related_market_id",
      "dependency_type": "a_implies_b|b_implies_a|mutual|exclusive",
      "confidence": 0-100,
      "reasoning": "one sentence"
    }}
  ]
}}

Only include markets with confidence >= 50. Focus on actionable relationships."""

        return prompt

    async def _call_llm(self, prompt: str) -> str:
        """Make API call to Claude."""
        if not self._client:
            raise RuntimeError("Client not initialized")

        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "temperature": 0.2,  # Lower temperature for more consistent analysis
            "system": CROSS_MARKET_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }

        response = await self._client.post("/messages", json=payload)
        response.raise_for_status()

        data = response.json()
        content = data.get("content", [])

        if content and content[0].get("type") == "text":
            return content[0].get("text", "")
        return ""

    def _parse_response(
        self,
        response: str,
        primary_id: str,
        candidates: List[Dict[str, Any]],
    ) -> Tuple[List[RelatedMarket], List[MarketDependency]]:
        """Parse LLM response into structured data."""
        related_markets = []
        dependencies = []

        # Build market lookup
        market_lookup = {}
        for m in candidates:
            mid = m.get("condition_id", m.get("id", ""))
            market_lookup[mid] = m
            # Also index by partial ID
            if len(mid) >= 12:
                market_lookup[mid[:12]] = m

        try:
            # Extract JSON from response
            json_match = re.search(r'\{[\s\S]*\}', response)
            if not json_match:
                logger.warning("cross_market_no_json", response_preview=response[:200])
                return [], []

            data = json.loads(json_match.group())

            # Parse related markets
            for item in data.get("related_markets", []):
                try:
                    market_id = item.get("market_id", "")

                    # Find full market info
                    market = market_lookup.get(market_id)
                    if not market:
                        # Try partial match
                        for key in market_lookup:
                            if key.startswith(market_id) or market_id.startswith(key):
                                market = market_lookup[key]
                                market_id = market.get("condition_id", market.get("id", ""))
                                break

                    if not market:
                        continue

                    title = market.get("question", market.get("title", ""))
                    tokens = market.get("tokens", [])
                    token_id = tokens[0].get("token_id", "") if tokens else ""

                    related = RelatedMarket(
                        market_id=market_id,
                        token_id=token_id,
                        title=title,
                        relationship=item.get("relationship", "correlated"),
                        dependency_type=item.get("dependency_type", "independent"),
                        confidence=float(item.get("confidence", 50)),
                        expected_direction=item.get("expected_direction", "uncertain"),
                        lag_estimate_seconds=int(item.get("lag_estimate_seconds", 0)),
                        reasoning=item.get("reasoning", ""),
                    )
                    related_markets.append(related)

                except Exception as e:
                    logger.warning("cross_market_parse_item_error", error=str(e))

            # Parse dependencies
            for item in data.get("dependencies", []):
                try:
                    dep = MarketDependency(
                        market_a_id=item.get("market_a_id", primary_id),
                        market_b_id=item.get("market_b_id", ""),
                        dependency_type=item.get("dependency_type", "independent"),
                        confidence=float(item.get("confidence", 50)),
                        reasoning=item.get("reasoning", ""),
                    )
                    dependencies.append(dep)
                except Exception as e:
                    logger.warning("cross_market_parse_dep_error", error=str(e))

        except json.JSONDecodeError as e:
            logger.warning("cross_market_json_error", error=str(e))

        return related_markets, dependencies

    def _prioritize_opportunities(
        self,
        related_markets: List[RelatedMarket],
        current_prices: Optional[Dict[str, Decimal]],
        primary_market: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Prioritize trading opportunities based on lag and confidence."""
        opportunities = []

        for rm in related_markets:
            # Skip low confidence
            if rm.confidence < 60:
                continue

            # Calculate opportunity score
            # Higher score = better opportunity
            score = 0

            # Lagging markets are opportunities (prices haven't adjusted yet)
            if rm.lag_estimate_seconds > 0:
                # More lag = more opportunity (up to a point)
                lag_score = min(rm.lag_estimate_seconds / 60, 10)  # Max 10 points for 10+ min lag
                score += lag_score

            # Higher confidence = better
            score += rm.confidence / 20  # Max 5 points

            # Direct relationships are clearer
            if rm.relationship == "direct":
                score += 3
            elif rm.relationship == "correlated":
                score += 2
            elif rm.relationship == "second_order":
                score += 1  # Second order is speculative but valuable

            # Dependencies are stronger signals
            if rm.dependency_type in ("implies", "implied_by"):
                score += 2

            opportunity = {
                "market_id": rm.market_id,
                "token_id": rm.token_id,
                "title": rm.title,
                "score": score,
                "relationship": rm.relationship,
                "expected_direction": rm.expected_direction,
                "lag_seconds": rm.lag_estimate_seconds,
                "confidence": rm.confidence,
                "reasoning": rm.reasoning,
            }
            opportunities.append(opportunity)

        # Sort by score descending
        opportunities.sort(key=lambda x: x["score"], reverse=True)

        return opportunities


def find_simple_dependencies(
    markets: List[Dict[str, Any]],
) -> List[Tuple[str, str, str]]:
    """
    Find simple logical dependencies between markets without LLM.

    Returns list of (market_a_id, market_b_id, dependency_type) tuples.

    This is a fast heuristic check for obvious dependencies.
    """
    dependencies = []

    # Build title index
    market_titles = {}
    for m in markets:
        mid = m.get("condition_id", m.get("id", ""))
        title = m.get("question", m.get("title", "")).lower()
        market_titles[mid] = title

    # Check for obvious dependencies
    for mid_a, title_a in market_titles.items():
        for mid_b, title_b in market_titles.items():
            if mid_a >= mid_b:  # Avoid duplicates and self-comparison
                continue

            # Check for subset relationships
            # E.g., "Trump wins by 5+" implies "Trump wins"
            if _title_implies(title_a, title_b):
                dependencies.append((mid_a, mid_b, "a_implies_b"))
            elif _title_implies(title_b, title_a):
                dependencies.append((mid_a, mid_b, "b_implies_a"))

    return dependencies


def _title_implies(title_a: str, title_b: str) -> bool:
    """Check if title_a logically implies title_b."""
    # Simple heuristic: if A is more specific than B
    # E.g., "wins by 5+" implies "wins"

    # Check for "by X" patterns (margin of victory)
    margin_pattern = r'by \d+'
    if re.search(margin_pattern, title_a) and not re.search(margin_pattern, title_b):
        # A has margin, B doesn't - check if same subject
        base_a = re.sub(margin_pattern, '', title_a).strip()
        if base_a in title_b or title_b in base_a:
            return True

    # Check for "before X date" patterns
    date_pattern = r'before|by (january|february|march|april|may|june|july|august|september|october|november|december)'
    if re.search(date_pattern, title_a) and not re.search(date_pattern, title_b):
        base_a = re.sub(date_pattern, '', title_a).strip()
        if base_a in title_b:
            return True

    return False
