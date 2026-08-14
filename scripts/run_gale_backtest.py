#!/usr/bin/env python3
"""Run Gale ORB5 point-in-time backtest and write durable JSON evidence."""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.config import DATA_DIR, WAREHOUSE_DIR  # noqa: E402
from tempest.gale import STRATEGY_ID  # noqa: E402
from tempest.gale_backtest import load_screen_observations, run_gale_backtest  # noqa: E402
from tempest.validation import CostModel, summarize  # noqa: E402
from tempest.warehouse import load_from_warehouse  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--spread-bps", type=float, default=25.0)
    parser.add_argument("--slippage-bps", type=float, default=25.0)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    symbols = [s.upper() for s in args.symbols] if args.symbols else sorted(
        path.name.replace("symbol=", "")
        for path in WAREHOUSE_DIR.iterdir()
        if path.is_dir() and path.name.startswith("symbol=")
    )
    observations = load_screen_observations()
    cost = CostModel(
        spread_bps=args.spread_bps, slippage_bps=args.slippage_bps
    )
    reports = []
    for symbol in symbols:
        bars = load_from_warehouse(symbol)
        if bars.empty:
            continue
        reports.append(run_gale_backtest(
            bars, symbol, observations=observations, cost_model=cost
        ))

    all_trades = [trade for report in reports for trade in report["trades"]]
    synthetic = [
        type("R", (), {
            "gross_return": trade["gross_return"],
            "net_return": trade["net_return"],
            "r_multiple": trade["r_multiple"],
            "exit_reason": trade["exit_reason"],
        })
        for trade in all_trades
    ]
    aggregate = summarize(synthetic)
    now = datetime.now(timezone.utc)
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unknown"
    payload = {
        "strategy_id": STRATEGY_ID,
        "generated_at_utc": now.isoformat(),
        "code_revision": revision,
        "parameters": {
            "spread_bps": args.spread_bps,
            "slippage_bps": args.slippage_bps,
        },
        "symbols": symbols,
        "aggregate": aggregate,
        "reports": reports,
    }
    output = Path(args.output) if args.output else (
        DATA_DIR / f"gale_backtest_report_{now.date().isoformat()}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"Gale backtest: {len(all_trades)} trade(s), report={output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
