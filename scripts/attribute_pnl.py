#!/usr/bin/env python3
"""Per-symbol realized P&L from the trade journal (FIFO, long-only).

Usage:
  PYTHONPATH=src python3 scripts/attribute_pnl.py
  PYTHONPATH=src python3 scripts/attribute_pnl.py --json
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.risk import load_journal, today_realized_pnl  # noqa: E402


def attribute(journal) -> dict:
    """FIFO pair entries vs exits per symbol; realized P&L per symbol."""
    if journal is None or journal.empty:
        return {}
    out: dict[str, dict] = {}
    for sym, grp in journal.groupby(journal["symbol"].astype(str).str.upper()):
        grp = grp.sort_values("timestamp_utc")
        qty = 0.0
        cost = 0.0
        realized = 0.0
        wins = 0
        trades = 0
        for _, row in grp.iterrows():
            action = str(row.get("action", ""))
            try:
                px = float(row.get("price") or row.get("exit_price") or 0)
            except (TypeError, ValueError):
                continue
            try:
                rqty = float(row.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if action == "entry" and rqty > 0:
                cost = (cost * qty + px * rqty) / (qty + rqty) if (qty + rqty) else px
                qty += rqty
            elif action in ("exit", "stop_filled", "tp_filled", "broker_closed") and rqty > 0:
                close_qty = min(rqty, qty)
                pnl = (px - cost) * close_qty
                realized += pnl
                qty -= close_qty
                trades += 1
                if pnl > 0:
                    wins += 1
        out[sym] = {
            "realized_pnl": round(realized, 2),
            "closed_trades": trades,
            "win_rate": round(wins / trades, 4) if trades else None,
            "open_qty": round(qty, 2),
        }
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    journal = load_journal()
    if journal.empty:
        print("No trades journaled yet.")
        return 0
    att = attribute(journal)
    if args.json:
        import json
        print(json.dumps(att, indent=2))
        return 0
    print(f"{'SYMBOL':8} {'realized':>10} {'trades':>6} {'win%':>6} {'open':>6}")
    total = 0.0
    for sym, a in sorted(att.items()):
        wr = f"{a['win_rate']*100:.0f}%" if a["win_rate"] is not None else "  - "
        print(f"{sym:8} {a['realized_pnl']:>10.2f} {a['closed_trades']:>6} {wr:>6} {a['open_qty']:>6.0f}")
        total += a["realized_pnl"]
    print(f"\nTotal realized: ${total:.2f}")
    print(f"Today realized: ${today_realized_pnl():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
