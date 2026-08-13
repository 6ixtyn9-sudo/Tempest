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
- Honest backtest: costs (spread/slippage), chronological split, walk-forward,
  per-horizon follow-through, per-bucket breakdown.
- Nothing live. No broker. No real money. Backtest before anything else.

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
```

## Paper trading (Alpaca PAPER only)

`scripts/paper_trade.py` turns Tempest into a live paper trader:
- Screens the market (TradingView) for the five pillars
- Watches each qualifier for a fresh first-pullback signal (entry bar =
  the latest completed bar — no chasing)
- Submits a DAY bracket on the Alpaca PAPER account: entry limit +
  2R take-profit + stop at the pullback low
- Manages exits (horizon / near-close) and journals every action

Safeguards (non-negotiable):
- `TEMPEST_PAPER=1` must be set or the trader REFUSES to run
- The client is always constructed with `paper=True`
- Risk rails: max open positions (3), max notional/position ($1,000),
  per-symbol cooldown (1h), daily loss cap ($200), halt flag
- Paper account only — never live keys, never live mode

Run locally:
```
PYTHONPATH=src python3 scripts/paper_trade.py --dry-run   # what it would do
PYTHONPATH=src python3 scripts/paper_trade.py              # place on paper
PYTHONPATH=src python3 scripts/attribute_pnl.py            # realized P&L
```

## Automated daily capture (GitHub Actions)

The `.github/workflows/daily_capture.yml` workflow runs the capture loop
twice per trading day (09:30 ET open, 15:30 ET near-close) and on manual
dispatch: screen the market -> log qualifiers -> fetch their 1m bars ->
run the same-day backtest -> commit results. localdata/ is persisted via
the Actions cache. Add this workflow when you push the repo (the scaffold
includes it; just push and enable Actions).

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
scripts/
  build_warehouse.py
  run_backtest.py
  screen_market.py # run the pillars screen, log qualifiers, fetch bars
```

## Doctrine

- Backtest before anything. No live trading, no broker, no real money.
- Fixed-prior thresholds (the pillars are domain priors, not fits).
- No look-ahead: signals use only bars at/before the entry candle.
- Honest costs: microcap spreads/slippage are brutal; the backtest nets them.
- The gate is the product; the video is the hypothesis generator.
