"""Performance metrics calculation for backtesting."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from storage.models import Fill


@dataclass
class BacktestMetrics:
    """Comprehensive backtest performance metrics."""

    # Return metrics
    total_return: Decimal
    total_return_pct: Decimal
    annualized_return_pct: Optional[Decimal]

    # Risk metrics
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    volatility: Optional[Decimal]  # Daily return volatility
    sharpe_ratio: Optional[Decimal]

    # Trade metrics
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    average_win: Decimal
    average_loss: Decimal
    profit_factor: Optional[Decimal]  # Gross profit / Gross loss

    # Timing metrics
    start_date: datetime
    end_date: datetime
    trading_days: int

    # Per-market attribution (optional)
    per_token_pnl: dict = None


def calculate_metrics(
    equity_curve: List[tuple[datetime, Decimal]],
    fills: List[Fill],
    initial_balance: Decimal,
    risk_free_rate: Decimal = Decimal("0.05"),  # 5% annual
) -> BacktestMetrics:
    """
    Calculate comprehensive performance metrics.

    Args:
        equity_curve: List of (timestamp, equity) tuples
        fills: List of executed fills
        initial_balance: Starting balance
        risk_free_rate: Annual risk-free rate for Sharpe ratio

    Returns:
        BacktestMetrics object
    """
    if not equity_curve:
        return _empty_metrics()

    # Sort equity curve by timestamp
    equity_curve = sorted(equity_curve, key=lambda x: x[0])

    start_date = equity_curve[0][0]
    end_date = equity_curve[-1][0]
    final_equity = equity_curve[-1][1]

    # Total return
    total_return = final_equity - initial_balance
    total_return_pct = total_return / initial_balance if initial_balance > 0 else Decimal("0")

    # Trading days
    trading_days = (end_date - start_date).days
    if trading_days == 0:
        trading_days = 1

    # Annualized return
    years = Decimal(str(trading_days)) / Decimal("365")
    annualized_return_pct = None
    if years > 0 and total_return_pct > Decimal("-1"):
        try:
            annualized_return_pct = (
                (1 + total_return_pct) ** (Decimal("1") / years) - 1
            )
        except Exception:
            pass

    # Max drawdown
    max_drawdown, max_drawdown_pct = _calculate_max_drawdown(equity_curve)

    # Daily returns for volatility/Sharpe
    daily_returns = _calculate_daily_returns(equity_curve)
    volatility = _calculate_volatility(daily_returns)
    sharpe_ratio = _calculate_sharpe_ratio(
        daily_returns, volatility, risk_free_rate
    )

    # Trade metrics
    trade_metrics = _calculate_trade_metrics(fills)

    return BacktestMetrics(
        total_return=total_return,
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return_pct,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        volatility=volatility,
        sharpe_ratio=sharpe_ratio,
        total_trades=trade_metrics["total"],
        winning_trades=trade_metrics["wins"],
        losing_trades=trade_metrics["losses"],
        win_rate=trade_metrics["win_rate"],
        average_win=trade_metrics["avg_win"],
        average_loss=trade_metrics["avg_loss"],
        profit_factor=trade_metrics["profit_factor"],
        start_date=start_date,
        end_date=end_date,
        trading_days=trading_days,
        per_token_pnl=_calculate_per_token_pnl(fills),
    )


def _empty_metrics() -> BacktestMetrics:
    """Return empty metrics for edge case."""
    return BacktestMetrics(
        total_return=Decimal("0"),
        total_return_pct=Decimal("0"),
        annualized_return_pct=None,
        max_drawdown=Decimal("0"),
        max_drawdown_pct=Decimal("0"),
        volatility=None,
        sharpe_ratio=None,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=Decimal("0"),
        average_win=Decimal("0"),
        average_loss=Decimal("0"),
        profit_factor=None,
        start_date=datetime.utcnow(),
        end_date=datetime.utcnow(),
        trading_days=0,
    )


def _calculate_max_drawdown(
    equity_curve: List[tuple[datetime, Decimal]]
) -> tuple[Decimal, Decimal]:
    """Calculate maximum drawdown in absolute and percentage terms."""
    if not equity_curve:
        return Decimal("0"), Decimal("0")

    peak = equity_curve[0][1]
    max_dd = Decimal("0")
    max_dd_pct = Decimal("0")

    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = drawdown / peak if peak > 0 else Decimal("0")

    return max_dd, max_dd_pct


def _calculate_daily_returns(
    equity_curve: List[tuple[datetime, Decimal]]
) -> List[Decimal]:
    """Calculate daily percentage returns."""
    if len(equity_curve) < 2:
        return []

    # Group by date
    daily_equity = {}
    for ts, equity in equity_curve:
        date = ts.date()
        daily_equity[date] = equity  # Keep last equity of the day

    dates = sorted(daily_equity.keys())
    returns = []

    for i in range(1, len(dates)):
        prev_equity = daily_equity[dates[i - 1]]
        curr_equity = daily_equity[dates[i]]
        if prev_equity > 0:
            ret = (curr_equity - prev_equity) / prev_equity
            returns.append(ret)

    return returns


def _calculate_volatility(daily_returns: List[Decimal]) -> Optional[Decimal]:
    """Calculate annualized volatility of daily returns."""
    if len(daily_returns) < 2:
        return None

    # Mean return
    mean = sum(daily_returns) / len(daily_returns)

    # Variance
    variance = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)

    # Standard deviation (daily)
    try:
        daily_vol = variance ** Decimal("0.5")
        # Annualize (sqrt(252))
        annualized_vol = daily_vol * Decimal("15.87")  # sqrt(252) ≈ 15.87
        return annualized_vol
    except Exception:
        return None


def _calculate_sharpe_ratio(
    daily_returns: List[Decimal],
    volatility: Optional[Decimal],
    risk_free_rate: Decimal,
) -> Optional[Decimal]:
    """Calculate annualized Sharpe ratio."""
    if not daily_returns or volatility is None or volatility == 0:
        return None

    # Annualized mean return
    mean_daily = sum(daily_returns) / len(daily_returns)
    annualized_return = mean_daily * Decimal("252")

    # Sharpe ratio
    excess_return = annualized_return - risk_free_rate
    return excess_return / volatility


def _calculate_trade_metrics(fills: List[Fill]) -> dict:
    """Calculate trade-level metrics."""
    if not fills:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": Decimal("0"),
            "avg_win": Decimal("0"),
            "avg_loss": Decimal("0"),
            "profit_factor": None,
        }

    # Group fills into round-trip trades by token
    # Simplified: count each fill as a "trade"
    total = len(fills)

    # For proper PnL, we'd need to track cost basis
    # For now, just count fills
    return {
        "total": total,
        "wins": 0,  # Would need cost basis tracking
        "losses": 0,
        "win_rate": Decimal("0"),
        "avg_win": Decimal("0"),
        "avg_loss": Decimal("0"),
        "profit_factor": None,
    }


def _calculate_per_token_pnl(fills: List[Fill]) -> dict:
    """Calculate PnL per token."""
    per_token = {}

    for fill in fills:
        if fill.token_id not in per_token:
            per_token[fill.token_id] = {
                "buys": Decimal("0"),
                "sells": Decimal("0"),
                "net_notional": Decimal("0"),
                "trades": 0,
            }

        notional = fill.price * fill.size
        per_token[fill.token_id]["trades"] += 1

        if fill.side.value == "BUY":
            per_token[fill.token_id]["buys"] += notional
            per_token[fill.token_id]["net_notional"] -= notional
        else:
            per_token[fill.token_id]["sells"] += notional
            per_token[fill.token_id]["net_notional"] += notional

    return per_token
