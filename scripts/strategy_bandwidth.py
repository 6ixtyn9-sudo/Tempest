#!/usr/bin/env python3
"""How fast can each strategy generate evidence, and are they independent?

The binding constraint on this system is not profitability -- it is
MEASUREMENT BANDWIDTH. A strategy firing once every three days needs ~4.5
months to reach n=30, which is the minimum for a significance test. Until
then every sweep correctly refuses to adopt anything, and the project
cannot iterate.

Adding strategies is the standard fix, but only if they are UNCORRELATED.
Two strategies that fire on the same symbol on the same day, in the same
direction, are one strategy with extra steps: they double the trade count
while adding almost nothing to the independent sample size, and they
concentrate risk instead of spreading it.

This measures both properties from the warehouse:
  * signal rate  -- signals per symbol-session, and implied days per trade
  * overlap      -- how often two strategies fire on the same symbol+session

Usage:
  PYTHONPATH=src python3 scripts/strategy_bandwidth.py
  PYTHONPATH=src python3 scripts/strategy_bandwidth.py --candidates-per-day 10
"""

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.config import WAREHOUSE_DIR  # noqa: E402
from tempest.features import compute_features  # noqa: E402
from tempest.gale import detect_gale_orb  # noqa: E402
from tempest.strategy import MIN_RISK_FRACTION, detect_first_pullback  # noqa: E402
from tempest.validation import CostModel  # noqa: E402
from tempest.warehouse import load_from_warehouse  # noqa: E402

TARGET_N = 30  # minimum sample for a significance test


def load_frames():
    frames = {}
    if not WAREHOUSE_DIR.exists():
        return frames
    for part in sorted(WAREHOUSE_DIR.glob("symbol=*")):
        symbol = part.name.split("=", 1)[1]
        raw = load_from_warehouse(symbol)
        if raw.empty:
            continue
        feat = compute_features(raw)
        if feat is not None and not feat.empty:
            frames[symbol] = feat
    return frames


def tempest_signals(frames, min_risk):
    keys = []
    for symbol, feat in frames.items():
        for sig in detect_first_pullback(
            feat, symbol, squeeze_bars=3, min_risk_fraction=min_risk
        ):
            keys.append((symbol, str(sig.session)))
    return keys


def gale_signals(frames, _min_risk):
    keys = []
    for symbol, feat in frames.items():
        for sig in detect_gale_orb(feat, symbol):
            keys.append((symbol, str(sig.session)))
    return keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates-per-day", type=float, default=10.0,
                    help="symbols the screen surfaces daily (100M float: ~10)")
    ap.add_argument("--min-risk", type=float, default=0.02,
                    help="live probe floor (default 0.02)")
    args = ap.parse_args()

    frames = load_frames()
    if not frames:
        print(f"No warehouse at {WAREHOUSE_DIR}. Run build_warehouse.py first.")
        return 1

    sessions = sum(f["session"].nunique() for f in frames.values())
    print(f"warehouse: {len(frames)} symbols, {sessions} symbol-sessions")
    print(f"live floor: min_risk={args.min_risk} "
          f"(detector default {MIN_RISK_FRACTION})")
    print(f"cost model: {CostModel().round_trip_bps():.0f} bps round trip\n")

    strategies = {
        "tempest_first_pullback": tempest_signals(frames, args.min_risk),
        "gale_orb5": gale_signals(frames, args.min_risk),
    }

    header = (f"{'strategy':<26}{'signals':>9}{'per session':>13}"
              f"{'/day':>8}{'days/trade':>12}{'days to n=30':>14}")
    print(header)
    print("-" * len(header))
    for name, keys in strategies.items():
        n = len(keys)
        rate = n / sessions if sessions else 0.0
        per_day = rate * args.candidates_per_day
        days = (1.0 / per_day) if per_day > 0 else float("inf")
        to_target = days * TARGET_N
        print(f"{name:<26}{n:>9}{rate:>13.4f}{per_day:>8.2f}"
              f"{days:>12.1f}{to_target:>14.0f}")

    combined = [k for keys in strategies.values() for k in keys]
    total_rate = len(combined) / sessions if sessions else 0.0
    per_day = total_rate * args.candidates_per_day
    days = (1.0 / per_day) if per_day > 0 else float("inf")
    print(f"{'COMBINED':<26}{len(combined):>9}{total_rate:>13.4f}"
          f"{per_day:>8.2f}{days:>12.1f}{days * TARGET_N:>14.0f}")

    print("\nIndependence check (shared symbol+session):")
    names = list(strategies)
    for a, b in itertools.combinations(names, 2):
        sa, sb = set(strategies[a]), set(strategies[b])
        if not sa or not sb:
            print(f"  {a} vs {b}: one side has no signals, cannot assess")
            continue
        shared = sa & sb
        jaccard = len(shared) / len(sa | sb)
        print(f"  {a} vs {b}: {len(shared)} shared of {len(sa)}/{len(sb)} "
              f"(Jaccard {jaccard:.2f})")
        if jaccard > 0.3:
            print("    HIGH OVERLAP -- these are not independent samplers.")
        else:
            print("    Low overlap -- genuinely different setups.")

    print("\nWhat would actually move the needle:")
    slowest = max(
        ((n, len(k)) for n, k in strategies.items()), key=lambda kv: kv[1]
    )
    print(f"  Fastest existing sampler: {slowest[0]} ({slowest[1]} signals).")
    if per_day > 0:
        print(f"  To reach n=30 in 30 trading days you need "
              f"{TARGET_N / 30.0:.2f} signals/day, i.e. "
              f"{(TARGET_N / 30.0) / max(total_rate, 1e-9):.0f} candidates/day "
              f"at the current per-session rate.")
        print(f"  You currently screen ~{args.candidates_per_day:.0f}/day.")
    print("\n  Levers, in order of leverage:")
    print("   1. More candidates/day (screen breadth) -- scales linearly and")
    print("      does NOT change any strategy's statistical properties.")
    print("   2. More UNCORRELATED strategies -- only helps if overlap is low")
    print("      AND the new strategy's geometry clears breakeven.")
    print("   3. Looser entry gates -- buys frequency with worse expectancy;")
    print("      the 90-day sweep showed squeeze_bars=2 at -0.671%/trade.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
