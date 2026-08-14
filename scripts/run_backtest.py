#!/usr/bin/env python3
"""Run the momentum replication backtest over the warehouse.

Usage:
  PYTHONPATH=src python3 scripts/run_backtest.py            # all symbols
  PYTHONPATH=src python3 scripts/run_backtest.py --symbols YXT GFAI

Prints an honest report: per-symbol summary, aggregate gross vs net,
win rate, avg R, exit-reason mix, and per-bucket breakdowns (the
discovery surface beyond the course's rules).
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.backtest import run_backtest  # noqa: E402
from tempest.config import DATA_DIR, WAREHOUSE_DIR  # noqa: E402
from tempest.validation import CostModel, summarize  # noqa: E402
from tempest.warehouse import load_from_warehouse  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=None)
    p.add_argument("--spread-bps", type=float, default=25.0)
    p.add_argument("--slippage-bps", type=float, default=25.0)
    p.add_argument("--hold-bars", type=int, default=15)
    p.add_argument("--relax", action="store_true",
                   help="DIAGNOSTIC: loosen pillars (relvol>=2x, gap>=1%, skip price/float) "
                        "to validate backtest mechanics on large-cap data")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", default=None,
                   help="Durable JSON report path (default: dated localdata report)")
    args = p.parse_args()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = sorted(
            d.name.replace("symbol=", "")
            for d in WAREHOUSE_DIR.iterdir()
            if d.is_dir() and d.name.startswith("symbol=")
        )

    cost = CostModel(spread_bps=args.spread_bps, slippage_bps=args.slippage_bps)
    reports = []
    for sym in symbols:
        df = load_from_warehouse(sym)
        if df.empty:
            print(f"{sym}: no warehouse data, skipping")
            continue
        rep = run_backtest(df, sym, cost_model=cost, hold_bars=args.hold_bars,
                          relax=args.relax)
        reports.append(rep)
        s = rep["summary"]
        ss = rep.get("screen_stats", {})
        print(f"\n== {sym}: {s.get('n', 0)} trades | gross {s.get('gross_mean', 0):.4f} "
              f"net {s.get('net_mean', 0):.4f} | win {s.get('win_rate', 0):.1%} | "
              f"avgR {s.get('avg_r', 0):.2f} | {s.get('exit_reasons', {})}")
        if ss.get("sessions"):
            print(f"   screen: {ss.get('passed', 0)}/{ss.get('sessions', 0)} sessions passed | "
                  f"rejects: {ss.get('reject_reasons', {})}")

    all_trades = [t for r in reports for t in r["trades"]]
    agg = summarize([type("R", (), {"gross_return": t["gross_return"],
                                    "net_return": t["net_return"],
                                    "r_multiple": t["r_multiple"],
                                    "exit_reason": t["exit_reason"]}) for t in all_trades])
    print("\n=== AGGREGATE (net of costs) ===")
    print(json.dumps({k: v for k, v in agg.items() if k != "exit_reasons"}, indent=2))
    print("exit reasons:", agg.get("exit_reasons"))

    generated_at = datetime.now(timezone.utc)
    try:
        code_revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        code_revision = "unknown"
    payload = {
        "generated_at_utc": generated_at.isoformat(),
        "code_revision": code_revision,
        "parameters": {
            "spread_bps": args.spread_bps,
            "slippage_bps": args.slippage_bps,
            "hold_bars": args.hold_bars,
            "relax": args.relax,
        },
        "symbols": symbols,
        "aggregate": agg,
        "reports": reports,
    }
    output = Path(args.output) if args.output else (
        DATA_DIR / f"backtest_report_{generated_at.date().isoformat()}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"durable report: {output}")

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
