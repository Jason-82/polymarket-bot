# Polymarket Trading Bot - Architecture Document

## Overview

A modular, event-driven trading bot for Polymarket prediction markets with:
- Robust market data ingestion
- Hot-swappable strategy plugins
- Strict risk controls with kill switch
- Paper trading and backtesting harness
- Comprehensive monitoring and audit logging

## Compliance Constraint

**CRITICAL**: The bot must respect Polymarket's geoblock check before any order placement.
If blocked, the bot runs in `READ_ONLY` or `PAPER` mode only - never attempting to trade.

## Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ |
| Trading Client | py-clob-client (official Polymarket SDK) |
| Async Runtime | asyncio |
| HTTP Client | httpx |
| WebSockets | websockets |
| Storage | SQLite (MVP) → PostgreSQL (Phase 2) |
| Config | YAML + environment variables (.env) |
| Logging | Structured JSON logs (structlog) |

## Bot Modes

| Mode | Description |
|------|-------------|
| `READ_ONLY` | Ingest data, build datasets, no orders |
| `PAPER` | Simulate fills against live orderbook, record PnL, no real orders |
| `LIVE` | Real order placement (requires geoblock check pass) |

## Top-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           OBSERVABILITY LAYER                            │
│  (Metrics, Alerts, Dashboard, Audit Logs)                               │
├─────────────────────────────────────────────────────────────────────────┤
│                             CORE ENGINE                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │  Universe   │  │   State     │  │  Strategy   │  │    Risk     │    │
│  │  Selector   │  │   Store     │  │   Runner    │  │   Manager   │    │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘    │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    EXECUTION / OMS                               │    │
│  │  (Order Placement, Cancel, Replace, Idempotent, Paper/Live)      │    │
│  └─────────────────────────────────────────────────────────────────┘    │
├─────────────────────────────────────────────────────────────────────────┤
│                          CONNECTORS LAYER                                │
│  ┌───────────┐  ┌───────────────┐  ┌───────────────┐  ┌─────────────┐  │
│  │  Gamma    │  │  CLOB REST    │  │  CLOB WS      │  │  Data API   │  │
│  │  API      │  │  Client       │  │  Client       │  │  Client     │  │
│  └───────────┘  └───────────────┘  └───────────────┘  └─────────────┘  │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    GEOBLOCK GATE                                 │    │
│  └─────────────────────────────────────────────────────────────────┘    │
├─────────────────────────────────────────────────────────────────────────┤
│                           STORAGE LAYER                                  │
│  (SQLite/PostgreSQL: Markets, Orderbooks, Fills, Positions, Decisions)  │
└─────────────────────────────────────────────────────────────────────────┘
```

## Directory Structure

```
polymarket-bot/
├── bot/                      # Core engine and main entry point
│   ├── __init__.py
│   ├── main.py               # CLI entry point
│   ├── engine.py             # Main event loop and orchestration
│   ├── config.py             # Configuration loading and validation
│   ├── state.py              # In-memory state store
│   └── universe.py           # Market/token universe selection
│
├── connectors/               # External API integrations
│   ├── __init__.py
│   ├── geoblock.py           # Geoblock check implementation
│   ├── gamma_client.py       # Gamma API (market discovery)
│   ├── clob_rest_client.py   # CLOB REST API wrapper
│   ├── clob_ws_client.py     # CLOB WebSocket client
│   └── data_api_client.py    # Data API (positions, trades)
│
├── strategies/               # Strategy plugins
│   ├── __init__.py
│   ├── base.py               # StrategyBase interface
│   ├── loader.py             # Dynamic strategy loading
│   ├── market_maker.py       # Passive two-sided quoter
│   └── value_threshold.py    # Threshold value trader
│
├── risk/                     # Risk management
│   ├── __init__.py
│   ├── manager.py            # Risk checks and clamping
│   ├── kill_switch.py        # Kill switch implementation
│   └── config.py             # Risk limit configuration
│
├── execution/                # Order management system
│   ├── __init__.py
│   ├── oms.py                # Order lifecycle management
│   ├── order_intent.py       # Exchange-agnostic order model
│   └── paper_engine.py       # Paper trading fill simulation
│
├── storage/                  # Data persistence
│   ├── __init__.py
│   ├── database.py           # Database connection and migrations
│   ├── models.py             # SQLAlchemy/dataclass models
│   ├── dao.py                # Data access objects
│   └── migrations/           # Database migrations
│
├── backtest/                 # Backtesting harness
│   ├── __init__.py
│   ├── runner.py             # Backtest execution
│   ├── data_loader.py        # Historical data loading
│   └── metrics.py            # Performance metrics calculation
│
├── monitoring/               # Observability
│   ├── __init__.py
│   ├── logger.py             # Structured logging setup
│   ├── metrics.py            # Metrics collection
│   ├── alerts.py             # Alert dispatching
│   └── dashboard/            # Web dashboard (Streamlit/FastAPI)
│
├── scripts/                  # Utility scripts
│   ├── build_universe_cache.py
│   ├── backfill_price_history.py
│   └── check_geoblock.py
│
├── tests/                    # Test suite
│   ├── unit/
│   ├── integration/
│   └── conftest.py
│
├── config/                   # Configuration files
│   ├── config.yaml           # Main configuration
│   ├── risk.yaml             # Risk limits
│   └── strategies.yaml       # Strategy configurations
│
├── .env.example              # Environment variable template
├── requirements.txt          # Python dependencies
├── pyproject.toml            # Project metadata
├── Dockerfile                # Container image
├── docker-compose.yaml       # Container orchestration
├── ARCHITECTURE.md           # This document
├── README.md                 # Project documentation
├── SECURITY.md               # Security documentation
└── COMPLIANCE.md             # Compliance documentation
```

## Canonical Data Models

### Market (from Gamma API)
```python
@dataclass
class Market:
    market_id: str
    event_id: str
    condition_id: str
    title: str
    slug: str
    description: str
    active: bool
    closed: bool
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    tokens: List[Token]  # YES/NO token IDs
    category: str
    liquidity: Decimal
    volume: Decimal
    last_updated: datetime
```

### Token
```python
@dataclass
class Token:
    token_id: str
    market_id: str
    outcome: str  # "Yes" or "No"
    winner: Optional[bool]
```

### OrderBook
```python
@dataclass
class OrderBookLevel:
    price: Decimal
    size: Decimal

@dataclass
class OrderBook:
    token_id: str
    timestamp: datetime
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]

    @property
    def best_bid(self) -> Optional[Decimal]: ...
    @property
    def best_ask(self) -> Optional[Decimal]: ...
    @property
    def midpoint(self) -> Optional[Decimal]: ...
    @property
    def spread(self) -> Optional[Decimal]: ...
```

### Position
```python
@dataclass
class Position:
    token_id: str
    shares: Decimal
    avg_cost: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    timestamp: datetime
```

### OrderIntent (Strategy Output)
```python
@dataclass
class OrderIntent:
    token_id: str
    side: Literal["BUY", "SELL"]
    price: Decimal
    size: Decimal
    order_type: Literal["GTC", "GTD", "FOK"]
    expiration_ts: Optional[int]
    client_order_id: str
    strategy_name: str
```

### Fill
```python
@dataclass
class Fill:
    fill_id: str
    order_id: str
    client_order_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    price: Decimal
    size: Decimal
    fee: Decimal
    timestamp: datetime
    mode: Literal["PAPER", "LIVE"]
```

### PnL Snapshot
```python
@dataclass
class PnLSnapshot:
    timestamp: datetime
    mode: Literal["PAPER", "LIVE"]
    total_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    max_drawdown: Decimal
    positions: List[Position]
```

## Strategy Plugin Interface

```python
class StrategyBase(ABC):
    """Base class for all trading strategies."""

    name: str

    @abstractmethod
    async def on_tick(
        self,
        context: StrategyContext
    ) -> List[OrderIntent]:
        """Called on each tick/event. Returns desired order intents."""
        pass

    @abstractmethod
    def get_risk_budget(self) -> RiskBudget:
        """Declare the risk budget this strategy requests."""
        pass

    def on_fill(self, fill: Fill) -> None:
        """Optional callback when a fill occurs."""
        pass

    def on_cancel(self, order_id: str) -> None:
        """Optional callback when an order is cancelled."""
        pass


@dataclass
class StrategyContext:
    """Immutable context provided to strategies each tick."""

    timestamp: datetime
    orderbooks: Dict[str, OrderBook]  # token_id -> OrderBook
    positions: Dict[str, Position]     # token_id -> Position
    open_orders: List[Order]
    market_metadata: Dict[str, Market] # market_id -> Market
    account_balance: Decimal
    mode: Literal["READ_ONLY", "PAPER", "LIVE"]
    config: Dict[str, Any]             # Strategy-specific config
```

## Engine Loop Design

The engine uses an event-driven architecture with periodic timer ticks:

```
┌─────────────────────────────────────────────────────────────────┐
│                        ENGINE MAIN LOOP                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. STARTUP                                                      │
│     ├─ Load configuration                                        │
│     ├─ Check geoblock → set allowed modes                        │
│     ├─ Initialize connectors (Gamma, CLOB REST, CLOB WS, Data)  │
│     ├─ Load market universe                                      │
│     ├─ Initialize storage                                        │
│     ├─ Load strategies from config                               │
│     └─ Subscribe to WebSocket channels                           │
│                                                                  │
│  2. EVENT LOOP (asyncio)                                         │
│     ├─ WebSocket Events                                          │
│     │   ├─ Orderbook updates → update state → trigger strategy  │
│     │   ├─ Trade events → update positions                       │
│     │   └─ Order updates → update OMS state                      │
│     │                                                            │
│     ├─ Timer Ticks (configurable interval, e.g., 1s)            │
│     │   ├─ Run strategy.on_tick() for each active strategy      │
│     │   ├─ Risk Manager clamps intents                           │
│     │   ├─ OMS syncs orders (if PAPER/LIVE)                      │
│     │   └─ Persist snapshots to storage                          │
│     │                                                            │
│     ├─ Periodic Tasks                                            │
│     │   ├─ Geoblock recheck (every 6h)                          │
│     │   ├─ Market universe refresh (every 1h)                   │
│     │   ├─ Position reconciliation (every 5m)                   │
│     │   └─ Risk metrics update (every 30s)                      │
│     │                                                            │
│     └─ Kill Switch Check (continuous)                            │
│         ├─ Check circuit breakers                                │
│         ├─ Check manual trigger file/env                         │
│         └─ If triggered → cancel all → halt                      │
│                                                                  │
│  3. SHUTDOWN                                                     │
│     ├─ Cancel all open orders (if LIVE)                         │
│     ├─ Close WebSocket connections                               │
│     ├─ Flush pending writes to storage                           │
│     └─ Final audit log entry                                     │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## API Endpoints Reference

| Service | Base URL | Purpose |
|---------|----------|---------|
| CLOB REST | https://clob.polymarket.com | Trading, orderbook snapshots |
| Gamma REST | https://gamma-api.polymarket.com | Market discovery, metadata |
| Data API | https://data-api.polymarket.com | Positions, activity, trades |
| CLOB WebSocket | wss://ws-subscriptions-clob.polymarket.com/ws/ | Real-time orderbook |
| Geoblock | https://polymarket.com/api/geoblock | Trading eligibility check |

## Definition of Done

### MVP (Phase 1)
- [x] Architecture document complete
- [x] Directory skeleton created
- [ ] READ_ONLY mode working:
  - [ ] Discovers markets via Gamma API
  - [ ] Subscribes to orderbook updates for selected tokens
  - [ ] Persists snapshots to SQLite
  - [ ] CLI status command shows current state
- [ ] Geoblock check implemented and enforced
- [ ] Basic structured logging

### Phase 2
- [ ] PAPER mode with simulated fills
- [ ] Baseline strategies (market maker, value threshold)
- [ ] Risk manager with circuit breakers
- [ ] Kill switch (automatic + manual)
- [ ] Monitoring dashboard
- [ ] Alerts (Telegram/Slack)

### Phase 3
- [ ] LIVE mode (after extensive paper testing)
- [ ] Backtesting harness
- [ ] Performance metrics and attribution
- [ ] PostgreSQL migration option
- [ ] Full deployment automation
