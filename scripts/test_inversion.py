#!/usr/bin/env python3
"""Test whether inverting a losing strategy produces a winning one.

The intuition is reasonable: if a setup reliably loses, fade it. The
arithmetic is less forgiving, because costs do not invert.

    net_long     = +gross - cost
    net_inverted = -gross - cost

Inverting flips the GROSS return but you still pay the spread. So
inversion only pays when gross is strongly NEGATIVE -- specifically when
gross < -cost. A strategy whose gross is merely zero inverts into another
zero-gross strategy that still bleeds the spread.

Gale measured at n=62: net -1.000%, cost 100 bps, so gross = +0.000%.
Naive inversion therefore predicts -1.000% again, not +1.000%.

But a naive sign flip is NOT a fair test of a short. Path dependency
differs: for a short the stop sits ABOVE entry (hit when price rises) and
the target BELOW, so which level is touched first can change entirely.
This script simulates the short properly rather than negating returns.

Usage:
  PYTHONPATH=src python3 scripts/test_inversion.py
  PYTHONPATH=src python3 scripts/test_inversion.py --rr 1.0 2.0 3.0
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

from tempest.config import WAREHOUSE_DIR  # noqa: E402
from tempest.features import compute_features  # noqa: E402
from tempest.gale import HOLD_BARS, detect_gale_orb  # noqa: E402
from tempest.validation import CostModel  # noqa: E402
from tempest.warehouse import load_from_warehouse  # noqa: E402

COST = CostModel()
MIN_N_FOR_CI = 10


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


def simulate_long(entry, stop, rr, grp, start, hold):
    risk = entry - stop
    if risk <= 0:
        return None
    target = entry + rr * risk
    forward = grp.iloc[start:start + hold]
    if forward.empty:
        return None
    for _, bar in forward.iterrows():
        if bar["low"] <= stop:
            return COST.net_return((stop - entry) / entry), "stop"
        if bar["high"] >= target:
            return COST.net_return((target - entry) / entry), "target"
    last = float(forward["close"].iloc[-1])
    return COST.net_return((last - entry) / entry), "horizon"


def simulate_short(entry, stop, rr, grp, start, hold):
    """Short: stop ABOVE entry, target BELOW. Profits when price falls."""
    risk = stop - entry
    if risk <= 0:
        return None
    target = entry - rr * risk
    forward = grp.iloc[start:start + hold]
    if forward.empty:
        return None
    for _, bar in forward.iterrows():
        # Conservative: the adverse level is checked first.
        if bar["high"] >= stop:
            return COST.net_return((entry - stop) / entry), "stop"
        if bar["low"] <= target:
            return COST.net_return((entry - target) / entry), "target"
    last = float(forward["close"].iloc[-1])
    return COST.net_return((entry - last) / entry), "horizon"


def bootstrap_ci(sample, rng, iters=20000):
    if len(sample) < 2:
        return np.nan, np.nan
    draws = rng.choice(sample, size=(iters, len(sample)), replace=True).mean(axis=1)
    return tuple(np.percentile(draws, [2.5, 97.5]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rr", nargs="+", type=float, default=[1.0, 2.0, 3.0])
    ap.add_argument("--seed", type=int, default=20260814)
    args = ap.parse_args()

    frames = load_frames()
    if not frames:
        print(f"No warehouse at {WAREHOUSE_DIR}. Run build_warehouse.py first.")
        return 1

    sessions = sum(f["session"].nunique() for f in frames.values())
    print(f"warehouse: {len(frames)} symbols, {sessions} symbol-sessions")
    print(f"cost model: {COST.round_trip_bps():.0f} bps round trip\n")

    rng = np.random.default_rng(args.seed)
    rows = []
    header = (f"{'direction':<26}{'rr':>5}{'n':>5}{'win%':>7}"
              f"{'gross%':>9}{'net%':>9}{'95% CI':>19}")
    print(header)
    print("-" * len(header))

    for label, fn, rr_list in [
        ("LONG breakout (measured)", simulate_long, [2.0]),
        ("SHORT / fade the breakout", simulate_short, args.rr),
    ]:
        for rr in rr_list:
            nets = []
            for symbol, feat in frames.items():
                for sig in detect_gale_orb(feat, symbol):
                    grp = (feat[feat["session"] == sig.session]
                           .sort_values("bar_ts_utc").reset_index(drop=True))
                    idx = grp.index[grp["bar_ts_utc"] == sig.signal_ts]
                    if len(idx) == 0:
                        continue
                    entry = float(sig.trigger_price)
                    risk = entry - float(sig.stop_price)
                    if risk <= 0:
                        continue
                    stop = (float(sig.stop_price) if fn is simulate_long
                            else entry + risk)
                    result = fn(entry, stop, rr, grp, int(idx[0]) + 1, HOLD_BARS)
                    if result:
                        nets.append(result[0])
            arr = np.array(nets)
            if len(arr) == 0:
                print(f"{label:<26}{rr:>5.1f}{0:>5}")
                continue
            lo, hi = bootstrap_ci(arr, rng)
            gross = 100 * arr.mean() + COST.round_trip_bps() / 100.0
            flag = "" if len(arr) >= MIN_N_FOR_CI else "  (n<10)"
            print(f"{label:<26}{rr:>5.1f}{len(arr):>5}"
                  f"{100 * (arr > 0).mean():>7.1f}{gross:>9.3f}"
                  f"{100 * arr.mean():>9.3f}"
                  f"   [{100 * lo:>6.2f},{100 * hi:>6.2f}]{flag}")
            rows.append((label, rr, len(arr), arr.mean(), lo, hi))

    print("\nWhy inversion rarely rescues a strategy:")
    print("  net_long = +gross - cost ;  net_inverted = -gross - cost")
    print("  Costs do NOT invert. Inversion pays only when")
    print(f"  gross < -{COST.round_trip_bps() / 100:.2f}% per trade.")
    longs = [r for r in rows if r[0].startswith("LONG")]
    if longs:
        g = 100 * longs[0][3] + COST.round_trip_bps() / 100.0
        print(f"\n  Measured LONG gross: {g:+.3f}%/trade.")
        if g > -COST.round_trip_bps() / 100.0:
            print("  That is not negative enough. There is no systematic")
            print("  mispricing to fade -- the setup is simply uninformative,")
            print("  and an uninformative signal is uninformative in both")
            print("  directions while the spread is paid either way.")

    print("\nAlso note, before shorting microcaps for real:")
    print("  * borrow may be unavailable or expensive on sub-$100M floats")
    print("  * hard-to-borrow fees are not in this cost model")
    print("  * short squeezes give unbounded loss on exactly this universe")
    print("  A paper result would understate all three.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
