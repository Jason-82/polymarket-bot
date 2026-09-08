# pm — a maker-first Polymarket bot

Built from first principles for a small account. Read [DESIGN.md](DESIGN.md)
for the reasoning; the short version:

- Polymarket now charges **taker** fees and pays **makers** (zero fees,
  rebates, liquidity rewards). So the bot rests orders; it only crosses the
  spread when the payoff is arithmetic (YES + NO < $1).
- Strategies are pure functions; the same code runs in paper, live and
  backtest. Everything is recorded to SQLite so expectancy can be measured
  instead of guessed.
- Risk limits are in dollars and are enforced, not suggested.

## Install (Windows / Git Bash shown; Linux and macOS are the same minus the path)

```bash
git clone <repo> && cd polymarket-bot
python -m venv venv
source venv/Scripts/activate          # Windows Git Bash;  Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # edit later; not needed for paper mode
```

Python 3.11+.

## First run: three safe commands

```bash
python -m pm probe          # connectivity, geoblock status, fee info, a live order book, WebSocket check
python -m pm scan           # universe + YES/NO ask sums after fees; shows any structural arb right now
python -m pm run --mode read_only   # record the tape, run strategies "on paper only" (no simulated fills)
```

Then paper trade:

```bash
python -m pm run --mode paper
python -m pm status         # tape size, fills, rejections, latest equity
python -m pm backtest --since-hours 24
```

Stop with `Ctrl+C`. Create a file named `KILL` in the working directory to
cancel everything and halt without stopping the process; delete it to resume.

## Configuration

`config.yaml` — mode, universe filters, dollar risk limits, execution
throttles and strategy parameters (each has comments). `.env` — secrets only.

Defaults are sized for a **$300 account**: $40 per market, $200 total,
$20/day loss limit, 10-share quotes.

## Strategies

| name | what it does | default |
|---|---|---|
| `complete_set` | Buys YES+NO (or every YES of a complete neg-risk event) when the asks plus taker fees sum below $1. | on |
| `maker` | Rests bids on both YES and NO around fair value, inventory-skewed; posts asks on held inventory. Earns spread + liquidity rewards + maker rebates. | on |
| `favorite_yield` | Bids on ≥0.95 outcomes near resolution when the annualised yield clears a threshold. | off |

Adding one: create `pm/strategy/<name>.py`, decorate the class with
`@register("<name>")`, implement `on_tick(ctx) -> list[Intent]`, and add a
block under `strategies:` in `config.yaml`.

## Measuring whether it works: markouts

`python -m pm status` prints, per strategy and per token, three numbers in
cents per share:

- **capture**: how far inside the midpoint we were filled (the spread we earned).
- **drift 30s / 300s**: how the midpoint moved after the fill. Negative means
  the market moved against us, i.e. we were picked off.
- **net**: capture plus drift. This is the number that decides whether a
  market is worth quoting.

Run it with `--since-hours 24` to look at one day. Tokens with persistently
negative net belong in `exclude` lists or need a wider spread.

## Scale features

- **Reward-aware universe.** With `prefer_rewards: true` the bot pulls the
  liquidity-rewards program and ranks incentivised markets first by daily
  pool. In those markets the maker quotes inside the qualifying spread at the
  qualifying size, so resting orders score every second whether or not they
  fill.
- **Queue-position-aware quoting.** A resting order within `hold_within` of
  the new target is kept rather than replaced; the OMS also ignores partial
  fills and size changes under `size_change_band`. Every cancel/replace goes
  to the back of the queue.
- **Live fills from the user WebSocket channel**, with periodic polling as a
  reconciliation fallback.
- **Telegram alerts** (set `PM_TELEGRAM_BOT_TOKEN` and `PM_TELEGRAM_CHAT_ID`
  in `.env`): start/stop, kill switch, feed down, daily loss halt, fills, and
  a periodic heartbeat.

## How paper fills work (read this before trusting paper P&L)

- Taker orders fill by walking the live book, with the venue fee applied.
- Post-only orders that would cross are rejected, as on the venue.
- Resting orders fill when a real trade prints at or through their price, or
  the far side of the book moves through them. **Queue position is ignored**,
  so paper is an upper bound on maker fills.

## Going live

1. Fund a Polygon wallet with pUSD on Polymarket and note the address that
   holds the collateral (`PM_FUNDER`) and its type (`PM_SIGNATURE_TYPE`:
   0 = plain EOA, 1 = Polymarket proxy, 2 = Polymarket Gnosis safe).
2. Put the signing key in `.env` as `PM_PRIVATE_KEY`. On first live start
   the bot derives CLOB API credentials and prints them; paste them into
   `.env` so it does not have to derive them again.
3. Set `mode: live` in `config.yaml` **and** `LIVE_TRADING=yes` in `.env`.
   Both are required.
4. Start small: lower `max_total_notional_usd` and `max_notional_per_market_usd`
   to a few dollars for the first session.

On start and stop the bot cancels **all** open orders for the account — it
assumes it is the only thing trading from that wallet. `pm probe` reports
geoblock status; order endpoints are IP-gated by Polymarket and jurisdiction
is your responsibility.

## Layout

```
pm/
  models.py      Book, Market, Intent, Order, Fill, Position (Decimal everywhere)
  config.py      config.yaml + .env
  fees.py        taker fee = shares × rate × (p(1−p))^e ; unknown rate → worst case
  gamma.py       market discovery
  clob.py        REST books / fee info / rewards ; geoblock
  feed.py        WebSocket /ws/market → books + trades
  universe.py    market selection; completes neg-risk events
  state.py       MarketState, Portfolio, Context (what strategies see)
  strategy/      complete_set, maker, favorite_yield
  risk.py        dollar caps, daily loss, resolution window, kill switch
  oms.py         diff desired intents vs open orders
  execution/     Recorder (read_only), PaperExchange, LiveExchange (py-clob-client-v2)
  store.py       SQLite tape
  backtest.py    replay tape through the same paper stack
  engine.py      the loop
tests/           unit tests (no network)
```

## Tests

```bash
pytest -q
```
