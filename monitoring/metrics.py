"""Metrics collection for monitoring."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional


@dataclass
class BotMetrics:
    """Runtime metrics for monitoring."""

    # Timing
    started_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None

    # Counters
    ticks: int = 0
    orderbook_updates: int = 0
    orders_placed: int = 0
    orders_cancelled: int = 0
    fills: int = 0
    errors: int = 0

    # Gauges
    open_orders_count: int = 0
    positions_count: int = 0
    total_exposure: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")

    # Connection state
    ws_connected: bool = False
    ws_reconnects: int = 0

    # Rate metrics (calculated)
    fills_per_hour: float = 0.0
    orders_per_hour: float = 0.0
    error_rate: float = 0.0

    def update_rates(self) -> None:
        """Update rate metrics based on uptime."""
        if not self.started_at:
            return

        uptime_hours = (datetime.utcnow() - self.started_at).total_seconds() / 3600
        if uptime_hours > 0:
            self.fills_per_hour = self.fills / uptime_hours
            self.orders_per_hour = self.orders_placed / uptime_hours
            self.error_rate = self.errors / max(self.ticks, 1)

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        self.update_rates()
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "uptime_seconds": (
                (datetime.utcnow() - self.started_at).total_seconds()
                if self.started_at else 0
            ),
            "ticks": self.ticks,
            "orderbook_updates": self.orderbook_updates,
            "orders_placed": self.orders_placed,
            "orders_cancelled": self.orders_cancelled,
            "fills": self.fills,
            "errors": self.errors,
            "open_orders_count": self.open_orders_count,
            "positions_count": self.positions_count,
            "total_exposure": str(self.total_exposure),
            "unrealized_pnl": str(self.unrealized_pnl),
            "realized_pnl": str(self.realized_pnl),
            "ws_connected": self.ws_connected,
            "ws_reconnects": self.ws_reconnects,
            "fills_per_hour": round(self.fills_per_hour, 2),
            "orders_per_hour": round(self.orders_per_hour, 2),
            "error_rate": round(self.error_rate, 4),
        }
