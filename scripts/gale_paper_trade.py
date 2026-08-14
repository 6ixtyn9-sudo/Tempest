#!/usr/bin/env python3
"""Run one Gale ORB5 Alpaca PAPER pass through shared Tempest safety rails."""

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest import broker as broker_mod  # noqa: E402
from tempest.broker import get_account_equity  # noqa: E402
from tempest.gale_shadow import append_screen_rows, append_status  # noqa: E402
from tempest.gale_trader import GalePaperTrader  # noqa: E402
from tempest.risk import RiskLimits, is_halted  # noqa: E402
from tempest.sources import tradingview  # noqa: E402
from tempest.sources.alpaca import AlpacaSource  # noqa: E402
from tempest.strategy import REL_VOL_TRADE_MAX  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-notional", type=float, default=1000.0)
    parser.add_argument("--max-risk", type=float, default=50.0)
    parser.add_argument("--max-open", type=int, default=3)
    parser.add_argument("--max-daily-loss", type=float, default=200.0)
    args = parser.parse_args()

    if is_halted():
        append_status("halted", "global HALT_TRADING.flag set")
        print("Gale halted by global HALT_TRADING.flag")
        return 0

    try:
        broker_mod.get_trading_client()
        append_status("client_ok", "Gale paper client constructed")
    except Exception as exc:  # noqa: BLE001
        append_status("client_failed", f"{type(exc).__name__}: {exc}")
        print(f"Gale paper client failed: {exc}")
        return 1

    rows = tradingview.screen(tradingview.build_filter())
    if not rows and tradingview.LAST_ERROR:
        append_status("screen_failed", tradingview.LAST_ERROR)
        print(f"Gale screen failed: {tradingview.LAST_ERROR}")
        return 1
    observed = []
    candidates = []
    for row in rows:
        item = dict(row)
        item["tradeable"] = float(row.get("relvol") or 0) <= REL_VOL_TRADE_MAX
        observed.append(item)
        if item["tradeable"]:
            candidates.append(str(row["symbol"]).upper())
        else:
            append_status(
                "skip_extreme",
                f"{row['symbol']}: relvol={float(row.get('relvol') or 0):.1f} > {REL_VOL_TRADE_MAX}",
            )
    now = datetime.now(timezone.utc)
    screen_evidence = append_screen_rows(observed, now)

    limits = RiskLimits(
        max_open_positions=args.max_open,
        max_notional_per_position=args.max_notional,
        max_risk_per_position=args.max_risk,
        max_daily_realized_loss=args.max_daily_loss,
        per_symbol_cooldown_seconds=3600,
        horizon_bars=15,
    )
    try:
        source = AlpacaSource()
        trader = GalePaperTrader(
            broker_mod, source, screen_evidence=screen_evidence, limits=limits
        )
        if not args.dry_run:
            equity = get_account_equity()
            append_status("equity", f"{equity:.2f}")
            print(f"Gale paper equity: ${equity:,.2f}")
        result = trader.run_once(sorted(set(candidates)), dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - unknown account state fails closed
        detail = f"{type(exc).__name__}: {exc}"
        append_status("state_failed", detail)
        print(f"FATAL: Gale paper pass stopped: {detail}")
        return 1

    tally = Counter(str(event.get("action")) for event in result["entries"])
    detail = (
        f"open={result['open_positions']} slots={result.get('exposure_slots', 0)} "
        f"exits={len(result['exits'])} "
        f"entries={','.join(f'{key}={value}' for key, value in sorted(tally.items())) or 'none'}"
    )
    append_status("pass_done", detail)
    for event in result["entries"]:
        if event.get("action") in ("watching", "blocked", "rejected"):
            append_status(
                f"entry_{event.get('action')}",
                f"{event.get('symbol')}: {event.get('reason', '')}",
            )
    print(f"Gale paper: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
