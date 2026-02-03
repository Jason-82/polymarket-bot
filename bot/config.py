"""Configuration management for the trading bot."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import os
import yaml


class BotMode(str, Enum):
    """Operating mode for the bot."""
    READ_ONLY = "READ_ONLY"  # Ingest data only, no orders
    PAPER = "PAPER"          # Simulate fills, no real orders
    LIVE = "LIVE"            # Real order placement


@dataclass
class RiskLimits:
    """Risk management limits."""
    max_order_size_per_token: Decimal = Decimal("100")
    max_total_open_notional: Decimal = Decimal("1000")
    max_net_exposure_per_market: Decimal = Decimal("500")
    max_total_exposure: Decimal = Decimal("2000")
    max_daily_loss: Decimal = Decimal("100")
    max_drawdown_pct: Decimal = Decimal("0.1")  # 10%
    min_order_update_interval_ms: int = 1000  # 1 second
    price_epsilon: Decimal = Decimal("0.001")  # Don't update if change < epsilon


@dataclass
class StrategyConfig:
    """Configuration for a single strategy."""
    name: str
    enabled: bool = True
    params: Dict[str, Any] = field(default_factory=dict)
    markets: List[str] = field(default_factory=list)  # Allowlist of market_ids
    tokens: List[str] = field(default_factory=list)   # Allowlist of token_ids


@dataclass
class ApiConfig:
    """API endpoint configuration."""
    clob_rest_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"
    geoblock_url: str = "https://polymarket.com/api/geoblock"


@dataclass
class Config:
    """Main configuration container."""
    mode: BotMode = BotMode.READ_ONLY
    chain_id: int = 137  # Polygon mainnet

    # API configuration
    api: ApiConfig = field(default_factory=ApiConfig)

    # Risk limits
    risk: RiskLimits = field(default_factory=RiskLimits)

    # Strategy configurations
    strategies: List[StrategyConfig] = field(default_factory=list)

    # Universe selection
    universe_filters: Dict[str, Any] = field(default_factory=dict)

    # Database path
    database_path: str = "data/polymarket.db"

    # Logging
    log_level: str = "INFO"
    log_file: Optional[str] = None

    # Timing intervals (seconds)
    tick_interval: float = 1.0
    geoblock_check_interval: int = 21600  # 6 hours
    universe_refresh_interval: int = 3600  # 1 hour
    position_reconcile_interval: int = 300  # 5 minutes

    # Alert configuration
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    slack_webhook_url: Optional[str] = None

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load configuration from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls._from_dict(data)

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration with environment variable overrides."""
        config = cls()

        # Mode override
        if mode := os.getenv("BOT_MODE"):
            config.mode = BotMode(mode.upper())

        # Chain ID
        if chain_id := os.getenv("CHAIN_ID"):
            config.chain_id = int(chain_id)

        # Database path
        if db_path := os.getenv("DATABASE_PATH"):
            config.database_path = db_path

        # Log level
        if log_level := os.getenv("LOG_LEVEL"):
            config.log_level = log_level

        # Alert configuration
        config.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        config.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        config.slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")

        # Load strategies from default strategies.yaml if it exists
        strategies_path = Path("config/strategies.yaml")
        if strategies_path.exists():
            try:
                with open(strategies_path) as f:
                    strat_data = yaml.safe_load(f)
                if strat_data and "strategies" in strat_data:
                    config.strategies = [
                        StrategyConfig(
                            name=s["name"],
                            enabled=s.get("enabled", True),
                            params=s.get("params", {}),
                            markets=s.get("markets", []),
                            tokens=s.get("tokens", []),
                        )
                        for s in strat_data["strategies"]
                    ]
            except Exception:
                pass  # Fall back to no strategies

        return config

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Create config from dictionary."""
        config = cls()

        if "mode" in data:
            config.mode = BotMode(data["mode"].upper())

        if "chain_id" in data:
            config.chain_id = data["chain_id"]

        if "api" in data:
            api_data = data["api"]
            config.api = ApiConfig(
                clob_rest_url=api_data.get("clob_rest_url", config.api.clob_rest_url),
                gamma_url=api_data.get("gamma_url", config.api.gamma_url),
                data_api_url=api_data.get("data_api_url", config.api.data_api_url),
                clob_ws_url=api_data.get("clob_ws_url", config.api.clob_ws_url),
                geoblock_url=api_data.get("geoblock_url", config.api.geoblock_url),
            )

        if "risk" in data:
            risk_data = data["risk"]
            config.risk = RiskLimits(
                max_order_size_per_token=Decimal(str(risk_data.get(
                    "max_order_size_per_token", config.risk.max_order_size_per_token
                ))),
                max_total_open_notional=Decimal(str(risk_data.get(
                    "max_total_open_notional", config.risk.max_total_open_notional
                ))),
                max_net_exposure_per_market=Decimal(str(risk_data.get(
                    "max_net_exposure_per_market", config.risk.max_net_exposure_per_market
                ))),
                max_total_exposure=Decimal(str(risk_data.get(
                    "max_total_exposure", config.risk.max_total_exposure
                ))),
                max_daily_loss=Decimal(str(risk_data.get(
                    "max_daily_loss", config.risk.max_daily_loss
                ))),
                max_drawdown_pct=Decimal(str(risk_data.get(
                    "max_drawdown_pct", config.risk.max_drawdown_pct
                ))),
                min_order_update_interval_ms=risk_data.get(
                    "min_order_update_interval_ms", config.risk.min_order_update_interval_ms
                ),
                price_epsilon=Decimal(str(risk_data.get(
                    "price_epsilon", config.risk.price_epsilon
                ))),
            )

        if "strategies" in data:
            config.strategies = [
                StrategyConfig(
                    name=s["name"],
                    enabled=s.get("enabled", True),
                    params=s.get("params", {}),
                    markets=s.get("markets", []),
                    tokens=s.get("tokens", []),
                )
                for s in data["strategies"]
            ]

        if "universe_filters" in data:
            config.universe_filters = data["universe_filters"]

        if "database_path" in data:
            config.database_path = data["database_path"]

        if "log_level" in data:
            config.log_level = data["log_level"]

        if "log_file" in data:
            config.log_file = data["log_file"]

        if "tick_interval" in data:
            config.tick_interval = data["tick_interval"]

        return config

    def validate(self) -> List[str]:
        """Validate configuration, return list of errors."""
        errors = []

        if self.risk.max_order_size_per_token <= 0:
            errors.append("max_order_size_per_token must be positive")

        if self.risk.max_drawdown_pct <= 0 or self.risk.max_drawdown_pct > 1:
            errors.append("max_drawdown_pct must be between 0 and 1")

        if self.tick_interval <= 0:
            errors.append("tick_interval must be positive")

        return errors
