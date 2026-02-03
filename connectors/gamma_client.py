"""Gamma API client for market discovery and metadata."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import httpx

from storage.models import Market, Token
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GammaClientConfig:
    """Configuration for Gamma API client."""
    base_url: str = "https://gamma-api.polymarket.com"
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 1.0


class GammaClient:
    """
    Client for Polymarket Gamma API.

    The Gamma API provides:
    - Market discovery and metadata
    - Event information
    - Token ID mappings
    """

    def __init__(self, config: Optional[GammaClientConfig] = None):
        self.config = config or GammaClientConfig()
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "GammaClient":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make an HTTP request with retry logic."""
        client = self._client or httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
        )
        should_close = self._client is None

        last_error = None
        for attempt in range(self.config.max_retries):
            try:
                response = await client.request(method, path, params=params)

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "5"))
                    logger.warning(
                        "gamma_rate_limited",
                        retry_after=retry_after,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(
                    "gamma_request_failed",
                    status_code=e.response.status_code,
                    attempt=attempt + 1,
                )
                if e.response.status_code >= 500:
                    await asyncio.sleep(self.config.retry_delay * (2 ** attempt))
                else:
                    raise

            except httpx.RequestError as e:
                last_error = e
                logger.warning(
                    "gamma_request_error",
                    error=str(e),
                    attempt=attempt + 1,
                )
                await asyncio.sleep(self.config.retry_delay * (2 ** attempt))

            finally:
                if should_close:
                    await client.aclose()

        raise last_error or Exception("Max retries exceeded")

    async def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Market]:
        """
        Get list of markets from Gamma API.

        Args:
            active: Include active markets
            closed: Include closed markets
            limit: Maximum number of markets to return
            offset: Offset for pagination

        Returns:
            List of Market objects
        """
        params = {
            "limit": limit,
            "offset": offset,
        }
        if active and not closed:
            params["active"] = "true"
            params["closed"] = "false"
        elif closed and not active:
            params["closed"] = "true"

        logger.debug("gamma_get_markets", params=params)

        data = await self._request("GET", "/markets", params=params)

        markets = []
        for item in data:
            try:
                market = self._parse_market(item)
                markets.append(market)
            except Exception as e:
                logger.warning(
                    "gamma_parse_market_failed",
                    market_id=item.get("id"),
                    error=str(e),
                )

        logger.info("gamma_markets_fetched", count=len(markets))
        return markets

    async def get_market(self, market_id: str) -> Optional[Market]:
        """Get a specific market by ID."""
        try:
            data = await self._request("GET", f"/markets/{market_id}")
            return self._parse_market(data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_events(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Get list of events from Gamma API.

        Events group related markets together.
        """
        params = {"limit": limit, "offset": offset}
        data = await self._request("GET", "/events", params=params)
        return data

    async def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific event by ID."""
        try:
            return await self._request("GET", f"/events/{event_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def search_markets(
        self,
        query: str,
        limit: int = 20,
    ) -> List[Market]:
        """Search markets by keyword."""
        # Gamma API may support different search mechanisms
        # For now, fetch all and filter client-side
        markets = await self.get_markets(limit=500)
        query_lower = query.lower()

        results = [
            m for m in markets
            if query_lower in m.title.lower() or
               query_lower in m.description.lower()
        ]

        return results[:limit]

    def _parse_market(self, data: Dict[str, Any]) -> Market:
        """Parse market data from API response."""
        import json

        # Parse tokens
        tokens = []
        clob_token_ids = data.get("clobTokenIds", [])

        # Handle case where clobTokenIds is a JSON string instead of a list
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except json.JSONDecodeError:
                clob_token_ids = []

        # Parse outcomes - could be a string like "Yes,No" or a JSON string like '["Yes","No"]'
        outcomes_raw = data.get("outcomes", "Yes,No")
        if isinstance(outcomes_raw, str):
            # Try parsing as JSON first
            try:
                outcomes = json.loads(outcomes_raw)
            except json.JSONDecodeError:
                # Fall back to comma-separated
                outcomes = outcomes_raw.split(",")
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = ["Yes", "No"]

        for i, token_id in enumerate(clob_token_ids):
            outcome = outcomes[i] if i < len(outcomes) else f"Outcome {i}"
            outcome_str = outcome.strip() if isinstance(outcome, str) else str(outcome)
            tokens.append(Token(
                token_id=token_id,
                market_id=data.get("id", ""),
                outcome=outcome_str,
                winner=None,
            ))

        # Parse dates
        start_date = None
        end_date = None
        if data.get("startDate"):
            try:
                start_date = datetime.fromisoformat(
                    data["startDate"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass
        if data.get("endDate"):
            try:
                end_date = datetime.fromisoformat(
                    data["endDate"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        return Market(
            market_id=data.get("id", ""),
            event_id=data.get("eventId", ""),
            condition_id=data.get("conditionId", ""),
            title=data.get("question", data.get("title", "")),
            slug=data.get("slug", ""),
            description=data.get("description", ""),
            active=data.get("active", True),
            closed=data.get("closed", False),
            start_date=start_date,
            end_date=end_date,
            tokens=tokens,
            category=data.get("category", ""),
            liquidity=Decimal(str(data.get("liquidity", 0))),
            volume=Decimal(str(data.get("volume", 0))),
            last_updated=datetime.utcnow(),
            question=data.get("question"),
            outcomes=data.get("outcomes"),
        )

    async def get_all_active_markets(
        self,
        batch_size: int = 100,
    ) -> List[Market]:
        """
        Fetch all active markets with pagination.

        Args:
            batch_size: Number of markets per request

        Returns:
            List of all active markets
        """
        all_markets = []
        offset = 0

        while True:
            batch = await self.get_markets(
                active=True,
                closed=False,
                limit=batch_size,
                offset=offset,
            )

            if not batch:
                break

            all_markets.extend(batch)
            offset += batch_size

            # Safety limit
            if offset > 10000:
                logger.warning("gamma_pagination_limit_reached")
                break

        logger.info("gamma_all_active_markets_fetched", total=len(all_markets))
        return all_markets


async def fetch_active_markets(
    base_url: str = "https://gamma-api.polymarket.com"
) -> List[Market]:
    """Convenience function to fetch all active markets."""
    config = GammaClientConfig(base_url=base_url)
    async with GammaClient(config) as client:
        return await client.get_all_active_markets()
