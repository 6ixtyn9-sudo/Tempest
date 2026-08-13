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


STATUS_PATH = None


def _status(stage: str, detail: str = "") -> None:
    """Append one line to localdata/paper_status.csv so the paper step's
    outcome is visible in git. Actions logs need a sign-in, and the step is
    wrapped in `|| echo` so a failure never turns the run red."""
    import csv, os
    from pathlib import Path as _P
    from tempest.config import DATA_DIR as _D
    override = STATUS_PATH or os.getenv("TEMPEST_STATUS_PATH")
    path = _P(override) if override else _D / "paper_status.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp_utc", "stage", "detail"])
        w.writerow([datetime.now(timezone.utc).isoformat(), stage, detail])


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
        _status("client_ok", "paper trading client constructed")
    except RuntimeError as e:
        print(f"ERROR: {e}")
        _status("client_failed", str(e))
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"ERROR building paper client: {e}")
        _status("client_failed", f"{type(e).__name__}: {e}")
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
        _status("no_candidates", "screen returned zero qualifiers")
        print("No candidates today — nothing to do.")
        return 0

    broker = broker_mod
    source = AlpacaSource()
    trader = PaperTrader(broker, source, limits=limits)
    if not args.dry_run:
        try:
            equity = get_account_equity()
            print(f"Paper equity: ${equity:,.2f}")
            _status("equity", f"{equity:.2f}")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            blob = str(e).lower()
            auth = any(t in blob for t in ("unauthorized", "forbidden", "401", "403"))
            _status("auth_failed" if auth else "equity_failed", msg)
            print(f"FATAL: could not read paper equity: {msg}")
            if auth:
                print("Alpaca rejected the credentials. ALPACA_API_KEY must be the "
                      "short PK... key ID and ALPACA_SECRET_KEY the long secret.")
            return 1

    result = trader.run_once(candidates, dry_run=args.dry_run)
    print(f"\nOpen positions: {result['open_positions']}")
    for e in result["exits"]:
        print(f"  [EXIT] {e}")
    for e in result["entries"]:
        print(f"  [ENTRY] {e}")
    _status("pass_done",
            f"open={result['open_positions']} exits={len(result['exits'])} "
            f"entries={','.join(str(e.get('action')) for e in result['entries']) or 'none'}")
    print(f"\nPass done {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
