"""Tempest — Handover

Date: 2026-08-13
Purpose: clean-slate US microcap momentum research lab. Test the Warrior
Trading momentum methodology honestly, then discover edges beyond it.
Single source of truth: update this file in place; no drifting docs.

Why a new repo and not a Price feature
  Price is a liquid large-cap state-slicing lab with a deliberately closed
  universe and no float/news/intraday-microcap data. The momentum strategy
  lives in a different substrate (low-float microcaps, 1-minute bars, gap
  and volume demand). Mixing them would corrupt Price's observation window
  and Tempest's screen. Separate repos, shared doctrine.

Current state
  Scaffold: data adapters (yfinance 1m, Alpaca 1m, TradingView five-pillar
  screener), warehouse, features, strategy replication (5 pillars +
  first-pullback), validation, backtest core, market screen, and a PAPER
  trading layer (Alpaca paper only: bracket entries, stops, risk rails,
  journal, P&L attribution). Tests pass (34).

Design decisions
  - The 5 pillars are HYPOTHESES with fixed thresholds from the course.
    The backtest measures follow-through after costs; the bucket analysis
    searches for structure the course never quantified.
  - Live screener FIRST (TradingView backend): a fixed symbol basket almost
    never contains the rare qualifying days (proven empirically: 12 symbols
    x 17 sessions = 0 passes). The screener finds the movers as they happen;
    the pilot's 30-day window only matters once the bars of qualifying days
    are captured.
  - yfinance 1m (~30 days, 7-day chunked) for the pilot. Polygon (paid)
    adapter interface exists for real 1-minute history when justified.
  - Costs are generous (microcap spreads/slippage are brutal); halts and
    borrow are documented as unmodelled risks in v1.
  - No look-ahead, fixed-prior thresholds, walk-forward validation — the
    Price/Halcyon discipline carried over.

Deferred (do not build without evidence or operator sign-off)
  - Live (real-money) trading — paper must show a real edge first
  - Polygon data (paid) until the pilot shows promise
  - Finviz HTML scraper fallback (TradingView backend may change/throttle)
  - Richer order-fill attribution (partial fills, multi-leg avg) — the
    next poll now journals stop_filled/tp_filled from broker order history
  - Any live trading, broker integration, real money
  - Options, forex, crypto lanes

Known risks
  - yfinance 1m is rate-limited and only ~30 days deep; pilot samples are
    small and directional at best.
  - Microcap data quality (halted bars, gaps, phantom prints) needs
    sanitisation; the warehouse keeps raw + adjusted fields.
  - The course's edge, if any, is a human-discretion edge (Level 2, tape).
    Tempest can only approximate it mechanically — the honest expectation
    is that measured follow-through will be far below the course's claims.

2026-08-13 — integrity fixes
  - Backtest screens pillars as-of the signal bar (relvol_asof + cumulative
    volume). Using full-session volume to bless a morning entry was look-ahead.
  - bucket_for edges were off-by-one (a 3% gap was labelled 5-10%).
  - Detector emits the FIRST pullback per session only.
  - Paper poll: same-pass max-open accounting, skip symbols with a resting
    order, refuse screen names with relvol > 50x (halt/resume prints).
  - Broker-side stop/TP fills are now journaled on the next poll
    (reconcile against open positions vs unmatched entries).
  - Backtest float pillar reads localdata/screen_log.csv (same source as live).
  - Breakout fill is the prior high (gap-through pays the open), not the close.
  - paper_status.csv is tailed to 250 rows on commit.
"""
