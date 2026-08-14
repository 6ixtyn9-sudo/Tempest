#!/usr/bin/env python3
"""Measure REAL execution cost from Alpaca paper fills.

Replaces the assumption at the centre of every conclusion so far. The
backtest charges CostModel(spread_bps=25, slippage_bps=25) = 100 bps round
trip, and that single number decides whether the strategy is viable:

    breakeven win rate for a 2R exit = (r + c) / (3r)

    stop r    c=100bps    c=40bps
      0.5%      100.0%      60.0%
      1.0%       66.7%      46.7%
      2.0%       50.0%      40.0%

At 40 bps a 1% stop needs 46.7% rather than 66.7% -- the difference between
"unviable" and "plausible". Nobody has measured it. This does.

Method
------
For each entry order in the journal, compare the intended limit price with
the broker's actual filled_avg_price:

    entry slippage_bps = 10000 * (filled - intended) / intended

Positive = paid MORE than intended (adverse). Exits are measured the same
way against the stop/target leg price. Round trip = entry + exit.

Honest limits
-------------
* Paper fills are SIMULATED. Alpaca's paper engine fills against real
  quotes but does not model market impact, queue position, or partial
  fills the way a live venue does. Treat the result as a lower bound on
  real cost, not a substitute for live execution data.
* Limit entries can only fill AT or BETTER than the limit, so entry
  slippage is bounded at <= 0 by construction. The real cost lives in the
  exits: the bracket's stop leg is a STOP order, which becomes a MARKET
  order when triggered. That is where microcap slippage happens, and it is
  the number worth watching.
* Unfilled limit orders are an invisible cost (opportunity, not slippage)
  and are reported separately as a fill rate.

Usage:
  PYTHONPATH=src python3 scripts/measure_slippage.py
  PYTHONPATH=src python3 scripts/measure_slippage.py --json localdata/slippage.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tempest.risk import load_journal  # noqa: E402
from tempest.validation import CostModel  # noqa: E402

ENTRY_ACTIONS = {"order_submitted", "entry", "entry_filled"}
EXIT_ACTIONS = {"exit", "stop_filled", "target_filled", "exit_submitted",
                "broker_closed"}


def bps(actual, intended):
    if not intended or not np.isfinite(intended) or intended <= 0:
        return np.nan
    return 10000.0 * (float(actual) - float(intended)) / float(intended)


def describe(values, label):
    clean = np.array([v for v in values if v is not None and np.isfinite(v)])
    if len(clean) == 0:
        return None
    return {
        "label": label, "n": int(len(clean)),
        "mean_bps": float(clean.mean()), "median_bps": float(np.median(clean)),
        "p90_bps": float(np.percentile(clean, 90)),
        "worst_bps": float(clean.max()), "best_bps": float(clean.min()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--broker", action="store_true",
                    help="also reconcile against live Alpaca order history")
    args = ap.parse_args()

    journal = load_journal()
    if journal.empty:
        print("Trade journal is empty: no fills have ever occurred.")
        print()
        print("Nothing to measure yet. This is expected -- Tempest has taken")
        print("zero trades to date. Re-run once paper fills accumulate.")
        print()
        print(f"Assumed cost model remains UNVERIFIED: "
              f"{CostModel().round_trip_bps():.0f} bps round trip.")
        print("Every viability conclusion drawn so far rests on that number.")
        return 0

    journal["action"] = journal["action"].astype(str).str.strip()
    filled = journal[journal["status"].astype(str).str.lower().isin(
        {"filled", "partially_filled"}
    )]

    entries = filled[filled["action"].isin(ENTRY_ACTIONS)]
    exits = filled[filled["action"].isin(EXIT_ACTIONS)]

    entry_slip = []
    for _, row in entries.iterrows():
        intended = pd.to_numeric(row.get("entry_price"), errors="coerce")
        actual = pd.to_numeric(row.get("price"), errors="coerce")
        if pd.notna(intended) and pd.notna(actual):
            entry_slip.append(bps(actual, intended))

    exit_slip = []
    for _, row in exits.iterrows():
        actual = pd.to_numeric(row.get("exit_price"), errors="coerce")
        if pd.isna(actual):
            actual = pd.to_numeric(row.get("price"), errors="coerce")
        reason = str(row.get("reason") or row.get("action") or "").lower()
        intended = pd.to_numeric(
            row.get("target_price") if "target" in reason else row.get("stop_price"),
            errors="coerce",
        )
        if pd.notna(intended) and pd.notna(actual):
            # Selling below the intended exit is adverse -> positive bps.
            exit_slip.append(-bps(actual, intended))

    submitted = journal[journal["action"] == "order_submitted"]
    fill_rate = (len(entries) / len(submitted)) if len(submitted) else np.nan

    print(f"journal rows: {len(journal)}   entries filled: {len(entries)}   "
          f"exits filled: {len(exits)}")
    if np.isfinite(fill_rate):
        print(f"limit-order fill rate: {100 * fill_rate:.1f}% "
              f"({len(entries)}/{len(submitted)} submitted)")
    print()

    report = {}
    header = f"{'leg':>10}{'n':>5}{'mean':>9}{'median':>9}{'p90':>9}{'worst':>9}"
    print(header)
    print("-" * len(header))
    for values, label in [(entry_slip, "entry"), (exit_slip, "exit")]:
        stats = describe(values, label)
        if stats is None:
            print(f"{label:>10}{0:>5}{'-':>9}{'-':>9}{'-':>9}{'-':>9}")
            continue
        report[label] = stats
        print(f"{label:>10}{stats['n']:>5}{stats['mean_bps']:>9.1f}"
              f"{stats['median_bps']:>9.1f}{stats['p90_bps']:>9.1f}"
              f"{stats['worst_bps']:>9.1f}")

    modelled = CostModel().round_trip_bps()
    if "entry" in report and "exit" in report:
        measured = report["entry"]["mean_bps"] + report["exit"]["mean_bps"]
        report["measured_round_trip_bps"] = measured
        print()
        print(f"MEASURED round trip : {measured:.1f} bps")
        print(f"MODELLED round trip : {modelled:.1f} bps")
        n_min = min(report["entry"]["n"], report["exit"]["n"])
        if n_min < 20:
            print(f"\n  n={n_min} is too small to retune the cost model. "
                  "Directional only.")
        else:
            delta = measured - modelled
            verdict = ("model is PESSIMISTIC (real costs lower)" if delta < -10
                       else "model is OPTIMISTIC (real costs higher)" if delta > 10
                       else "model is roughly right")
            print(f"  -> {verdict}")
        print("\nBreakeven win rate for a 2R exit at the MEASURED cost:")
        c = max(measured, 0.0) / 10000.0
        for r in [0.005, 0.01, 0.02, 0.03]:
            print(f"    stop {100 * r:>4.1f}%  ->  {100 * (r + c) / (3 * r):>5.1f}%")
    else:
        print("\nNot enough filled legs to compute a round trip.")
        print(f"Modelled {modelled:.0f} bps remains UNVERIFIED.")

    print("\nCaveat: Alpaca paper fills are simulated against real quotes but")
    print("do not model market impact or queue position. Treat as a LOWER")
    print("BOUND on live cost. Limit entries fill at-or-better by")
    print("construction, so exits (stop legs are market orders) carry the")
    print("real slippage.")

    if args.json:
        args.json.write_text(json.dumps(
            {"modelled_round_trip_bps": modelled, **report}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
