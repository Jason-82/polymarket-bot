"""Data API client for positions, activity, and trade history."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import httpx

from storage.models import Position, Order, Fill, AccountState, OrderSide, TradeMode
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DataApiConfig:
    """Configuration for Data API client."""
    base_url: str = "https://data-api.polymarket.com"
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 1.0


class DataApiClient:
    """
    Client for Polymarket Data API.

    The Data API provides:
    - Position information
    - Trade/fill history
    - Activity feed
    - User order history

    Used for reconciliation and account state tracking.
    """

    def __init__(self, config: Optional[DataApiConfig] = None):
        self.config = config or DataApiConfig()
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "DataApiClient":
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
                        "data_api_rate_limited",
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
                    "data_api_request_failed",
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
                    "data_api_request_error",
                    error=str(e),
                    path=path,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(self.config.retry_delay * (2 ** attempt))

            finally:
                if should_close:
                    await client.aclose()

        raise last_error or Exception("Max retries exceeded")

    async def get_positions(self, address: str) -> List[Position]:
        """
        Get current positions for an address.

        Args:
            address: Ethereum address

        Returns:
            List of Position objects
        """
        logger.debug("data_api_get_positions", address=address[:10] + "...")

        data = await self._request("GET", f"/positions", params={"user": address})

        positions = []
        for item in data:
            try:
                pos = self._parse_position(item)
                positions.append(pos)
            except Exception as e:
                logger.warning(
                    "data_api_parse_position_failed",
                    token_id=item.get("asset"),
                    error=str(e),
                )

        logger.info("data_api_positions_fetched", count=len(positions))
        return positions

    async def get_trades(
        self,
        address: str,
        limit: int = 100,
        before: Optional[str] = None,
    ) -> List[Fill]:
        """
        Get trade history for an address.

        Args:
            address: Ethereum address
            limit: Maximum trades to return
            before: Cursor for pagination

        Returns:
            List of Fill objects
        """
        params = {"user": address, "limit": limit}
        if before:
            params["before"] = before

        data = await self._request("GET", "/trades", params=params)

        fills = []
        for item in data.get("trades", data):
            try:
                fill = self._parse_fill(item)
                fills.append(fill)
            except Exception as e:
                logger.warning(
                    "data_api_parse_fill_failed",
                    error=str(e),
                )

        return fills

    async def get_orders(
        self,
        address: str,
        status: Optional[str] = None,  # "open", "filled", "cancelled"
        limit: int = 100,
    ) -> List[Order]:
        """
        Get order history for an address.

        Args:
            address: Ethereum address
            status: Filter by status
            limit: Maximum orders to return

        Returns:
            List of Order objects
        """
        params = {"user": address, "limit": limit}
        if status:
            params["status"] = status

        data = await self._request("GET", "/orders", params=params)

        orders = []
        for item in data.get("orders", data):
            try:
                order = self._parse_order(item)
                orders.append(order)
            except Exception as e:
                logger.warning(
                    "data_api_parse_order_failed",
                    order_id=item.get("id"),
                    error=str(e),
                )

        return orders

    async def get_activity(
        self,
        address: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Get activity feed for an address.

        Args:
            address: Ethereum address
            limit: Maximum activities to return

        Returns:
            List of activity entries (raw dict)
        """
        params = {"user": address, "limit": limit}
        data = await self._request("GET", "/activity", params=params)
        return data.get("activities", data)

    async def get_account_state(
        self,
        address: str,
        balance: Decimal = Decimal("0"),
    ) -> AccountState:
        """
        Get unified account state.

        Args:
            address: Ethereum address
            balance: Current available balance (must be provided externally)

        Returns:
            AccountState object
        """
        # Fetch positions and open orders in parallel
        positions_task = self.get_positions(address)
        orders_task = self.get_orders(address, status="open")

        positions_list, orders = await asyncio.gather(positions_task, orders_task)

        # Convert to dict by token_id
        positions = {pos.token_id: pos for pos in positions_list}

        return AccountState(
            address=address,
            balance=balance,
            positions=positions,
            open_orders=orders,
            timestamp=datetime.utcnow(),
        )

    def _parse_position(self, data: Dict[str, Any]) -> Position:
        """Parse position data from API response."""
        return Position(
            token_id=data.get("asset", data.get("token_id", "")),
            shares=Decimal(str(data.get("size", data.get("shares", 0)))),
            avg_cost=Decimal(str(data.get("avgCost", data.get("avg_cost", 0)))),
            realized_pnl=Decimal(str(data.get("realizedPnl", 0))),
            unrealized_pnl=Decimal(str(data.get("unrealizedPnl", 0))),
            timestamp=datetime.utcnow(),
        )

    def _parse_order(self, data: Dict[str, Any]) -> Order:
        """Parse order data from API response."""
        # Parse timestamps
        created_at = datetime.utcnow()
        updated_at = datetime.utcnow()
        if data.get("createdAt"):
            try:
                created_at = datetime.fromisoformat(
                    data["createdAt"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass
        if data.get("updatedAt"):
            try:
                updated_at = datetime.fromisoformat(
                    data["updatedAt"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        return Order(
            order_id=data.get("id", data.get("order_id", "")),
            client_order_id=data.get("clientOrderId", ""),
            token_id=data.get("asset", data.get("token_id", "")),
            side=OrderSide.BUY if data.get("side", "").lower() == "buy" else OrderSide.SELL,
            price=Decimal(str(data.get("price", 0))),
            original_size=Decimal(str(data.get("originalSize", data.get("size", 0)))),
            remaining_size=Decimal(str(data.get("remainingSize", data.get("size", 0)))),
            status=data.get("status", "unknown"),
            created_at=created_at,
            updated_at=updated_at,
        )

    def _parse_fill(self, data: Dict[str, Any]) -> Fill:
        """Parse fill data from API response."""
        timestamp = datetime.utcnow()
        if data.get("timestamp"):
            try:
                timestamp = datetime.fromisoformat(
                    data["timestamp"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        return Fill(
            fill_id=data.get("id", data.get("fill_id", "")),
            order_id=data.get("orderId", data.get("order_id", "")),
            client_order_id=data.get("clientOrderId", ""),
            token_id=data.get("asset", data.get("token_id", "")),
            side=OrderSide.BUY if data.get("side", "").lower() == "buy" else OrderSide.SELL,
            price=Decimal(str(data.get("price", 0))),
            size=Decimal(str(data.get("size", 0))),
            fee=Decimal(str(data.get("fee", 0))),
            timestamp=timestamp,
            mode=TradeMode.LIVE,  # Data API only shows live trades
        )


async def fetch_account_state(
    address: str,
    base_url: str = "https://data-api.polymarket.com",
    balance: Decimal = Decimal("0"),
) -> AccountState:
    """Convenience function to fetch account state."""
    config = DataApiConfig(base_url=base_url)
    async with DataApiClient(config) as client:
        return await client.get_account_state(address, balance)
