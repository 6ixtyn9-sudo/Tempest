#!/usr/bin/env python3
"""Fetch 1-minute bars into the warehouse (yfinance pilot, <=30 days).

Usage:
  PYTHONPATH=src python3 scripts/build_warehouse.py --symbols YXT GFAI --days 30
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.sources.yfinance_1m import YFinance1mSource  # noqa: E402
from tempest.warehouse import save_to_warehouse  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()

    src = YFinance1mSource()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    for sym in args.symbols:
        df = src.fetch_1m(sym.upper(), start, end)
        if df.empty:
            print(f"{sym}: no bars fetched (delisted? rate-limited? symbol bad?)")
            continue
        n = save_to_warehouse(df)
        print(f"{sym}: saved {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
