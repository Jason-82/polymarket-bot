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
| `maker` | Rests bids on both YES and NO around fair value, inventory-skewed; posts asks on held inventory. Earns spread + liquidity rewards (+ maker rebates where the venue pays them). Refuses crossed books and YES/NO books that disagree. | on |
| `favorite_yield` | Bids on ≥0.95 outcomes near resolution when the annualised yield clears a threshold. | off |

Adding one: create `pm/strategy/<name>.py`, decorate the class with
`@register("<name>")`, implement `on_tick(ctx) -> list[Intent]`, and add a
block under `strategies:` in `config.yaml`.

## Venues

The engine is venue-agnostic. Set `venue:` in `config.yaml` or pass `--venue`.

| venue | access | fees (per contract) | maker programs |
|---|---|---|---|
| `polymarket` | international CLOB V2, pUSD on Polygon, IP-gated | taker `rate × p(1−p)` by category (0 to 0.07); maker 0 | liquidity rewards + rebate share of taker fees |
| `polymarket_us` | CFTC-regulated, USD, KYC, Ed25519 API keys | taker `0.06 × p(1−p)`; maker rebate **tiered from 2026-10-19**: 0 below $10M/month maker volume (was a flat `0.0125 × p(1−p)` before) | liquidity rewards scored by ticks from best price |

`venue_us.maker_rebate_coef` in `config.yaml` sets the rebate the bot books on
US maker fills; it defaults to 0, the tier every account under $10M/month is
in. `complete_set` is disabled on the US venue: each market is a single
mirrored contract, so YES ask + NO ask is always 1 + spread.

Each venue keeps its own tape (`db_path: data/pm_{venue}.sqlite`) and log
(`data/pm_{venue}.log`), so both can run at once. Check them separately:
`python -m pm status` (international) and `python -m pm --venue us status`.
Tapes recorded before 2026-10-15 are in `data/pm.sqlite`: read them with
`python -m pm status --db data/pm.sqlite` or
`python -m pm backtest --db data/pm.sqlite`.

Complete-set legs fire as a unit: each later leg is sized to what the earlier
legs filled, the set stops at the first leg that fills nothing, and any
remaining imbalance is logged as `group_imbalanced`. Orders found resting on
the venue that the bot does not know about are cancelled: run the bot on a
wallet or account that nothing else trades from.

On Polymarket US each market is one contract with a unified book. The bot
maps it to a YES token (`<slug>|L`) and a synthetic NO token (`<slug>|S`)
whose book is the mirror of the long book, so every strategy runs unchanged.
A NO bid is sent as `BUY_SHORT`; the venue nets long and short into cash.

```bash
python -m pm --venue us probe          # schema, book, balances, order previews (nothing placed), WebSocket
python -m pm --venue us scan           # selected universe with spreads and depth
python -m pm --venue us run --mode paper
```

Keys come from `polymarket.us/developer` and go in `.env` as `PM_US_KEY_ID`
and `PM_US_SECRET_KEY`. The venue caps streaming subscriptions
(`venue_us.ws_max_markets`); other markets are REST-polled.

## Which markets can a small order actually fill in? `measure`

```bash
python -m pm --venue us measure                      # the venue's current tape
python -m pm --venue us measure --db data/archive_optimistic/pm_polymarket_us.sqlite --csv data/measure.csv
```

One block per market (the mirrored short side of a Polymarket US market is the
same book and is not repeated): the median size waiting at the best bid and
ask, contracts traded per hour against each side, how often the spread is 1, 2
or 3+ ticks (and its median in cents, since a 1-cent spread is 10 ticks on a
0.001-tick market), and the hours until a 20-contract order (`--size`) would
fill if it joins the back of the queue, or if it prices one tick better, using
only trading seen while the spread allowed that. A maker needs both sides to
fill, so markets are ranked by the slower side.

Flow comes from trade prints where the tape has them (streamed markets), taken
over the stretch the prints cover but never less than an hour; or from the
venue's traded-volume counter (Polymarket US tapes recorded from 2026-10-18),
over the time it was recorded, calibrated against trade prints where both
exist. One taker order that sweeps several resting orders prints several
trades at once, so prints on the same side less than a second apart count as
one burst. Markets with fewer than 5 bursts are shown but not ranked. Markets with no trade data at all show only
book churn, which is not trading, and get no fill time. Reports open tapes
read-only and never modify them.

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
- **Resting orders join the back of the queue.** Trades at our price consume
  the displayed size ahead of us first. Size that disappears without a trade
  counts as cancellations spread through the queue. A trade through our price,
  the far side reaching our price, or our level vanishing while the price moves
  away fills us completely. That last rule is deliberately pessimistic: it is
  where adverse selection shows up.
- Until 2026-10-17 queue position was ignored. On deep books that turned small
  prints into fills and made the maker look profitable. Tapes recorded before
  then can be re-scored with the current model:
  `python -m pm --venue us backtest --db <old tape>`.
- On Polymarket US, YES and NO are two sides of one contract, so buying one
  side while holding the other closes the held side, as on the venue.
- Paper restarts resume from the tape's paper fills (`execution.paper_resume`;
  live fills on the same tape are never replayed into paper). To start a fresh
  experiment, stop the bot (`Ctrl+C`, wait for `engine_stopped`) and move the
  tape together with its `-wal`/`-shm` side files into a folder:
  `mkdir -p data/archive && mv data/pm_polymarket_us.sqlite* data/archive/`.
  The archived tape can still be re-scored with `backtest --db`.
- On mirrored venues the maker posts no separate inventory ask: the bid on the
  other side is the unwind order at that price, and the risk gate charges it
  only for the part that would open a new position.
- Simultaneous mirrored orders share one queue in the simulator, a print is
  counted once even if the book showing it arrives first, and empty or crossed
  snapshots never fill anything.
- Markouts compare each fill with the midpoint just before it, then with the
  midpoint 30 s and 300 s later. A horizon the tape did not cover is shown as
  `n/a`, never as zero; `cov%` says how much was measured.

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
