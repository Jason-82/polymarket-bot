# Design — from first principles

This document is the reasoning behind v2. Read it before the code: every
structural choice below follows from a small number of facts about how
Polymarket actually works in 2026 and how much capital is in play.

## 1. What a Polymarket market is

- A binary market has two conditional tokens, YES and NO. At resolution
  exactly one pays $1, the other $0. Therefore **at all times YES + NO
  is worth exactly $1**, and a price is a probability.
- A "negative-risk" event (e.g. "who wins the nomination") is a set of
  binary markets whose YES tokens are mutually exclusive: **Σ YES = $1**
  across the set, *unless* the event is "augmented" (a placeholder
  outcome can still be added), in which case the sum may legitimately be
  < 1.
- Trading is on a central limit order book (CLOB V2, live since
  2026‑04‑28). Prices move in ticks (0.01 normally, finer near 0/1).
  Collateral is pUSD on Polygon.
- **Makers pay zero fees and receive rebates. Takers pay**
  `fee = shares × rate × (p·(1−p))^e` per fill, where `rate` depends on the
  market category (crypto 0.07, sports 0.05, politics/finance 0.04,
  economics/culture 0.05, geopolitics 0). At p = 0.50 with rate 0.04 that
  is **1¢/share = 2 % of notional** — on a round trip, 4 %.
- Polymarket also pays **liquidity rewards** for resting orders near the
  midpoint, and **maker rebates** (a share of taker fees) when resting
  orders are filled. Both pay daily above a $1 threshold.
- Order-placement endpoints are IP-gated by jurisdiction; read-only
  endpoints are not.

## 2. Where money can come from

Prediction markets are zero-sum before fees. Every dollar a strategy
earns is paid by a counterparty who was **wrong, slow, impatient, or
paying for liquidity**. That gives exactly four sources of edge:

| Source | Who pays you | What you need | Verdict for us |
|---|---|---|---|
| **Impatience** (market making) | Takers crossing the spread; Polymarket's reward programs | Patience, inventory discipline, fast quote pulls | **Primary.** Fees make taking expensive and making free; the venue literally subsidises this. |
| **Structure** (complete-set / neg-risk arbitrage) | Whoever left YES+NO < $1 on the book | Speed. Bots clear these in seconds. | **Secondary / canary.** Cheap to detect; rarely fillable by us; excellent first live test because payoff is arithmetic, not opinion. |
| **Behaviour** (longshot bias, near-resolution favourites) | Traders who overpay for lottery tickets or ignore time value | Patience, capital lockup, tail-risk tolerance | **Optional.** Real but slow; disabled by default. |
| **Information / speed** (news) | Traders slower than you | Sub-second feeds, pre-mapped markets, taker execution | **Not built.** RSS is minutes late; an LLM call is seconds; taker fees eat a 3¢ edge. This is the least likely source of edge for a small, non-colocated account. |

The earlier version of this project was built around the fourth row.
Re-deriving from the fee schedule, that is backwards: **the default
execution style must be maker-side**, and information (if ever added)
should *skew maker quotes*, not trigger taker orders.

## 3. What a few hundred dollars can realistically do

Absolute P&L on $300 will be small regardless of strategy. So the honest
product goal is not "make money", it is:

1. **Don't lose the $300** while learning (hard dollar caps, kill switch,
   never cross the spread unless the payoff is arithmetic).
2. **Measure real expectancy.** Record every book, decision, order and
   fill so that after a few weeks we can answer, with data, whether any
   strategy is positive net of fees. Paper trading first; then live with
   the same code path.
3. **Be ready to scale** if the answer is yes — the code should not need
   rewriting to raise limits.

## 4. Architecture that follows

```
 Gamma (discovery) ─┐
                    ├─► Universe ─► MarketState ◄── WebSocket /ws/market (books, trades)
 CLOB REST (fees,   │                  │
  tick, neg-risk) ──┘                  ▼
                              Strategies (pure functions)
                              Context → [Intent]
                                       │
                                       ▼
                               Risk gate (dollars)
                                       │
                                       ▼
                              OMS (diff desired vs open)
                                       │
                 ┌─────────────────────┼──────────────────────┐
                 ▼                     ▼                      ▼
           Recorder             PaperExchange           LiveExchange
          (READ_ONLY)        (fills vs live tape)     (py-clob-client-v2)
                 └─────────────────────┴──────────────────────┘
                                       ▼
                                   SQLite store  ─► backtest replay / status
```

Principles:

- **One loop, one state.** The WebSocket feed owns the books; everything
  else reads from `MarketState`. No component makes its own market-data
  calls mid-tick.
- **Strategies are pure.** `on_tick(Context) → list[Intent]`. No I/O, no
  clocks, no globals. The same function runs in paper, live and backtest.
- **Intents are declarative.** A strategy says "I want a bid of 20 shares
  at 0.43 tagged `maker:TOKEN:BUY`". The OMS diffs that against open
  orders and issues the minimum cancels/places. Strategies never manage
  order IDs.
- **Risk is in dollars and is a gate, not advice.** Per-market notional,
  total notional, open-order count, daily loss, cash reserve, time-to-
  resolution, geoblock, kill-switch file. An intent that fails any gate is
  dropped and logged with the reason.
- **Maker by default.** Every intent is `post_only=True` unless the
  strategy explicitly declares a taker order, and taker orders carry the
  fee in their expected-value calculation.
- **Same execution interface in all three modes.** Paper fills are
  simulated against the *live tape*: a resting bid fills when a real
  trade prints at or below its price. This is slightly optimistic (queue
  position is ignored) and is documented as such.
- **Record everything.** Books (top of book + depth), trades, intents,
  risk rejections, orders, fills, P&L marks. Backtests replay the
  recorded tape; they are not a separate simulator.
- **Small dependency surface.** `httpx`, `websockets`, `pyyaml`,
  `python-dotenv`, and `py-clob-client-v2` (only imported in live mode).
  Standard-library logging. Runs on Windows (no Unix signal handlers).

## 5. The strategies

### `complete_set` (structure)
For each binary market: `cost = ask_yes + ask_no + taker_fee(ask_yes) + taker_fee(ask_no)`.
If `cost < 1 − margin`, buy both legs at the ask (FAK). Holding both to
resolution returns exactly $1. For non-augmented neg-risk events, the same
with `Σ ask_yes_i`. It also logs the *sell* side (`bid_yes + bid_no > 1`)
which would require an on-chain split and is not executed in v2.

### `maker` (impatience)
For each eligible market, rest **bids on both YES and NO**. A NO bid at
`1 − p` is economically an ask on YES; if both fill you hold a complete
set that cost `1 − spread` and pays $1. Quotes sit `half_spread` from a
fair value (midpoint, skewed by inventory) and are pulled when the book is
stale, too thin, too wide, too close to 0/1, or too close to resolution.
Size is small and inventory-capped. This is the strategy the venue's
reward programs are designed to pay.

### `favorite_yield` (behaviour, off by default)
Post bids on YES ≥ 0.95 in markets resolving within N days when the
annualised yield clears a threshold. Capital lockup and tail risk are the
costs; the config caps both.

## 6. What is deliberately not here

- No LLM/news pipeline (see §2). The clean way to add one later is a
  `FairValueProvider` that the maker consults to skew quotes.
- No on-chain split/merge/redeem. Complete sets are held to resolution.
- No cross-venue arbitrage.
- No Twitter.

## 7. Operating notes

- `probe` first: it verifies connectivity, geoblock status and fee info
  for one market and prints the live book.
- `scan` runs the structural check once across the universe without
  trading — a safe, informative first run.
- `run --mode read_only` records the tape. `paper` simulates. `live`
  trades. Nothing in `live` is reachable without `LIVE_TRADING=yes` in the
  environment *and* `mode: live` in config.
- Jurisdictional access is your responsibility. Polymarket's order
  endpoints are IP-gated; a regulated US venue (Polymarket US) exists with
  a separate API and is not targeted by this code.
