"""Order Management System for order lifecycle management."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Set
import time

from storage.models import Order, Fill, OrderIntent, OrderSide, OrderType, TradeMode, OrderBook
from execution.order_intent import round_to_tick
from execution.orderbook_analyzer import OrderBookAnalyzer, estimate_execution_cost
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class OrderState:
    """Internal order state tracking."""
    intent: OrderIntent
    exchange_order_id: Optional[str] = None
    status: str = "pending"  # pending, open, filled, cancelled, rejected
    filled_size: Decimal = Decimal("0")
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_update_time: float = 0  # For rate limiting


class OrderManagementSystem:
    """
    Order Management System (OMS).

    Responsibilities:
    - Translate OrderIntents to exchange orders
    - Track order lifecycle (pending -> open -> filled/cancelled)
    - Maintain idempotency via client_order_id mapping
    - Enforce quote refresh discipline (avoid spam)
    - Support paper trading mode (simulated fills)
    """

    def __init__(
        self,
        mode: str = "PAPER",  # PAPER or LIVE
        min_update_interval_ms: int = 1000,
        price_epsilon: Decimal = Decimal("0.001"),
        max_slippage_pct: Decimal = Decimal("0.05"),
        on_fill: Optional[Callable[[Fill], None]] = None,
    ):
        self.mode = mode
        self.min_update_interval_ms = min_update_interval_ms
        self.price_epsilon = price_epsilon
        self.max_slippage_pct = max_slippage_pct
        self._on_fill = on_fill

        # Order tracking
        self._orders: Dict[str, OrderState] = {}  # client_order_id -> OrderState
        self._exchange_to_client: Dict[str, str] = {}  # exchange_order_id -> client_order_id

        # For live mode, this would be set to the actual client
        self._trading_client = None

        # Paper trading fill simulator
        self._paper_fills: List[Fill] = []

        # Order book analyzer for execution quality
        self._ob_analyzer = OrderBookAnalyzer(
            max_slippage_pct=max_slippage_pct,
        )

    def set_trading_client(self, client) -> None:
        """Set the trading client for live mode."""
        self._trading_client = client

    async def sync_orders(
        self,
        desired_intents: List[OrderIntent],
        current_orderbook: Dict[str, Any],
    ) -> List[str]:
        """
        Synchronize desired order intents with actual open orders.

        Args:
            desired_intents: List of desired order intents from strategy
            current_orderbook: Current orderbook for fill simulation (paper)

        Returns:
            List of actions taken (for logging)
        """
        actions = []

        # Group intents by token
        intent_by_token: Dict[str, List[OrderIntent]] = {}
        for intent in desired_intents:
            if intent.token_id not in intent_by_token:
                intent_by_token[intent.token_id] = []
            intent_by_token[intent.token_id].append(intent)

        # Get current open orders by token
        open_by_token: Dict[str, List[OrderState]] = {}
        for coid, state in self._orders.items():
            if state.status == "open":
                if state.intent.token_id not in open_by_token:
                    open_by_token[state.intent.token_id] = []
                open_by_token[state.intent.token_id].append(state)

        # Process each token
        all_tokens = set(intent_by_token.keys()) | set(open_by_token.keys())
        for token_id in all_tokens:
            new_intents = intent_by_token.get(token_id, [])
            current_orders = open_by_token.get(token_id, [])

            token_actions = await self._sync_token_orders(
                token_id, new_intents, current_orders, current_orderbook
            )
            actions.extend(token_actions)

        return actions

    async def _sync_token_orders(
        self,
        token_id: str,
        new_intents: List[OrderIntent],
        current_orders: List[OrderState],
        current_orderbook: Dict[str, Any],
    ) -> List[str]:
        """Sync orders for a single token."""
        actions = []
        current_time = time.time() * 1000  # ms

        # Build map of current orders by side
        current_by_side: Dict[OrderSide, OrderState] = {}
        for order in current_orders:
            current_by_side[order.intent.side] = order

        # Build map of new intents by side
        new_by_side: Dict[OrderSide, OrderIntent] = {}
        for intent in new_intents:
            new_by_side[intent.side] = intent

        # Compare and update
        for side in [OrderSide.BUY, OrderSide.SELL]:
            current = current_by_side.get(side)
            new = new_by_side.get(side)

            if current and new:
                # Both exist - check if update needed
                if self._should_update(current, new, current_time):
                    await self.cancel_order(current.intent.client_order_id)
                    await self.place_order(new)
                    actions.append(f"update_{side.value}_{token_id}")
            elif current and not new:
                # Cancel existing
                await self.cancel_order(current.intent.client_order_id)
                actions.append(f"cancel_{side.value}_{token_id}")
            elif new and not current:
                # Place new
                await self.place_order(new)
                actions.append(f"place_{side.value}_{token_id}")

        # Check for simulated fills in paper mode
        if self.mode == "PAPER":
            await self._check_paper_fills(token_id, current_orderbook)

        return actions

    def _should_update(
        self,
        current: OrderState,
        new: OrderIntent,
        current_time: float,
    ) -> bool:
        """Check if order should be updated."""
        # Check rate limit
        if current_time - current.last_update_time < self.min_update_interval_ms:
            return False

        # Check price change
        price_diff = abs(current.intent.price - new.price)
        if price_diff < self.price_epsilon:
            # Also check size
            size_diff = abs(current.intent.size - new.size)
            if size_diff < Decimal("0.01"):
                return False

        return True

    async def place_order(self, intent: OrderIntent) -> Optional[str]:
        """
        Place a new order.

        Returns exchange order ID or None if failed.
        """
        # Check for duplicate
        if intent.client_order_id in self._orders:
            existing = self._orders[intent.client_order_id]
            if existing.status in ("pending", "open"):
                logger.warning(
                    "oms_duplicate_order",
                    client_order_id=intent.client_order_id,
                )
                return existing.exchange_order_id

        # Create order state
        state = OrderState(
            intent=intent,
            status="pending",
            last_update_time=time.time() * 1000,
        )
        self._orders[intent.client_order_id] = state

        if self.mode == "LIVE":
            # Place real order
            exchange_id = await self._place_live_order(intent)
            if exchange_id:
                state.exchange_order_id = exchange_id
                state.status = "open"
                self._exchange_to_client[exchange_id] = intent.client_order_id
                logger.info(
                    "oms_order_placed",
                    client_order_id=intent.client_order_id,
                    exchange_order_id=exchange_id,
                    token_id=intent.token_id,
                    side=intent.side.value,
                    price=str(intent.price),
                    size=str(intent.size),
                )
                return exchange_id
            else:
                state.status = "rejected"
                return None
        else:
            # Paper mode - immediately mark as open
            state.status = "open"
            state.exchange_order_id = f"paper_{intent.client_order_id}"
            logger.debug(
                "oms_paper_order_placed",
                client_order_id=intent.client_order_id,
                token_id=intent.token_id,
                side=intent.side.value,
                price=str(intent.price),
                size=str(intent.size),
            )
            return state.exchange_order_id

    async def _place_live_order(self, intent: OrderIntent) -> Optional[str]:
        """Place a real order via trading client."""
        if not self._trading_client:
            logger.error("oms_no_trading_client")
            return None

        try:
            # This would call py-clob-client
            # result = await self._trading_client.create_order(...)
            # return result.order_id
            raise NotImplementedError("Live trading not implemented yet")
        except Exception as e:
            logger.error("oms_place_order_failed", error=str(e))
            return None

    async def cancel_order(self, client_order_id: str) -> bool:
        """Cancel an order by client order ID."""
        if client_order_id not in self._orders:
            return False

        state = self._orders[client_order_id]
        if state.status not in ("pending", "open"):
            return False

        if self.mode == "LIVE" and state.exchange_order_id:
            success = await self._cancel_live_order(state.exchange_order_id)
            if not success:
                return False

        state.status = "cancelled"
        state.updated_at = datetime.utcnow()

        logger.debug(
            "oms_order_cancelled",
            client_order_id=client_order_id,
            exchange_order_id=state.exchange_order_id,
        )
        return True

    async def _cancel_live_order(self, exchange_order_id: str) -> bool:
        """Cancel a real order."""
        if not self._trading_client:
            return False

        try:
            # await self._trading_client.cancel_order(exchange_order_id)
            raise NotImplementedError("Live trading not implemented yet")
        except Exception as e:
            logger.error("oms_cancel_order_failed", error=str(e))
            return False

    async def cancel_all(self, token_id: Optional[str] = None) -> int:
        """
        Cancel all open orders.

        Args:
            token_id: If provided, only cancel orders for this token

        Returns:
            Number of orders cancelled
        """
        count = 0
        for coid, state in list(self._orders.items()):
            if state.status not in ("pending", "open"):
                continue
            if token_id and state.intent.token_id != token_id:
                continue

            if await self.cancel_order(coid):
                count += 1

        logger.info("oms_cancel_all", token_id=token_id, count=count)
        return count

    async def _check_paper_fills(
        self,
        token_id: str,
        orderbook: Dict[str, Any],
    ) -> None:
        """Check for simulated fills in paper mode with VWAP-based pricing."""
        if not orderbook:
            return

        best_bid = orderbook.get("best_bid")
        best_ask = orderbook.get("best_ask")

        # Try to get full orderbook for VWAP calculation
        full_orderbook = orderbook.get("_full_orderbook")  # OrderBook object if available

        for coid, state in list(self._orders.items()):
            if state.status != "open":
                continue
            if state.intent.token_id != token_id:
                continue

            fill = None
            fill_price = None

            if state.intent.side == OrderSide.BUY:
                # Buy order fills if price >= best_ask
                if best_ask and state.intent.price >= best_ask:
                    # Use VWAP if full orderbook available
                    if full_orderbook:
                        vwap_result = self._ob_analyzer.calculate_vwap(
                            full_orderbook, OrderSide.BUY, state.intent.size
                        )
                        if vwap_result.fully_filled:
                            fill_price = vwap_result.vwap
                        else:
                            # Partial fill at VWAP
                            fill_price = vwap_result.vwap
                            logger.debug(
                                "oms_paper_partial_fill",
                                requested=str(state.intent.size),
                                available=str(vwap_result.total_size_available),
                            )
                    else:
                        fill_price = best_ask

                    fill = Fill(
                        fill_id=f"paper_fill_{coid}_{int(time.time())}",
                        order_id=state.exchange_order_id or coid,
                        client_order_id=coid,
                        token_id=token_id,
                        side=OrderSide.BUY,
                        price=fill_price,
                        size=state.intent.size,
                        fee=Decimal("0"),
                        timestamp=datetime.utcnow(),
                        mode=TradeMode.PAPER,
                    )
            else:
                # Sell order fills if price <= best_bid
                if best_bid and state.intent.price <= best_bid:
                    # Use VWAP if full orderbook available
                    if full_orderbook:
                        vwap_result = self._ob_analyzer.calculate_vwap(
                            full_orderbook, OrderSide.SELL, state.intent.size
                        )
                        if vwap_result.fully_filled:
                            fill_price = vwap_result.vwap
                        else:
                            fill_price = vwap_result.vwap
                    else:
                        fill_price = best_bid

                    fill = Fill(
                        fill_id=f"paper_fill_{coid}_{int(time.time())}",
                        order_id=state.exchange_order_id or coid,
                        client_order_id=coid,
                        token_id=token_id,
                        side=OrderSide.SELL,
                        price=fill_price,
                        size=state.intent.size,
                        fee=Decimal("0"),
                        timestamp=datetime.utcnow(),
                        mode=TradeMode.PAPER,
                    )

            if fill:
                state.status = "filled"
                state.filled_size = fill.size
                state.updated_at = datetime.utcnow()
                self._paper_fills.append(fill)

                # Log slippage if we used VWAP
                slippage = None
                if full_orderbook:
                    base_price = best_ask if fill.side == OrderSide.BUY else best_bid
                    if base_price:
                        slippage = abs(fill.price - base_price)

                logger.info(
                    "oms_paper_fill",
                    client_order_id=coid,
                    token_id=token_id,
                    side=fill.side.value,
                    price=str(fill.price),
                    size=str(fill.size),
                    slippage=str(slippage) if slippage else None,
                )

                if self._on_fill:
                    self._on_fill(fill)

    def check_execution_quality(
        self,
        intent: OrderIntent,
        orderbook: OrderBook,
    ) -> Dict[str, Any]:
        """
        Check execution quality for a proposed order.

        Returns analysis including:
        - Expected fill price (VWAP)
        - Estimated slippage
        - Whether execution is recommended
        - Warnings about liquidity
        """
        return self._ob_analyzer.check_execution_quality(
            orderbook=orderbook,
            side=intent.side,
            size=intent.size,
            price=intent.price,
        )

    def estimate_fill_cost(
        self,
        intent: OrderIntent,
        orderbook: OrderBook,
    ) -> Dict[str, Decimal]:
        """
        Estimate total cost of executing an order.

        Returns:
        - notional: Base trade value
        - slippage_cost: Cost due to slippage
        - gas_cost: Network fees
        - total_cost: All-in cost
        """
        return estimate_execution_cost(
            orderbook=orderbook,
            side=intent.side,
            size=intent.size,
        )

    def get_optimal_size(
        self,
        orderbook: OrderBook,
        side: OrderSide,
        max_slippage_pct: Optional[Decimal] = None,
    ) -> Decimal:
        """
        Get the optimal order size for a given slippage tolerance.

        Returns the maximum size that can be executed within the slippage limit.
        """
        slippage = max_slippage_pct or self.max_slippage_pct
        return self._ob_analyzer.get_max_size_for_slippage(
            orderbook=orderbook,
            side=side,
            max_slippage_pct=slippage,
        )

    def analyze_liquidity(self, orderbook: OrderBook) -> Dict[str, Any]:
        """
        Analyze orderbook liquidity.

        Returns snapshot of:
        - Spread and spread percentage
        - Depth at various price levels
        - Order book imbalance
        """
        snapshot = self._ob_analyzer.analyze_liquidity(orderbook)
        return {
            "token_id": snapshot.token_id,
            "best_bid": snapshot.best_bid,
            "best_ask": snapshot.best_ask,
            "spread": snapshot.spread,
            "spread_pct": snapshot.spread_pct,
            "bid_depth_10bps": snapshot.bid_depth_10bps,
            "ask_depth_10bps": snapshot.ask_depth_10bps,
            "bid_depth_50bps": snapshot.bid_depth_50bps,
            "ask_depth_50bps": snapshot.ask_depth_50bps,
            "total_bid_volume": snapshot.total_bid_volume,
            "total_ask_volume": snapshot.total_ask_volume,
            "imbalance": snapshot.imbalance,
        }

    def get_open_orders(self) -> List[Order]:
        """Get all open orders."""
        orders = []
        for state in self._orders.values():
            if state.status == "open":
                orders.append(Order(
                    order_id=state.exchange_order_id or state.intent.client_order_id,
                    client_order_id=state.intent.client_order_id,
                    token_id=state.intent.token_id,
                    side=state.intent.side,
                    price=state.intent.price,
                    original_size=state.intent.size,
                    remaining_size=state.intent.size - state.filled_size,
                    status="open",
                    created_at=state.created_at,
                    updated_at=state.updated_at,
                ))
        return orders

    def get_paper_fills(self) -> List[Fill]:
        """Get all paper trading fills."""
        return self._paper_fills.copy()

    def clear_paper_fills(self) -> None:
        """Clear paper trading fills."""
        self._paper_fills = []

    def get_status(self) -> Dict[str, Any]:
        """Get OMS status for monitoring."""
        open_count = sum(1 for s in self._orders.values() if s.status == "open")
        filled_count = sum(1 for s in self._orders.values() if s.status == "filled")

        return {
            "mode": self.mode,
            "total_orders": len(self._orders),
            "open_orders": open_count,
            "filled_orders": filled_count,
            "paper_fills": len(self._paper_fills),
        }
