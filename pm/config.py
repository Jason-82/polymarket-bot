"""Configuration: config.yaml for behaviour, .env for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .models import D, Mode


@dataclass
class UniverseConfig:
    refresh_seconds: int = 600
    max_markets: int = 40
    min_liquidity_usd: Decimal = Decimal("5000")
    min_volume_24h_usd: Decimal = Decimal("2000")
    min_days_to_resolution: float = 2
    max_days_to_resolution: float = 120
    exclude_tags: list[str] = field(default_factory=lambda: ["sports"])
    include_neg_risk: bool = True
    max_event_markets: int = 12      # neg-risk events larger than this are not completed
    prefer_rewards: bool = True      # rank incentivised markets first (by daily reward pool)
    min_reward_rate_per_day: Decimal = Decimal("0")   # optional floor when prefer_rewards
    condition_ids: list[str] = field(default_factory=list)


@dataclass
class RiskConfig:
    max_notional_per_market_usd: Decimal = Decimal("40")
    max_total_notional_usd: Decimal = Decimal("200")
    max_open_orders: int = 30
    daily_loss_limit_usd: Decimal = Decimal("20")
    min_cash_reserve_usd: Decimal = Decimal("20")
    min_hours_to_resolution: float = 24
    kill_switch_file: str = "KILL"
    require_geoblock_ok_for_live: bool = True


@dataclass
class ExecutionConfig:
    requote_epsilon: Decimal = Decimal("0.005")   # ignore price moves smaller than this
    min_seconds_between_requotes: float = 3.0
    taker_retry_seconds: float = 5.0              # cooldown before re-firing the same taker tag
    paper_starting_cash_usd: Decimal = Decimal("300")
    fill_poll_seconds: float = 2.0                # live: order-status poll cadence without the user feed
    reconcile_seconds: float = 20.0               # live: poll cadence while the user feed is connected
    size_change_band: Decimal = Decimal("0.25")   # replace a resting order only if size differs by more than this fraction


@dataclass
class VenueUSConfig:
    gateway_url: str = "https://gateway.polymarket.us"
    api_url: str = "https://api.polymarket.us"
    ws_url: str = "wss://api.polymarket.us"
    ws_max_markets: int = 10          # the venue caps streaming; the rest are REST-polled
    book_poll_seconds: float = 5.0
    max_book_candidates: int = 120    # how many pre-filtered markets to fetch books for when selecting
    prefilter_max_spread: Decimal = Decimal("0.15")
    prefilter_band: list = field(default_factory=lambda: [Decimal("0.05"), Decimal("0.95")])


@dataclass
class AlertsConfig:
    feed_down_seconds: float = 60.0
    on_fills: bool = True
    heartbeat_hours: float = 6.0                  # 0 disables the periodic "alive" message


@dataclass
class Secrets:
    private_key: str = ""
    funder: str = ""
    signature_type: int = 0
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    live_trading_ack: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    us_key_id: str = ""
    us_secret_key: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            telegram_bot_token=os.environ.get("PM_TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("PM_TELEGRAM_CHAT_ID", "").strip(),
            us_key_id=os.environ.get("PM_US_KEY_ID", "").strip(),
            us_secret_key=os.environ.get("PM_US_SECRET_KEY", "").strip(),
            private_key=os.environ.get("PM_PRIVATE_KEY", "").strip(),
            funder=os.environ.get("PM_FUNDER", "").strip(),
            signature_type=int(os.environ.get("PM_SIGNATURE_TYPE", "0") or 0),
            api_key=os.environ.get("PM_CLOB_API_KEY", "").strip(),
            api_secret=os.environ.get("PM_CLOB_API_SECRET", "").strip(),
            api_passphrase=os.environ.get("PM_CLOB_API_PASSPHRASE", "").strip(),
            live_trading_ack=os.environ.get("LIVE_TRADING", "").strip().lower() == "yes",
        )


@dataclass
class Config:
    venue: str = "polymarket"      # polymarket | polymarket_us
    mode: Mode = Mode.PAPER
    tick_seconds: float = 1.0
    db_path: str = "data/pm.sqlite"
    log_level: str = "INFO"
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    venue_us: VenueUSConfig = field(default_factory=VenueUSConfig)
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)
    secrets: Secrets = field(default_factory=Secrets)

    # Public endpoints
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    chain_id: int = 137

    @classmethod
    def load(cls, path: str | Path = "config.yaml", env_path: str | Path = ".env") -> "Config":
        load_dotenv(env_path, override=False)
        raw: dict[str, Any] = {}
        p = Path(path)
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = cls()
        cfg.venue = str(raw.get("venue", cfg.venue)).lower()
        cfg.mode = Mode(str(raw.get("mode", cfg.mode.value)).lower())
        cfg.tick_seconds = float(raw.get("tick_seconds", cfg.tick_seconds))
        cfg.db_path = str(raw.get("db_path", cfg.db_path))
        cfg.log_level = str(raw.get("log_level", cfg.log_level)).upper()
        cfg.universe = _build(UniverseConfig, raw.get("universe") or {})
        cfg.risk = _build(RiskConfig, raw.get("risk") or {})
        cfg.execution = _build(ExecutionConfig, raw.get("execution") or {})
        cfg.alerts = _build(AlertsConfig, raw.get("alerts") or {})
        cfg.venue_us = _build(VenueUSConfig, raw.get("venue_us") or {})
        cfg.strategies = dict(raw.get("strategies") or {})
        cfg.secrets = Secrets.from_env()
        return cfg

    def strategy(self, name: str) -> dict[str, Any]:
        return dict(self.strategies.get(name) or {})


def _build(cls, data: dict[str, Any]):
    """Instantiate a dataclass from a dict, coercing Decimal fields."""
    obj = cls()
    for k, v in data.items():
        if not hasattr(obj, k):
            continue
        current = getattr(obj, k)
        if isinstance(current, Decimal):
            v = D(v)
        elif isinstance(current, bool):
            v = bool(v)
        elif isinstance(current, int) and not isinstance(current, bool):
            v = int(v)
        elif isinstance(current, float):
            v = float(v)
        elif isinstance(current, list) and current and isinstance(current[0], Decimal) and isinstance(v, list):
            v = [D(x) for x in v]
        setattr(obj, k, v)
    return obj
