#!/usr/bin/env python3
"""Run one paper-trading pass of the momentum strategy.

Screens the market (TradingView) for the five pillars, watches each
qualifier for a fresh first-pullback signal, submits DAY brackets
(entry limit + 2R take-profit + stop) on the Alpaca PAPER account, and
manages exits (horizon / near-close). Journals everything.

Usage:
  PYTHONPATH=src python3 scripts/paper_trade.py
  PYTHONPATH=src python3 scripts/paper_trade.py --dry-run
  PYTHONPATH=src python3 scripts/paper_trade.py --symbols BOXL NTHI
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.broker import get_account_equity  # noqa: E402
from tempest.risk import RiskLimits, is_halted  # noqa: E402
from tempest.sources.alpaca import AlpacaSource  # noqa: E402
from tempest.trader import PaperTrader  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--symbols", nargs="+", default=None,
                   help="Override the live screen with explicit symbols")
    p.add_argument("--max-notional", type=float, default=1000.0)
    p.add_argument("--max-open", type=int, default=3)
    p.add_argument("--max-daily-loss", type=float, default=200.0)
    p.add_argument("--cooldown-seconds", type=int, default=3600)
    p.add_argument("--horizon-bars", type=int, default=15)
    args = p.parse_args()

    limits = RiskLimits(
        max_notional_per_position=args.max_notional,
        max_open_positions=args.max_open,
        max_daily_realized_loss=args.max_daily_loss,
        per_symbol_cooldown_seconds=args.cooldown_seconds,
        horizon_bars=args.horizon_bars,
    )

    if is_halted():
        print("HALT flag set — refusing to trade. Remove localdata/HALT_TRADING.flag to resume.")
        return 0

    from tempest import broker as broker_mod
    from tempest.sources import tradingview

    # Paper guard first: this raises unless TEMPEST_PAPER=1 and keys exist.
    try:
        broker_mod.get_trading_client()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1

    # Candidates: explicit symbols, else the live five-pillar screen.
    if args.symbols:
        candidates = [s.upper() for s in args.symbols]
        print(f"Candidates (explicit): {candidates}")
    else:
        filters = tradingview.build_filter()
        rows = tradingview.screen(filters)
        candidates = sorted({r["symbol"] for r in rows})
        print(f"Candidates (screen): {candidates}")
        for r in rows:
            print(f"  {r['symbol']} gap={r['gap_pct']:.1f}% relvol={r['relvol']:.1f} "
                  f"float={r['float_shares']:,.0f}")

    if not candidates:
        print("No candidates today — nothing to do.")
        return 0

    broker = broker_mod
    source = AlpacaSource()
    trader = PaperTrader(broker, source, limits=limits)
    if not args.dry_run:
        try:
            equity = get_account_equity()
            print(f"Paper equity: ${equity:,.2f}")
        except Exception as e:  # noqa: BLE001
            print(f"Could not read equity (non-fatal): {e}")

    result = trader.run_once(candidates, dry_run=args.dry_run)
    print(f"\nOpen positions: {result['open_positions']}")
    for e in result["exits"]:
        print(f"  [EXIT] {e}")
    for e in result["entries"]:
        print(f"  [ENTRY] {e}")
    print(f"\nPass done {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
