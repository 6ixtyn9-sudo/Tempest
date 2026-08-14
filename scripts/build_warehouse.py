#!/usr/bin/env python3
"""Fetch 1-minute bars into the warehouse.

Sources
-------
  alpaca   (default) full 1m history on the free tier. Requires
           ALPACA_API_KEY / ALPACA_SECRET_KEY. This is the only source that
           can build a warehouse deep enough to tune parameters on.
  yfinance pilot source, HARD LIMITED BY THE VENDOR to ~8 days of 1m data
           per request and ~30 days of availability total. Measured
           2026-08-14: windows 8/16/24 days back returned bars; 32 and 40
           days back returned zero. Yahoo rejects any single 1m request
           spanning more than 8 days outright:
             "Only 8 days worth of 1m granularity data are allowed to be
              fetched per request."
           `--days 90` against yfinance therefore silently produced NOTHING
           before this change, because the old code issued one oversized
           request and printed "no bars fetched".

Both sources are now fetched in windows and appended, so --days is honoured
rather than truncated. yfinance still cannot exceed what Yahoo retains; the
script says so explicitly instead of failing quietly.

Usage:
  # deep history (recommended, needs Alpaca keys)
  PYTHONPATH=src python3 scripts/build_warehouse.py --symbols AIRO RRGB --days 90

  # from a file, one symbol per line
  PYTHONPATH=src python3 scripts/build_warehouse.py --symbols-file universe.txt --days 90

  # explicit yfinance (will be capped by the vendor)
  PYTHONPATH=src python3 scripts/build_warehouse.py --symbols AIRO --days 8 --source yfinance
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.warehouse import load_from_warehouse, save_to_warehouse  # noqa: E402

# Yahoo refuses any 1m request wider than 8 days.
YF_MAX_WINDOW_DAYS = 7
# Alpaca has no such per-request cap, but chunking keeps responses small and
# makes partial progress durable if a long build is interrupted.
ALPACA_WINDOW_DAYS = 30


def build_source(name: str):
    if name == "alpaca":
        from tempest.sources.alpaca import AlpacaSource
        return AlpacaSource(), ALPACA_WINDOW_DAYS
    if name == "yfinance":
        from tempest.sources.yfinance_1m import YFinance1mSource
        return YFinance1mSource(), YF_MAX_WINDOW_DAYS
    raise ValueError(f"unknown source: {name}")


def windows(start: datetime, end: datetime, span_days: int):
    """Yield [a, b) chunks of at most span_days, oldest first."""
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=span_days), end)
        yield cursor, stop
        cursor = stop


def main() -> int:
    p = argparse.ArgumentParser(
        description="Build the 1m warehouse.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbols", nargs="+")
    g.add_argument("--symbols-file", type=Path,
                   help="file with one symbol per line; # comments allowed")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--source", choices=["alpaca", "yfinance"], default="alpaca")
    p.add_argument("--sleep", type=float, default=0.3,
                   help="pause between requests, to stay polite with the vendor")
    args = p.parse_args()

    if args.symbols_file:
        raw = args.symbols_file.read_text().splitlines()
        symbols = [s.strip().upper() for s in raw
                   if s.strip() and not s.lstrip().startswith("#")]
    else:
        symbols = [s.upper() for s in args.symbols]
    if not symbols:
        print("no symbols given")
        return 2

    try:
        src, span = build_source(args.source)
    except RuntimeError as exc:
        print(f"cannot use source {args.source!r}: {exc}")
        if args.source == "alpaca":
            print("  -> set ALPACA_API_KEY / ALPACA_SECRET_KEY in .env, or")
            print("  -> pass --source yfinance (limited to ~8 days of 1m data)")
        return 1

    if args.source == "yfinance" and args.days > 30:
        print(f"WARNING: --days {args.days} with --source yfinance. Yahoo only")
        print("retains ~30 days of 1m data and caps each request at 8 days.")
        print("Expect far fewer days than requested. Use --source alpaca for depth.")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    grand_total = 0
    failures = []

    for i, sym in enumerate(symbols, 1):
        before = len(load_from_warehouse(sym))
        saved_any = False
        empty_windows = 0
        for a, b in windows(start, end, span):
            try:
                df = src.fetch_1m(sym, a, b)
            except Exception as exc:  # noqa: BLE001 - keep going, report at end
                print(f"  {sym} {a.date()}..{b.date()}: ERROR {exc}")
                continue
            if df is None or df.empty:
                empty_windows += 1
            else:
                save_to_warehouse(df)
                saved_any = True
            time.sleep(args.sleep)

        after = len(load_from_warehouse(sym))
        gained = after - before
        grand_total += max(0, gained)
        if not saved_any:
            failures.append(sym)
            print(f"[{i}/{len(symbols)}] {sym}: no bars (delisted? bad symbol? "
                  f"{empty_windows} empty windows)")
        else:
            sessions = 0
            wh = load_from_warehouse(sym)
            if not wh.empty:
                sessions = (wh["bar_ts_utc"].dt.tz_convert("America/New_York")
                            .dt.date.nunique())
            print(f"[{i}/{len(symbols)}] {sym}: warehouse {after:,} rows "
                  f"(+{gained:,}), {sessions} sessions")

    print(f"\nDone. {grand_total:,} new rows across {len(symbols)} symbols.")
    if failures:
        print(f"No data for {len(failures)}: {', '.join(failures)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
