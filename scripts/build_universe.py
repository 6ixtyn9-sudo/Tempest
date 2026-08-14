#!/usr/bin/env python3
"""Derive a research universe from recorded screen history.

Why not hand-pick: choosing symbols you remember moving is selection bias.
It loads the sweep with names that already did something interesting, which
inflates measured expectancy. The screen logs are the unbiased record --
they contain what the screener actually surfaced at the time, before anyone
knew how the session turned out.

Sources (whichever exist):
  localdata/screen_log.csv        Tempest five-pillar screen
  localdata/gale_screen_log.csv   Gale ORB5 screen

Usage:
  PYTHONPATH=src python3 scripts/build_universe.py                 # -> universe.txt
  PYTHONPATH=src python3 scripts/build_universe.py --passing-only  # only pillar passers
  PYTHONPATH=src python3 scripts/build_universe.py --min-sessions 2
  PYTHONPATH=src python3 scripts/build_universe.py --out my_universe.txt
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from tempest.config import DATA_DIR  # noqa: E402

SCREEN_LOGS = [
    ("tempest", DATA_DIR / "screen_log.csv", "passes"),
    ("gale", DATA_DIR / "gale_screen_log.csv", "tradeable"),
]
SYMBOL_RE = r"^[A-Z][A-Z0-9.\-]{0,9}$"


def load(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("universe.txt"))
    p.add_argument("--passing-only", action="store_true",
                   help="only symbols that PASSED the pillars at least once")
    p.add_argument("--min-sessions", type=int, default=1,
                   help="require the symbol on at least this many session dates")
    args = p.parse_args()

    frames = []
    for name, path, pass_col in SCREEN_LOGS:
        df = load(path)
        if df.empty or "symbol" not in df.columns:
            print(f"  {name}: {path.name} absent or empty")
            continue
        date_col = "session_date" if "session_date" in df.columns else "date_utc"
        keep = pd.DataFrame({
            "symbol": df["symbol"].astype(str).str.strip().str.upper(),
            "session": df[date_col].astype(str) if date_col in df else "",
            "passed": (df[pass_col].astype(str).str.lower().isin(["true", "1"])
                       if pass_col in df.columns else False),
            "source": name,
        })
        frames.append(keep)
        print(f"  {name}: {len(keep)} rows, "
              f"{keep['symbol'].nunique()} symbols, "
              f"{keep['session'].nunique()} sessions")

    if not frames:
        print("\nNo screen history found. Run scripts/screen_market.py first.")
        return 1

    allrows = pd.concat(frames, ignore_index=True)
    allrows = allrows[allrows["symbol"].str.match(SYMBOL_RE, na=False)]

    if args.passing_only:
        passing = set(allrows.loc[allrows["passed"], "symbol"])
        allrows = allrows[allrows["symbol"].isin(passing)]

    per_symbol = (allrows.groupby("symbol")
                  .agg(sessions=("session", "nunique"),
                       rows=("symbol", "size"),
                       ever_passed=("passed", "any"))
                  .sort_values(["sessions", "rows"], ascending=False))
    per_symbol = per_symbol[per_symbol["sessions"] >= args.min_sessions]

    if per_symbol.empty:
        print("\nNo symbols met the filters.")
        return 1

    args.out.write_text("\n".join(per_symbol.index) + "\n")

    sessions_total = allrows["session"].nunique()
    print(f"\n{len(per_symbol)} symbols -> {args.out}")
    print(per_symbol.to_string())

    print(f"\nDistinct session dates in screen history: {sessions_total}")
    if sessions_total < 20:
        print(
            f"\nWARNING: only {sessions_total} session(s) of screen history.\n"
            "  The screen log started recently, so this universe reflects a\n"
            "  few days of market conditions -- not a neutral sample. It is\n"
            "  still less biased than hand-picking, but a sweep built on it\n"
            "  inherits that narrowness. The log grows every session; rebuild\n"
            "  this file periodically and re-run the sweep as it deepens."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
