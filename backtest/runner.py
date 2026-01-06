"""Backtesting harness for historical strategy evaluation."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from storage.models import OrderBook, OrderBookLevel, Market
from storage.database import Database
from storage.dao import OrderBookDAO
from strategies.base import StrategyBase, StrategyContext
from execution.paper_engine import PaperTradingEngine, FillModel
from backtest.metrics import BacktestMetrics, calculate_metrics
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""
    strategy_name: str
    strategy_config: Dict[str, Any]
    token_ids: List[str]
    start_date: datetime
    end_date: datetime
    initial_balance: Decimal = Decimal("10000")
    fill_model: FillModel = FillModel.OPTIMISTIC


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    config: BacktestConfig
    metrics: BacktestMetrics
    fills: List[Dict[str, Any]]
    final_positions: Dict[str, Any]
    equity_curve: List[tuple[datetime, Decimal]]


class BacktestRunner:
    """
    Runs backtests on historical data.

    Supports:
    - Bar-based backtesting using orderbook snapshots
    - Configurable fill models
    - Performance metrics calculation
    """

    def __init__(self, database_path: str = "data/polymarket.db"):
        self.db = Database(database_path)
        self.db.initialize()
        self.orderbook_dao = OrderBookDAO(self.db)

    async def run(
        self,
        strategy: StrategyBase,
        config: BacktestConfig,
    ) -> BacktestResult:
        """
        Run a backtest.

        Args:
            strategy: Strategy instance to test
            config: Backtest configuration

        Returns:
            BacktestResult with metrics and details
        """
        logger.info(
            "backtest_starting",
            strategy=strategy.name,
            start=config.start_date.isoformat(),
            end=config.end_date.isoformat(),
            tokens=len(config.token_ids),
        )

        # Initialize paper trading engine
        paper_engine = PaperTradingEngine(
            fill_model=config.fill_model,
            fee_rate=Decimal("0"),  # No fees in backtest
        )

        # Load historical data
        historical_data = self._load_historical_data(
            config.token_ids,
            config.start_date,
            config.end_date,
        )

        if not historical_data:
            raise ValueError("No historical data found for the specified period")

        # Get unique timestamps
        timestamps = sorted(set(
            ts for data in historical_data.values() for ts in data.keys()
        ))

        logger.info(
            "backtest_data_loaded",
            data_points=len(timestamps),
        )

        # Track equity curve
        equity_curve: List[tuple[datetime, Decimal]] = []
        balance = config.initial_balance

        # Run through historical data
        for timestamp in timestamps:
            # Build orderbooks for this timestamp
            orderbooks: Dict[str, OrderBook] = {}
            for token_id in config.token_ids:
                if token_id in historical_data and timestamp in historical_data[token_id]:
                    orderbooks[token_id] = historical_data[token_id][timestamp]

            if not orderbooks:
                continue

            # Build strategy context
            positions = paper_engine.get_positions(orderbooks)
            context = StrategyContext(
                timestamp=timestamp,
                orderbooks=orderbooks,
                market_metadata={},
                token_to_market={},
                positions=positions,
                open_orders=[],
                account_balance=balance,
                mode="PAPER",
                config=config.strategy_config,
            )

            # Run strategy
            try:
                intents = await strategy.on_tick(context)

                # Simulate fills
                fills = paper_engine.simulate_fills(intents, orderbooks)

                # Update balance based on fills
                for fill in fills:
                    if fill.side.value == "BUY":
                        balance -= fill.notional + fill.fee
                    else:
                        balance += fill.notional - fill.fee

            except Exception as e:
                logger.warning(
                    "backtest_strategy_error",
                    timestamp=timestamp.isoformat(),
                    error=str(e),
                )

            # Calculate equity
            realized, unrealized = paper_engine.get_total_pnl(orderbooks)
            equity = balance + unrealized
            equity_curve.append((timestamp, equity))

        # Calculate final metrics
        final_positions = paper_engine.get_positions()
        all_fills = paper_engine.get_fills()

        metrics = calculate_metrics(
            equity_curve=equity_curve,
            fills=all_fills,
            initial_balance=config.initial_balance,
        )

        logger.info(
            "backtest_complete",
            total_return=f"{metrics.total_return_pct:.2%}",
            max_drawdown=f"{metrics.max_drawdown_pct:.2%}",
            trades=metrics.total_trades,
        )

        return BacktestResult(
            config=config,
            metrics=metrics,
            fills=[{
                "timestamp": f.timestamp.isoformat(),
                "token_id": f.token_id,
                "side": f.side.value,
                "price": str(f.price),
                "size": str(f.size),
            } for f in all_fills],
            final_positions={
                tid: {
                    "shares": str(pos.shares),
                    "avg_cost": str(pos.avg_cost),
                    "realized_pnl": str(pos.realized_pnl),
                }
                for tid, pos in final_positions.items()
            },
            equity_curve=equity_curve,
        )

    def _load_historical_data(
        self,
        token_ids: List[str],
        start: datetime,
        end: datetime,
    ) -> Dict[str, Dict[datetime, OrderBook]]:
        """Load historical orderbook data from database."""
        data: Dict[str, Dict[datetime, OrderBook]] = {}

        for token_id in token_ids:
            orderbooks = self.orderbook_dao.get_history(
                token_id=token_id,
                start=start,
                end=end,
                limit=100000,
            )

            data[token_id] = {ob.timestamp: ob for ob in orderbooks}

        return data
