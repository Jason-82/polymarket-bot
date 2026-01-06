"""Risk configuration utilities."""

from pathlib import Path
from typing import Any, Dict, Optional
from decimal import Decimal
import yaml

from bot.config import RiskLimits


def load_risk_config(path: Path) -> RiskLimits:
    """Load risk limits from YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)

    return RiskLimits(
        max_order_size_per_token=Decimal(str(data.get("max_order_size_per_token", 100))),
        max_total_open_notional=Decimal(str(data.get("max_total_open_notional", 1000))),
        max_net_exposure_per_market=Decimal(str(data.get("max_net_exposure_per_market", 500))),
        max_total_exposure=Decimal(str(data.get("max_total_exposure", 2000))),
        max_daily_loss=Decimal(str(data.get("max_daily_loss", 100))),
        max_drawdown_pct=Decimal(str(data.get("max_drawdown_pct", 0.1))),
        min_order_update_interval_ms=data.get("min_order_update_interval_ms", 1000),
        price_epsilon=Decimal(str(data.get("price_epsilon", 0.001))),
    )


def get_default_risk_config() -> Dict[str, Any]:
    """Get default risk configuration as dict."""
    return {
        "max_order_size_per_token": 100,
        "max_total_open_notional": 1000,
        "max_net_exposure_per_market": 500,
        "max_total_exposure": 2000,
        "max_daily_loss": 100,
        "max_drawdown_pct": 0.1,
        "min_order_update_interval_ms": 1000,
        "price_epsilon": 0.001,
    }
