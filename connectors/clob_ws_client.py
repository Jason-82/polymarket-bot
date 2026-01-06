"""CLOB WebSocket client for real-time orderbook updates."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
import websockets
from websockets.exceptions import ConnectionClosed

from storage.models import OrderBook, OrderBookLevel
from monitoring.logger import get_logger

logger = get_logger(__name__)


class ChannelType(str, Enum):
    """WebSocket channel types."""
    MARKET = "market"  # Orderbook updates for a token
    USER = "user"      # User-specific updates (orders, fills)


@dataclass
class Subscription:
    """WebSocket subscription."""
    channel: ChannelType
    asset_id: str  # token_id for market channel, address for user channel


@dataclass
class ClobWsConfig:
    """Configuration for CLOB WebSocket client."""
    url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"
    ping_interval: float = 30.0
    reconnect_delay: float = 5.0
    max_reconnect_delay: float = 60.0
    max_reconnect_attempts: int = 10


class ClobWebSocketClient:
    """
    WebSocket client for real-time CLOB data.

    Provides:
    - Real-time orderbook updates
    - User order/fill notifications (if authenticated)
    """

    def __init__(
        self,
        config: Optional[ClobWsConfig] = None,
        on_orderbook_update: Optional[Callable[[str, OrderBook], None]] = None,
        on_trade: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_user_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.config = config or ClobWsConfig()

        # Callbacks
        self._on_orderbook_update = on_orderbook_update
        self._on_trade = on_trade
        self._on_user_update = on_user_update

        # State
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscriptions: Set[Subscription] = set()
        self._orderbooks: Dict[str, OrderBook] = {}
        self._running = False
        self._reconnect_attempts = 0

        # Tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._ws is not None and self._ws.open

    @property
    def orderbooks(self) -> Dict[str, OrderBook]:
        """Get current orderbooks."""
        return self._orderbooks.copy()

    async def connect(self) -> None:
        """Connect to WebSocket server."""
        logger.info("clob_ws_connecting", url=self.config.url)

        try:
            self._ws = await websockets.connect(
                self.config.url,
                ping_interval=self.config.ping_interval,
            )
            self._running = True
            self._reconnect_attempts = 0

            # Start receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Resubscribe to existing subscriptions
            for sub in self._subscriptions:
                await self._send_subscribe(sub)

            logger.info("clob_ws_connected")

        except Exception as e:
            logger.error("clob_ws_connect_failed", error=str(e))
            raise

    async def disconnect(self) -> None:
        """Disconnect from WebSocket server."""
        logger.info("clob_ws_disconnecting")
        self._running = False

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("clob_ws_disconnected")

    async def subscribe_market(self, token_id: str) -> None:
        """
        Subscribe to orderbook updates for a token.

        Args:
            token_id: Token ID to subscribe to
        """
        sub = Subscription(channel=ChannelType.MARKET, asset_id=token_id)
        self._subscriptions.add(sub)

        if self.is_connected:
            await self._send_subscribe(sub)

    async def subscribe_markets(self, token_ids: List[str]) -> None:
        """Subscribe to multiple market channels."""
        for token_id in token_ids:
            await self.subscribe_market(token_id)

    async def unsubscribe_market(self, token_id: str) -> None:
        """Unsubscribe from orderbook updates for a token."""
        sub = Subscription(channel=ChannelType.MARKET, asset_id=token_id)
        self._subscriptions.discard(sub)

        if self.is_connected:
            await self._send_unsubscribe(sub)

    async def subscribe_user(self, address: str) -> None:
        """
        Subscribe to user-specific updates.

        Args:
            address: User's Ethereum address
        """
        sub = Subscription(channel=ChannelType.USER, asset_id=address)
        self._subscriptions.add(sub)

        if self.is_connected:
            await self._send_subscribe(sub)

    async def _send_subscribe(self, sub: Subscription) -> None:
        """Send subscription message."""
        if not self._ws:
            return

        message = {
            "type": "subscribe",
            "channel": sub.channel.value,
            "assets_ids": [sub.asset_id],
        }

        logger.debug("clob_ws_subscribe", channel=sub.channel.value, asset=sub.asset_id)
        await self._ws.send(json.dumps(message))

    async def _send_unsubscribe(self, sub: Subscription) -> None:
        """Send unsubscription message."""
        if not self._ws:
            return

        message = {
            "type": "unsubscribe",
            "channel": sub.channel.value,
            "assets_ids": [sub.asset_id],
        }

        await self._ws.send(json.dumps(message))

    async def _receive_loop(self) -> None:
        """Main receive loop for WebSocket messages."""
        while self._running:
            try:
                if not self._ws:
                    await asyncio.sleep(1)
                    continue

                message = await self._ws.recv()
                await self._handle_message(message)

            except ConnectionClosed:
                logger.warning("clob_ws_connection_closed")
                if self._running:
                    await self._reconnect()

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.error("clob_ws_receive_error", error=str(e))
                await asyncio.sleep(1)

    async def _handle_message(self, raw_message: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(raw_message)
            msg_type = data.get("type", data.get("event_type"))

            if msg_type == "book":
                await self._handle_book_update(data)
            elif msg_type == "price_change":
                await self._handle_price_change(data)
            elif msg_type == "trade":
                await self._handle_trade(data)
            elif msg_type == "order":
                await self._handle_user_order(data)
            elif msg_type == "fill":
                await self._handle_user_fill(data)
            elif msg_type == "subscribed":
                logger.debug("clob_ws_subscribed", data=data)
            elif msg_type == "error":
                logger.error("clob_ws_server_error", data=data)
            else:
                logger.debug("clob_ws_unknown_message", type=msg_type)

        except json.JSONDecodeError:
            logger.warning("clob_ws_invalid_json", message=raw_message[:100])
        except Exception as e:
            logger.error("clob_ws_handle_error", error=str(e))

    async def _handle_book_update(self, data: Dict[str, Any]) -> None:
        """Handle orderbook update message."""
        token_id = data.get("asset_id", data.get("market"))
        if not token_id:
            return

        # Parse bids and asks
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

        # Sort
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        orderbook = OrderBook(
            token_id=token_id,
            timestamp=datetime.utcnow(),
            bids=bids,
            asks=asks,
        )

        self._orderbooks[token_id] = orderbook

        if self._on_orderbook_update:
            try:
                self._on_orderbook_update(token_id, orderbook)
            except Exception as e:
                logger.error("clob_ws_callback_error", error=str(e))

    async def _handle_price_change(self, data: Dict[str, Any]) -> None:
        """Handle price change message."""
        token_id = data.get("asset_id")
        if token_id and token_id in self._orderbooks:
            # Update midpoint based on price change
            price = Decimal(str(data.get("price", 0)))
            logger.debug("clob_ws_price_change", token_id=token_id, price=price)

    async def _handle_trade(self, data: Dict[str, Any]) -> None:
        """Handle trade message."""
        if self._on_trade:
            try:
                self._on_trade(data)
            except Exception as e:
                logger.error("clob_ws_trade_callback_error", error=str(e))

    async def _handle_user_order(self, data: Dict[str, Any]) -> None:
        """Handle user order update."""
        if self._on_user_update:
            try:
                self._on_user_update({"type": "order", "data": data})
            except Exception as e:
                logger.error("clob_ws_user_callback_error", error=str(e))

    async def _handle_user_fill(self, data: Dict[str, Any]) -> None:
        """Handle user fill update."""
        if self._on_user_update:
            try:
                self._on_user_update({"type": "fill", "data": data})
            except Exception as e:
                logger.error("clob_ws_user_callback_error", error=str(e))

    async def _reconnect(self) -> None:
        """Attempt to reconnect after disconnection."""
        self._ws = None

        while self._running and self._reconnect_attempts < self.config.max_reconnect_attempts:
            self._reconnect_attempts += 1
            delay = min(
                self.config.reconnect_delay * (2 ** (self._reconnect_attempts - 1)),
                self.config.max_reconnect_delay,
            )

            logger.info(
                "clob_ws_reconnecting",
                attempt=self._reconnect_attempts,
                delay=delay,
            )
            await asyncio.sleep(delay)

            try:
                await self.connect()
                return
            except Exception as e:
                logger.warning("clob_ws_reconnect_failed", error=str(e))

        logger.error("clob_ws_max_reconnects_reached")

    def get_orderbook(self, token_id: str) -> Optional[OrderBook]:
        """Get current orderbook for a token."""
        return self._orderbooks.get(token_id)

    async def __aenter__(self) -> "ClobWebSocketClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()
