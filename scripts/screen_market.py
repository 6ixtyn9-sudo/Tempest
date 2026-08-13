#!/usr/bin/env python3
"""Run the five-pillar market screen (TradingView backend), log qualifiers,
and optionally fetch their 1-minute bars into the warehouse.

Usage:
  PYTHONPATH=src python3 scripts/screen_market.py              # screen + log
  PYTHONPATH=src python3 scripts/screen_market.py --fetch-bars # + fetch 1m bars
  PYTHONPATH=src python3 scripts/screen_market.py --no-cache   # bypass cache

Logs every scanned row (pass/fail) to localdata/screen_log.csv so the rare
qualifying sessions accumulate over time for backtesting.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from tempest.config import DATA_DIR  # noqa: E402
from tempest.sources import tradingview  # noqa: E402
from tempest.sources.tradingview import build_filter, screen  # noqa: E402
from tempest.strategy import screen_pillars  # noqa: E402

SCREEN_LOG_PATH = DATA_DIR / "screen_log.csv"


def _load_log() -> pd.DataFrame:
    if not SCREEN_LOG_PATH.exists():
        return pd.DataFrame(columns=[
            "date_utc", "symbol", "close", "gap_pct", "relvol",
            "float_shares", "volume", "passes",
        ])
    try:
        return pd.read_csv(SCREEN_LOG_PATH)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fetch-bars", action="store_true",
                   help="Also fetch 1m bars for qualifying symbols")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--min-relvol", type=float, default=5.0)
    p.add_argument("--min-gap", type=float, default=2.0)
    args = p.parse_args()

    filters = build_filter(relvol_min=args.min_relvol, gap_min_pct=args.min_gap)
    rows = screen(filters, use_cache=not args.no_cache)
    if not rows:
        if tradingview.LAST_ERROR:
            print(f"Screen FAILED to reach the scanner: {tradingview.LAST_ERROR}")
            return 1
        print("Screen returned no rows - no qualifiers today (normal; the "
              "five pillars are rare). Nothing to log.")
        return 0

    now = datetime.now(timezone.utc)
    log = _load_log()
    passes = []
    for r in rows:
        pill = screen_pillars(
            r["symbol"],
            relvol=r["relvol"],
            total_volume=r["volume"],
            gap_open=r["gap_pct"] / 100.0,   # TV gap is in percent
            price=r["close"],
            float_shares=r["float_shares"],
        )
        log = pd.concat([log, pd.DataFrame([{
            "date_utc": now.date().isoformat(),
            "symbol": r["symbol"], "close": r["close"], "gap_pct": r["gap_pct"],
            "relvol": r["relvol"], "float_shares": r["float_shares"],
            "volume": r["volume"], "passes": pill.passes,
        }])], ignore_index=True)
        if pill.passes:
            passes.append(r)
        print(f"  {'PASS' if pill.passes else 'fail'} {r['symbol']:6s} "
              f"close={r['close']:>7.2f} gap={r['gap_pct']:>6.1f}% "
              f"relvol={r['relvol']:>6.1f} float={r['float_shares']:>12,.0f} "
              f"vol={r['volume']:>12,.0f}")

    log = log.drop_duplicates(subset=["date_utc", "symbol"], keep="last")
    SCREEN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log.to_csv(SCREEN_LOG_PATH, index=False)
    print(f"\n{len(passes)} qualifier(s) today; log at {SCREEN_LOG_PATH} "
          f"({len(log)} rows total)")

    if args.fetch_bars and passes:
        from tempest.sources.yfinance_1m import YFinance1mSource
        from tempest.warehouse import save_to_warehouse
        src = YFinance1mSource()
        from datetime import timedelta
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        for r in passes:
            df = src.fetch_1m(r["symbol"], start, end)
            if df.empty:
                print(f"  {r['symbol']}: no 1m bars fetched")
                continue
            save_to_warehouse(df)
            print(f"  {r['symbol']}: saved {len(df)} 1m bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
