"""Paper trading fill simulation engine."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from storage.models import OrderBook, OrderIntent, Fill, Position, OrderSide, TradeMode
from monitoring.logger import get_logger

logger = get_logger(__name__)


class FillModel(str, Enum):
    """Fill simulation model."""
    OPTIMISTIC = "optimistic"      # Fill if crosses best bid/ask
    CONSERVATIVE = "conservative"  # Require size available + slippage


@dataclass
class PaperPosition:
    """Position tracking for paper trading."""
    token_id: str
    shares: Decimal = Decimal("0")
    avg_cost: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")

    def apply_fill(self, fill: Fill) -> Decimal:
        """
        Apply a fill to the position.

        Returns the realized PnL from this fill (if any).
        """
        realized = Decimal("0")

        if fill.side == OrderSide.BUY:
            # Buying - increase position
            if self.shares >= 0:
                # Adding to long or opening long
                total_cost = self.shares * self.avg_cost + fill.size * fill.price
                self.shares += fill.size
                self.avg_cost = total_cost / self.shares if self.shares > 0 else Decimal("0")
            else:
                # Covering short
                if fill.size >= abs(self.shares):
                    # Fully cover + go long
                    realized = abs(self.shares) * (self.avg_cost - fill.price)
                    remaining = fill.size - abs(self.shares)
                    self.shares = remaining
                    self.avg_cost = fill.price if remaining > 0 else Decimal("0")
                else:
                    # Partial cover
                    realized = fill.size * (self.avg_cost - fill.price)
                    self.shares += fill.size  # Less negative
        else:
            # Selling - decrease position
            if self.shares <= 0:
                # Adding to short or opening short
                total_cost = abs(self.shares) * self.avg_cost + fill.size * fill.price
                self.shares -= fill.size
                self.avg_cost = total_cost / abs(self.shares) if self.shares != 0 else Decimal("0")
            else:
                # Closing long
                if fill.size >= self.shares:
                    # Fully close + go short
                    realized = self.shares * (fill.price - self.avg_cost)
                    remaining = fill.size - self.shares
                    self.shares = -remaining
                    self.avg_cost = fill.price if remaining > 0 else Decimal("0")
                else:
                    # Partial close
                    realized = fill.size * (fill.price - self.avg_cost)
                    self.shares -= fill.size

        self.realized_pnl += realized
        return realized

    def get_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Calculate unrealized PnL at current price."""
        if self.shares == 0:
            return Decimal("0")
        elif self.shares > 0:
            return self.shares * (current_price - self.avg_cost)
        else:
            return abs(self.shares) * (self.avg_cost - current_price)

    def to_position(self, current_price: Optional[Decimal] = None) -> Position:
        """Convert to Position model."""
        unrealized = Decimal("0")
        if current_price:
            unrealized = self.get_unrealized_pnl(current_price)

        return Position(
            token_id=self.token_id,
            shares=self.shares,
            avg_cost=self.avg_cost,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            timestamp=datetime.utcnow(),
        )


class PaperTradingEngine:
    """
    Paper trading simulation engine.

    Simulates order execution against live orderbook data without
    placing real orders. Tracks positions and PnL.
    """

    def __init__(
        self,
        fill_model: FillModel = FillModel.OPTIMISTIC,
        fee_rate: Decimal = Decimal("0"),
    ):
        self.fill_model = fill_model
        self.fee_rate = fee_rate

        # State
        self._positions: Dict[str, PaperPosition] = {}
        self._fills: List[Fill] = []
        self._fill_counter = 0

    def simulate_fills(
        self,
        intents: List[OrderIntent],
        orderbooks: Dict[str, OrderBook],
    ) -> List[Fill]:
        """
        Simulate order fills based on current orderbook state.

        Args:
            intents: List of order intents to simulate
            orderbooks: Current orderbooks by token_id

        Returns:
            List of simulated fills
        """
        new_fills = []

        for intent in intents:
            orderbook = orderbooks.get(intent.token_id)
            if not orderbook:
                continue

            fill = self._try_fill(intent, orderbook)
            if fill:
                # Apply to position
                position = self._get_or_create_position(intent.token_id)
                position.apply_fill(fill)

                self._fills.append(fill)
                new_fills.append(fill)

                logger.info(
                    "paper_fill",
                    token_id=fill.token_id,
                    side=fill.side.value,
                    price=str(fill.price),
                    size=str(fill.size),
                    fee=str(fill.fee),
                )

        return new_fills

    def _try_fill(
        self,
        intent: OrderIntent,
        orderbook: OrderBook,
    ) -> Optional[Fill]:
        """Try to simulate a fill for an order intent."""
        if self.fill_model == FillModel.OPTIMISTIC:
            return self._optimistic_fill(intent, orderbook)
        else:
            return self._conservative_fill(intent, orderbook)

    def _optimistic_fill(
        self,
        intent: OrderIntent,
        orderbook: OrderBook,
    ) -> Optional[Fill]:
        """Optimistic fill model: fill if crosses best bid/ask."""
        if intent.side == OrderSide.BUY:
            # Buy fills if bid price >= best ask
            if orderbook.best_ask is None:
                return None
            if intent.price < orderbook.best_ask:
                return None

            fill_price = orderbook.best_ask
        else:
            # Sell fills if ask price <= best bid
            if orderbook.best_bid is None:
                return None
            if intent.price > orderbook.best_bid:
                return None

            fill_price = orderbook.best_bid

        fee = intent.size * fill_price * self.fee_rate

        self._fill_counter += 1
        return Fill(
            fill_id=f"paper_{self._fill_counter}",
            order_id=f"paper_order_{intent.client_order_id}",
            client_order_id=intent.client_order_id,
            token_id=intent.token_id,
            side=intent.side,
            price=fill_price,
            size=intent.size,
            fee=fee,
            timestamp=datetime.utcnow(),
            mode=TradeMode.PAPER,
        )

    def _conservative_fill(
        self,
        intent: OrderIntent,
        orderbook: OrderBook,
    ) -> Optional[Fill]:
        """Conservative fill model: require sufficient size at level."""
        if intent.side == OrderSide.BUY:
            if orderbook.best_ask is None or orderbook.best_ask_size is None:
                return None
            if intent.price < orderbook.best_ask:
                return None
            if intent.size > orderbook.best_ask_size:
                return None  # Not enough liquidity

            fill_price = orderbook.best_ask
        else:
            if orderbook.best_bid is None or orderbook.best_bid_size is None:
                return None
            if intent.price > orderbook.best_bid:
                return None
            if intent.size > orderbook.best_bid_size:
                return None

            fill_price = orderbook.best_bid

        fee = intent.size * fill_price * self.fee_rate

        self._fill_counter += 1
        return Fill(
            fill_id=f"paper_{self._fill_counter}",
            order_id=f"paper_order_{intent.client_order_id}",
            client_order_id=intent.client_order_id,
            token_id=intent.token_id,
            side=intent.side,
            price=fill_price,
            size=intent.size,
            fee=fee,
            timestamp=datetime.utcnow(),
            mode=TradeMode.PAPER,
        )

    def _get_or_create_position(self, token_id: str) -> PaperPosition:
        """Get or create a paper position."""
        if token_id not in self._positions:
            self._positions[token_id] = PaperPosition(token_id=token_id)
        return self._positions[token_id]

    def get_positions(
        self,
        orderbooks: Optional[Dict[str, OrderBook]] = None,
    ) -> Dict[str, Position]:
        """Get all positions as Position objects."""
        result = {}
        for token_id, paper_pos in self._positions.items():
            current_price = None
            if orderbooks and token_id in orderbooks:
                current_price = orderbooks[token_id].midpoint
            result[token_id] = paper_pos.to_position(current_price)
        return result

    def get_fills(self) -> List[Fill]:
        """Get all fills."""
        return self._fills.copy()

    def get_total_pnl(
        self,
        orderbooks: Optional[Dict[str, OrderBook]] = None,
    ) -> tuple[Decimal, Decimal]:
        """
        Get total realized and unrealized PnL.

        Returns:
            Tuple of (realized_pnl, unrealized_pnl)
        """
        realized = sum(pos.realized_pnl for pos in self._positions.values())
        unrealized = Decimal("0")

        if orderbooks:
            for token_id, pos in self._positions.items():
                if token_id in orderbooks:
                    mid = orderbooks[token_id].midpoint
                    if mid:
                        unrealized += pos.get_unrealized_pnl(mid)

        return realized, unrealized

    def reset(self) -> None:
        """Reset all state."""
        self._positions = {}
        self._fills = []
        self._fill_counter = 0
