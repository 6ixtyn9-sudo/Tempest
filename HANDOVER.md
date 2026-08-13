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
  Scaffold: data adapter (yfinance 1m pilot), warehouse, features,
  strategy replication (5 pillars + first-pullback), validation,
  backtest core, tests. Nothing live.

Design decisions
  - The 5 pillars are HYPOTHESES with fixed thresholds from the course.
    The backtest measures follow-through after costs; the bucket analysis
    searches for structure the course never quantified.
  - Data adapter first, scraper later: a live screener is only useful once
    the measurement says the setup pays. screen_today.py is a stub.
  - yfinance 1m (~30 days) for the pilot. Polygon (paid) adapter interface
    exists for real 1-minute history when the pilot justifies the spend.
  - Costs are generous (microcap spreads/slippage are brutal); halts and
    borrow are documented as unmodelled risks in v1.
  - No look-ahead, fixed-prior thresholds, walk-forward validation — the
    Price/Halcyon discipline carried over.

Deferred (do not build without evidence or operator sign-off)
  - Live screening / finviz scraper
  - Polygon data (paid) until the pilot shows promise
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
"""
