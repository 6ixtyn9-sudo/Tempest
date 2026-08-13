# Tempest

US microcap momentum research lab. Screen for high-demand setups, backtest the
follow-through honestly, let the data — not the hype — decide what trades.

## The thesis being tested (not assumed)

Source methodology: Ross Cameron / Warrior Trading momentum day-trading course
(2026-08). Five selection pillars (demand) + one entry pattern (the first
pullback):

1. Relative volume >= 5x the 50-day average (his money metric; >20x = mania)
2. High total volume (millions of shares)
3. Gapping up >= 2% from the prior close (news catalyst usually behind it)
4. Price $2-20 (low price = room for large percentage moves)
5. Float < 20M shares (the supply pillar; lower is better)

**Reading the screen's numbers.** The scanner's `gap_pct` and `relvol` come
from a delayed feed and are computed against the *prior close*, not a live
print. Treat them as a ranking signal, not a quote. Observed 2026-08-13:
DFSC at 969x relvol / 108% gap, FGI at 289x / 79% — both far beyond the
>20x "mania" band above, which the course never intended as a tradable
range. Extreme readings usually mean a thin float reacting to news, a
halt-and-resume, or a reverse split, and the printed gap can be stale by
minutes. Before trusting a fill on any such symbol, check the live quote
and the halt status independently; the journal records what the broker
returned, not whether the price was real.

Entry: squeeze up -> shallow pullback (retrace < 50%, holds VWAP and the 9 EMA,
green volume > red volume) -> first candle to make a new high. Stop = low of the
pullback; target >= 2:1 reward:risk.

**Tempest treats these as hypotheses, not rules.** The backtest measures whether
follow-through actually pays after costs, and the bucket analysis looks for
structure the course never quantified (time-of-day, gap size x relvol, float,
news vs no-news, horizon).

## What it does (current)

- Data adapter: yfinance 1-minute bars (pilot, ~30 days) behind a common
  interface; Polygon adapter slots in later for real history.
- Video-rule replication: mechanical 5-pillar screen + first-pullback detector.
- Honest backtest: costs (spread/slippage), as-of pillar screen and dated
  float evidence (no look-ahead), per-horizon follow-through, per-bucket
  breakdown, and committed dated JSON reports.
- Alpaca PAPER only (never live keys). No real money until paper shows an edge.

## Quickstart

```
python3 -m pip install -r requirements.txt
PYTHONPATH=src python3 -m pytest -q
# 1. Screen the whole market for the five pillars (TradingView backend)
PYTHONPATH=src python3 scripts/screen_market.py --fetch-bars
# 2. Backtest the qualifying sessions (the rare movers, logged daily)
PYTHONPATH=src python3 scripts/run_backtest.py
```

The five-pillar screen runs server-side on TradingView's scanner and returns
the rare qualifying movers (gap >= 2%, relvol >= 5x, price $2-20, float < 20M,
volume > 1M) in one request. Each day's screen is logged to
localdata/screen_log.csv so evidence accumulates over time.

## Paper trading (Alpaca PAPER only)

`scripts/paper_trade.py` turns Tempest into a live paper trader:
- Screens the market (TradingView) for the five pillars
- Watches each qualifier for a recent first-pullback signal (entry bar within
  `TEMPEST_SIGNAL_MAX_AGE_BARS`, default 5, of the latest completed bar)
- Re-prices a stale signal at the current bar and skips it if price has
  fallen below the stop (setup broken) or run more than
  `TEMPEST_MAX_ENTRY_SLIPPAGE` (default 1%) past the signal (chasing)
- Requires a current-day RTH bar no more than `TEMPEST_MAX_BAR_AGE_MINUTES`
  old (default 10); premarket, after-hours and prior-session bars cannot fire
- Blocks new entries inside the final 30 minutes of regular trading
- Submits a DAY bracket on the Alpaca PAPER account: entry limit +
  2R take-profit + stop at the pullback low
- Journals order submission separately from confirmed entry/exit fills
- Manages exits (horizon / near-close) and journals every action

Safeguards (non-negotiable):
- `TEMPEST_PAPER=1` must be set or the trader REFUSES to run
- The client is always constructed with `paper=True`
- Risk rails: max open positions (3), max notional/position ($1,000),
  max stop-risk/position ($50), per-symbol cooldown (1h), daily loss cap
  ($200), halt flag
- Position/order/close API failures stop the pass; unknown broker state never
  becomes an empty portfolio or an invented fill
- Paper account only — never live keys, never live mode

Run locally:
```
PYTHONPATH=src python3 scripts/paper_trade.py --dry-run   # what it would do
PYTHONPATH=src python3 scripts/paper_trade.py              # place on paper
PYTHONPATH=src python3 scripts/attribute_pnl.py            # realized P&L
```

## Automated daily capture (GitHub Actions)

Two workflows, both `workflow_dispatch` only — there is no GitHub `schedule:`
anywhere in this repo. GitHub's cron is queued rather than guaranteed and
drifts 5-30 minutes under load, which is fatal for a signal that lives a
handful of bars. Scheduling is external: cron-job.org posts to the
`workflow_dispatch` API.

- `daily_capture.yml` — screen -> log qualifiers -> fetch 1m bars -> same-day
  backtest -> commit. Dispatched at the open and near the close.
- `paper_poll.yml` — scan-only. Runs `paper_trade.py` and nothing else, every
  5 minutes during RTH, so a first pullback is seen while it is still
  actionable. A 5-minute poll with the default 5-bar window catches ~100% of
  signals; at 3x/day it caught none.

Both share `concurrency: group: tempest-paper` with `cancel-in-progress:
false`, so a poll dispatched mid-capture queues instead of colliding on the
git push. Each workflow re-checks the ET window itself and exits in seconds
outside 09:30-16:00 ET, so a misconfigured scheduler cannot trade off-hours.

### Reading localdata/paper_status.csv

Every pass appends rows here, because Actions logs need a sign-in and a
missing `trade_journal.csv` only means no order fired:

| stage | meaning |
|---|---|
| `client_ok` | paper client constructed (does NOT prove credentials work) |
| `equity,<amount>` | authenticated; the amount identifies the account |
| `auth_failed` | credentials rejected — the run fails loudly |
| `equity_failed` | broker/network error — the run fails loudly |
| `state_failed` | positions/orders/lifecycle unknown — pass stops with no new order |
| `no_candidates` | screen returned zero qualifiers (normal, exit 0) |
| `pass_done ... entries=watching=N` | real pass, no signal (normal) |
| `skip_watching,SYM: reason` | why each candidate was passed over |

An Alpaca key ID is ~26 chars beginning `PK`; the secret is ~44 chars. Paper
and live keys are not interchangeable and `get_trading_client` is hardcoded
`paper=True`, so live keys always 401.

## Layout

```
src/tempest/
  config.py        # paths, env, symbol hygiene
  warehouse.py     # parquet warehouse, dedup, keep-last
  features.py      # gap_open, relvol, 9EMA, VWAP, day-session labels
  strategy.py      # 5 pillars + first-pullback detector (mechanical)
  validation.py    # cost adjustment, chronological split, walk-forward
  backtest.py      # event simulation + per-bucket aggregation
  sources/
    base.py        # BarSource interface (yfinance now, Polygon later)
    yfinance_1m.py # pilot adapter (7-day chunked)
    tradingview.py # five-pillar live screener (scanner.tradingview.com)
    finviz.py      # fallback scraper stub (deferred)
  risk.py          # risk rails, journal, halt flag
  broker.py        # Alpaca paper client, equity, bracket orders
  trader.py        # PaperTrader: refresh -> manage exits -> try entry
scripts/
  build_warehouse.py
  run_backtest.py
  screen_market.py    # run the pillars screen, log qualifiers, fetch bars
  paper_trade.py      # one paper-trading pass
  capture_daily.sh    # screen + backtest + commit evidence
  commit_evidence.sh  # per-file `git add -f`, pull-rebase-push with retry
  attribute_pnl.py    # realized P&L from the journal
.github/workflows/
  daily_capture.yml   # screen + backtest, dispatched twice daily
  paper_poll.yml      # scan-only paper pass, dispatched every 5 min in RTH
```

## Doctrine

- Backtest before anything. Paper is allowed; live money is not.
- Fixed-prior thresholds (the pillars are domain priors, not fits).
- No look-ahead: signals use only bars at/before the entry candle.
- Honest costs: microcap spreads/slippage are brutal; the backtest nets them.
- The gate is the product; the video is the hypothesis generator.
