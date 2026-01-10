"""LLM-based news analysis and trade signal generation."""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

from connectors.news_sources.base import NewsEvent
from monitoring.logger import get_logger

logger = get_logger(__name__)


class SignalDirection(str, Enum):
    """Direction of a trade signal."""
    BUY = "buy"       # Price should go UP
    SELL = "sell"     # Price should go DOWN
    HOLD = "hold"     # No action


@dataclass
class TradeSignal:
    """
    A trade signal generated from news analysis.

    Represents the LLM's recommendation for a specific market.
    """
    market_id: str                     # Polymarket condition ID
    token_id: str                      # Specific token (YES/NO)
    direction: SignalDirection
    confidence: float                  # 0-100%
    fair_value_estimate: Optional[Decimal] = None  # LLM's price estimate
    current_price: Optional[Decimal] = None        # Price at signal time
    edge: Optional[float] = None       # Perceived edge (fair - current)
    reasoning: str = ""                # One-line explanation
    news_event_id: str = ""            # Source news event
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Position sizing hints
    suggested_size_pct: float = 1.0    # 0-100% of normal position size
    urgency: str = "normal"            # "immediate", "normal", "low"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "token_id": self.token_id,
            "direction": self.direction.value,
            "confidence": self.confidence,
            "fair_value_estimate": str(self.fair_value_estimate) if self.fair_value_estimate else None,
            "current_price": str(self.current_price) if self.current_price else None,
            "edge": self.edge,
            "reasoning": self.reasoning,
            "news_event_id": self.news_event_id,
            "timestamp": self.timestamp.isoformat(),
            "suggested_size_pct": self.suggested_size_pct,
            "urgency": self.urgency,
        }


@dataclass
class AnalysisResult:
    """Result of analyzing a news event."""
    news_event: NewsEvent
    signals: List[TradeSignal]
    affected_markets: List[str]        # Market IDs potentially affected
    analysis_time_ms: float
    model_used: str
    raw_response: Optional[str] = None

    @property
    def has_actionable_signals(self) -> bool:
        return any(s.direction != SignalDirection.HOLD for s in self.signals)


@dataclass
class LLMConfig:
    """Configuration for LLM analyzer."""
    api_key: str
    model: str = "claude-sonnet-4-20250514"
    base_url: str = "https://api.anthropic.com/v1"
    max_tokens: int = 2048
    temperature: float = 0.3           # Lower = more deterministic
    timeout_seconds: float = 30.0

    # Thresholds
    min_confidence_to_signal: float = 60.0
    min_edge_to_signal: float = 0.03   # 3 cents minimum edge

    # Rate limiting
    max_requests_per_minute: int = 20
    cooldown_between_requests_ms: int = 100


# System prompt for news analysis
NEWS_ANALYSIS_PROMPT = """You are a quantitative trading analyst specializing in prediction markets.

Your task is to analyze breaking news and determine how it affects Polymarket prediction markets.

IMPORTANT GUIDELINES:
1. Be CONSERVATIVE with confidence scores. Only high-confidence (75%+) signals should be acted on.
2. Consider both DIRECT and INDIRECT effects (e.g., Venezuela → oil prices → inflation → Fed policy)
3. Account for the fact that markets may have ALREADY priced in some of this information
4. Consider the TIMING - is this actionable now, or will it take time to play out?
5. Be aware of FAKE NEWS and verify the source reliability if mentioned

For each affected market, provide:
- Direction: "buy" (YES becomes more likely), "sell" (NO becomes more likely), or "hold"
- Confidence: 0-100 (be conservative, most news is noise)
- Fair value estimate: Your estimate of the true probability (0.01 to 0.99)
- Reasoning: One sentence explanation
- Urgency: "immediate" (trade now), "normal" (within minutes), "low" (hours)

Respond in valid JSON format only. No markdown, no explanation outside JSON."""


class LLMAnalyzer:
    """
    LLM-based news analyzer using Claude API.

    Analyzes news events and generates trade signals for relevant markets.
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._request_count = 0
        self._last_request_time: Optional[datetime] = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()

    async def connect(self) -> None:
        """Initialize HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            headers={
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=self.config.timeout_seconds,
        )

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def analyze(
        self,
        news_event: NewsEvent,
        active_markets: List[Dict[str, Any]],
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> AnalysisResult:
        """
        Analyze a news event for trading signals.

        Args:
            news_event: The news to analyze
            active_markets: List of active Polymarket markets with metadata
            current_prices: Current prices by token ID (optional)

        Returns:
            AnalysisResult with any trade signals
        """
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")

        start_time = datetime.utcnow()

        # Build the analysis prompt
        user_prompt = self._build_prompt(news_event, active_markets, current_prices)

        try:
            # Call Claude API
            response = await self._call_claude(user_prompt)

            # Parse the response
            signals = self._parse_response(response, news_event, current_prices)

            # Filter by confidence threshold
            signals = [
                s for s in signals
                if s.confidence >= self.config.min_confidence_to_signal
            ]

            elapsed_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            logger.info(
                "llm_analysis_complete",
                news_id=news_event.id,
                signals_generated=len(signals),
                elapsed_ms=elapsed_ms,
            )

            return AnalysisResult(
                news_event=news_event,
                signals=signals,
                affected_markets=[s.market_id for s in signals],
                analysis_time_ms=elapsed_ms,
                model_used=self.config.model,
                raw_response=response,
            )

        except Exception as e:
            logger.error("llm_analysis_error", error=str(e), news_id=news_event.id)
            elapsed_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            return AnalysisResult(
                news_event=news_event,
                signals=[],
                affected_markets=[],
                analysis_time_ms=elapsed_ms,
                model_used=self.config.model,
            )

    def _build_prompt(
        self,
        news_event: NewsEvent,
        active_markets: List[Dict[str, Any]],
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> str:
        """Build the user prompt for analysis."""
        # Format markets for the prompt
        markets_text = self._format_markets(active_markets, current_prices)

        prompt = f"""NEWS EVENT:
Source: {news_event.source}
Time: {news_event.timestamp.isoformat()}
Verified: {news_event.is_verified}

{news_event.full_text()}

---

ACTIVE MARKETS TO CONSIDER:
{markets_text}

---

Analyze how this news affects these markets. Return JSON with this structure:
{{
  "affected_markets": [
    {{
      "market_id": "condition_id_here",
      "token_id": "token_id_for_yes_or_no",
      "direction": "buy" | "sell" | "hold",
      "confidence": 0-100,
      "fair_value": 0.01-0.99,
      "reasoning": "one sentence",
      "urgency": "immediate" | "normal" | "low"
    }}
  ],
  "overall_assessment": "one paragraph summary"
}}

If no markets are meaningfully affected, return {{"affected_markets": [], "overall_assessment": "No actionable impact"}}"""

        return prompt

    def _format_markets(
        self,
        markets: List[Dict[str, Any]],
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> str:
        """Format markets for the prompt."""
        lines = []

        for i, market in enumerate(markets[:30], 1):  # Limit to 30 markets
            market_id = market.get("condition_id", market.get("id", "unknown"))
            question = market.get("question", market.get("title", "Unknown"))
            description = market.get("description", "")[:200]

            # Get tokens
            tokens = market.get("tokens", [])
            token_info = []
            for token in tokens:
                token_id = token.get("token_id", "")
                outcome = token.get("outcome", "YES/NO")
                price = current_prices.get(token_id) if current_prices else None
                price_str = f"${price:.2f}" if price else "?"
                token_info.append(f"{outcome}({token_id[:8]}...): {price_str}")

            lines.append(f"{i}. [{market_id[:12]}...] {question}")
            if description:
                lines.append(f"   Description: {description}")
            if token_info:
                lines.append(f"   Tokens: {', '.join(token_info)}")
            lines.append("")

        return "\n".join(lines)

    async def _call_claude(self, user_prompt: str) -> str:
        """Make API call to Claude."""
        if not self._client:
            raise RuntimeError("Client not initialized")

        payload = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "system": NEWS_ANALYSIS_PROMPT,
            "messages": [
                {"role": "user", "content": user_prompt}
            ],
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
        news_event: NewsEvent,
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> List[TradeSignal]:
        """Parse LLM response into trade signals."""
        signals = []

        try:
            # Try to extract JSON from response
            json_match = re.search(r'\{[\s\S]*\}', response)
            if not json_match:
                logger.warning("llm_no_json_in_response", response_preview=response[:200])
                return []

            data = json.loads(json_match.group())
            affected = data.get("affected_markets", [])

            for item in affected:
                try:
                    direction_str = item.get("direction", "hold").lower()
                    if direction_str == "buy":
                        direction = SignalDirection.BUY
                    elif direction_str == "sell":
                        direction = SignalDirection.SELL
                    else:
                        direction = SignalDirection.HOLD

                    # Skip hold signals
                    if direction == SignalDirection.HOLD:
                        continue

                    fair_value = item.get("fair_value")
                    if fair_value is not None:
                        fair_value = Decimal(str(fair_value))

                    token_id = item.get("token_id", "")
                    current_price = current_prices.get(token_id) if current_prices else None

                    # Calculate edge
                    edge = None
                    if fair_value and current_price:
                        if direction == SignalDirection.BUY:
                            edge = float(fair_value - current_price)
                        else:
                            edge = float(current_price - fair_value)

                    # Skip if edge is below threshold
                    if edge is not None and edge < self.config.min_edge_to_signal:
                        continue

                    confidence = float(item.get("confidence", 0))

                    # Size based on confidence
                    if confidence >= 90:
                        size_pct = 100
                    elif confidence >= 80:
                        size_pct = 75
                    elif confidence >= 70:
                        size_pct = 50
                    else:
                        size_pct = 25

                    signal = TradeSignal(
                        market_id=item.get("market_id", ""),
                        token_id=token_id,
                        direction=direction,
                        confidence=confidence,
                        fair_value_estimate=fair_value,
                        current_price=current_price,
                        edge=edge,
                        reasoning=item.get("reasoning", ""),
                        news_event_id=news_event.id,
                        timestamp=datetime.utcnow(),
                        suggested_size_pct=size_pct,
                        urgency=item.get("urgency", "normal"),
                    )
                    signals.append(signal)

                except Exception as e:
                    logger.warning("llm_signal_parse_error", error=str(e), item=item)

        except json.JSONDecodeError as e:
            logger.warning("llm_json_parse_error", error=str(e), response_preview=response[:200])

        return signals


class MockLLMAnalyzer:
    """Mock LLM analyzer for testing without API calls."""

    def __init__(self):
        self._mock_signals: List[TradeSignal] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def set_mock_signals(self, signals: List[TradeSignal]) -> None:
        """Set signals to return for next analysis."""
        self._mock_signals = signals

    async def analyze(
        self,
        news_event: NewsEvent,
        active_markets: List[Dict[str, Any]],
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> AnalysisResult:
        """Return mock signals."""
        return AnalysisResult(
            news_event=news_event,
            signals=self._mock_signals.copy(),
            affected_markets=[s.market_id for s in self._mock_signals],
            analysis_time_ms=50.0,
            model_used="mock",
        )
