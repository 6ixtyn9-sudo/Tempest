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
# fetch 1m bars for a small universe (<=30 days via yfinance; the adapter
# chunks requests into 7-day windows to respect Yahoo's server cap)
PYTHONPATH=src python3 scripts/build_warehouse.py --symbols SOFI PLTR HOOD COIN --days 30
# run the replication backtest over whatever is in the warehouse
PYTHONPATH=src python3 scripts/run_backtest.py
```

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
    yfinance_1m.py # pilot adapter
    finviz.py      # live screener stub (deferred until the backtest justifies it)
scripts/
  build_warehouse.py
  run_backtest.py
  screen_today.py  # stub
```

## Doctrine

- Backtest before anything. No live trading, no broker, no real money.
- Fixed-prior thresholds (the pillars are domain priors, not fits).
- No look-ahead: signals use only bars at/before the entry candle.
- Honest costs: microcap spreads/slippage are brutal; the backtest nets them.
- The gate is the product; the video is the hypothesis generator.
