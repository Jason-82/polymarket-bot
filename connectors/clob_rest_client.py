"""CLOB REST API client for orderbook and trading operations."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import httpx

from storage.models import OrderBook, OrderBookLevel, Order, Fill, OrderSide, TradeMode
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ClobClientConfig:
    """Configuration for CLOB REST client."""
    base_url: str = "https://clob.polymarket.com"
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 1.0


class ClobRestClient:
    """
    Client for Polymarket CLOB REST API.

    The CLOB API provides:
    - Orderbook snapshots
    - Order placement/cancellation (LIVE mode only)
    - Price history
    - Trade data

    NOTE: This is a read-only wrapper. For order placement,
    use the py-clob-client SDK which handles signing.
    """

    def __init__(
        self,
        config: Optional[ClobClientConfig] = None,
    ):
        self.config = config or ClobClientConfig()
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "ClobRestClient":
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
        json: Optional[Dict[str, Any]] = None,
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
                response = await client.request(
                    method, path, params=params, json=json
                )

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "5"))
                    logger.warning(
                        "clob_rate_limited",
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
                    "clob_request_failed",
                    status_code=e.response.status_code,
                    path=path,
                    attempt=attempt + 1,
                )
                if e.response.status_code >= 500:
                    await asyncio.sleep(self.config.retry_delay * (2 ** attempt))
                else:
                    raise

            except httpx.RequestError as e:
                last_error = e
                logger.warning(
                    "clob_request_error",
                    error=str(e),
                    path=path,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(self.config.retry_delay * (2 ** attempt))

            finally:
                if should_close:
                    await client.aclose()

        raise last_error or Exception("Max retries exceeded")

    async def get_orderbook(
        self,
        token_id: str,
        depth: int = 10,
    ) -> OrderBook:
        """
        Get orderbook snapshot for a token.

        Args:
            token_id: Token ID to get orderbook for
            depth: Number of price levels to fetch

        Returns:
            OrderBook snapshot
        """
        logger.debug("clob_get_orderbook", token_id=token_id, depth=depth)

        data = await self._request(
            "GET",
            f"/book",
            params={"token_id": token_id},
        )

        return self._parse_orderbook(token_id, data)

    async def get_orderbooks(
        self,
        token_ids: List[str],
    ) -> Dict[str, OrderBook]:
        """
        Get orderbook snapshots for multiple tokens.

        Args:
            token_ids: List of token IDs

        Returns:
            Dict mapping token_id to OrderBook
        """
        # Fetch in parallel
        tasks = [self.get_orderbook(tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        orderbooks = {}
        for token_id, result in zip(token_ids, results):
            if isinstance(result, Exception):
                logger.warning(
                    "clob_orderbook_fetch_failed",
                    token_id=token_id,
                    error=str(result),
                )
            else:
                orderbooks[token_id] = result

        return orderbooks

    async def get_midpoint(self, token_id: str) -> Optional[Decimal]:
        """Get midpoint price for a token."""
        try:
            data = await self._request(
                "GET",
                f"/midpoint",
                params={"token_id": token_id},
            )
            return Decimal(str(data.get("mid", 0)))
        except Exception as e:
            logger.warning("clob_midpoint_failed", token_id=token_id, error=str(e))
            return None

    async def get_price(self, token_id: str, side: str = "buy") -> Optional[Decimal]:
        """
        Get current price for a token.

        Args:
            token_id: Token ID
            side: "buy" for best ask, "sell" for best bid

        Returns:
            Price or None if not available
        """
        try:
            data = await self._request(
                "GET",
                f"/price",
                params={"token_id": token_id, "side": side},
            )
            return Decimal(str(data.get("price", 0)))
        except Exception as e:
            logger.warning("clob_price_failed", token_id=token_id, error=str(e))
            return None

    async def get_prices_history(
        self,
        token_id: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        interval: str = "1h",  # 1m, 5m, 15m, 1h, 4h, 1d
        fidelity: int = 60,
    ) -> List[Dict[str, Any]]:
        """
        Get historical price data for a token.

        Args:
            token_id: Token ID
            start_ts: Start timestamp (seconds)
            end_ts: End timestamp (seconds)
            interval: Candle interval
            fidelity: Data fidelity

        Returns:
            List of price history entries
        """
        params = {"market": token_id, "interval": interval, "fidelity": fidelity}
        if start_ts:
            params["startTs"] = start_ts
        if end_ts:
            params["endTs"] = end_ts

        data = await self._request("GET", "/prices-history", params=params)
        return data.get("history", [])

    async def get_spread(self, token_id: str) -> Optional[Dict[str, Decimal]]:
        """Get bid-ask spread for a token."""
        try:
            data = await self._request(
                "GET",
                f"/spread",
                params={"token_id": token_id},
            )
            return {
                "bid": Decimal(str(data.get("bid", 0))),
                "ask": Decimal(str(data.get("ask", 0))),
                "spread": Decimal(str(data.get("spread", 0))),
            }
        except Exception as e:
            logger.warning("clob_spread_failed", token_id=token_id, error=str(e))
            return None

    async def get_last_trade_price(self, token_id: str) -> Optional[Decimal]:
        """Get last trade price for a token."""
        try:
            data = await self._request(
                "GET",
                "/last-trade-price",
                params={"token_id": token_id},
            )
            return Decimal(str(data.get("price", 0)))
        except Exception as e:
            logger.warning("clob_last_trade_failed", token_id=token_id, error=str(e))
            return None

    def _parse_orderbook(self, token_id: str, data: Dict[str, Any]) -> OrderBook:
        """Parse orderbook data from API response."""
        bids = []
        asks = []

        for bid in data.get("bids", []):
            bids.append(OrderBookLevel(
                price=Decimal(str(bid.get("price", 0))),
                size=Decimal(str(bid.get("size", 0))),
            ))

        for ask in data.get("asks", []):
            asks.append(OrderBookLevel(
                price=Decimal(str(ask.get("price", 0))),
                size=Decimal(str(ask.get("size", 0))),
            ))

        # Sort bids descending (best first), asks ascending (best first)
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return OrderBook(
            token_id=token_id,
            timestamp=datetime.utcnow(),
            bids=bids,
            asks=asks,
        )

    async def get_markets_info(self) -> List[Dict[str, Any]]:
        """Get CLOB market information."""
        return await self._request("GET", "/markets")

    async def get_simplified_markets(self) -> List[Dict[str, Any]]:
        """Get simplified market data."""
        return await self._request("GET", "/simplified-markets")


async def fetch_orderbook(
    token_id: str,
    base_url: str = "https://clob.polymarket.com"
) -> OrderBook:
    """Convenience function to fetch a single orderbook."""
    config = ClobClientConfig(base_url=base_url)
    async with ClobRestClient(config) as client:
        return await client.get_orderbook(token_id)
