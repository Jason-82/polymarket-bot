# Polymarket Trading Bot

A modular, event-driven trading bot for Polymarket prediction markets with hot-swappable strategy plugins, strict risk controls, and paper trading capabilities.

## Features

- **Market Data Ingestion**: Real-time orderbook updates via WebSocket with REST fallback
- **Modular Strategy Plugins**: Hot-swappable strategies without code changes
- **Strict Risk Controls**: Pre-trade validation, exposure limits, and circuit breakers
- **Kill Switch**: Automatic and manual trading halt with full order cancellation
- **Paper Trading**: Simulate execution against live orderbooks without real orders
- **Backtesting**: Test strategies against historical data (coming soon)
- **Monitoring**: Structured JSON logs, metrics, and alerts (Telegram/Slack)
- **SQLite Storage**: Persist market data, decisions, and PnL for analysis

## Quick Start

### Prerequisites

- Python 3.10+
- VPN connection (if trading from a geo-restricted region)

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd polymarket-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your settings (optional for READ_ONLY mode)
```

### Running the Bot

```bash
# Check geoblock status first
python -m bot.main --check-geoblock

# Run demo (fetch markets, subscribe to WebSocket for 30s)
python -m bot.main --demo

# Run in READ_ONLY mode (safe - no trading)
python -m bot.main --mode READ_ONLY

# Run in PAPER mode (simulated trading)
python -m bot.main --mode PAPER

# Show current status
python -m bot.main --status

# List available markets
python -m bot.main --list-markets
```

## Bot Modes

| Mode | Description |
|------|-------------|
| `READ_ONLY` | Ingest market data and build datasets. No orders placed. **Default and safest mode.** |
| `PAPER` | Simulate order execution against live orderbooks. Track PnL without real money. |
| `LIVE` | Real order placement. **Only use after extensive paper trading!** |

## Important Compliance Notice

This bot **respects Polymarket's geoblock restrictions**. If you are in a geo-restricted region:

1. The bot will automatically detect this on startup
2. LIVE mode will be disabled
3. You can still use READ_ONLY and PAPER modes
4. **Do not attempt to circumvent geo-restrictions**

## Project Structure

```
polymarket-bot/
├── bot/              # Core engine and CLI
├── connectors/       # Polymarket API integrations
├── strategies/       # Trading strategy plugins
├── risk/             # Risk management
├── execution/        # Order management system
├── storage/          # Database and persistence
├── backtest/         # Backtesting harness
├── monitoring/       # Logging and alerts
├── config/           # Configuration files
└── tests/            # Test suite
```

## Strategies

Two baseline strategies are included as scaffolding:

### Market Maker
- Quotes bid/ask around midpoint with configurable spread
- Inventory skew to manage position risk
- **Not profitable out of the box** - for experimentation only

### Value Threshold
- Trades when price deviates from fair value estimate
- Requires you to provide fair value estimates
- **Not profitable out of the box** - for experimentation only

### Adding Custom Strategies

1. Create a new file in `strategies/`
2. Inherit from `StrategyBase`
3. Implement `on_tick()` and `get_risk_budget()`
4. Register with `@register_strategy("my_strategy")`
5. Add to `config/strategies.yaml`

## Risk Management

The bot includes multiple safety layers:

- **Pre-trade checks**: Price bounds, tick size, size limits
- **Exposure limits**: Per-market and total portfolio caps
- **Circuit breakers**: Max daily loss, max drawdown
- **Kill switch**: Automatic (on limits) or manual (file/env)

When the kill switch triggers:
1. All open orders are cancelled
2. Trading loop halts
3. Alerts are sent (if configured)

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy .

# Linting
ruff check .
```

## Security

- Never commit `.env` or private keys
- Use a dedicated trading wallet with limited funds
- Start with READ_ONLY mode to understand the system
- Paper trade extensively before going live
- Set conservative risk limits initially

See [SECURITY.md](SECURITY.md) for detailed security guidelines.

## Disclaimer

This software is provided for educational and research purposes. Trading prediction markets involves significant risk. You can lose money. The authors are not responsible for any financial losses incurred while using this software.

## License

MIT License - see LICENSE file for details.
