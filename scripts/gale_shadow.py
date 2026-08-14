#!/usr/bin/env python3
"""Run one Gale ORB5 shadow pass. No broker orders exist in this program."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.gale_shadow import (  # noqa: E402
    append_screen_rows,
    append_status,
    discover_shadow_signals,
    load_signals,
    save_signals,
    settle_open_signals,
)
from tempest.sources import tradingview  # noqa: E402
from tempest.sources.alpaca import AlpacaSource  # noqa: E402
from tempest.strategy import REL_VOL_TRADE_MAX  # noqa: E402


def main() -> int:
    now = datetime.now(timezone.utc)
    screen_rows = tradingview.screen(tradingview.build_filter())
    if not screen_rows and tradingview.LAST_ERROR:
        append_status("screen_failed", tradingview.LAST_ERROR)
        print(f"Gale screen failed: {tradingview.LAST_ERROR}")
        return 1

    observed = []
    candidates = set()
    for row in screen_rows:
        item = dict(row)
        item["tradeable"] = float(row.get("relvol") or 0) <= REL_VOL_TRADE_MAX
        observed.append(item)
        if item["tradeable"]:
            candidates.add(str(row["symbol"]).upper())
    screen = append_screen_rows(observed, now)

    existing = load_signals()
    open_symbols = set(
        existing.loc[existing["status"].astype(str) == "open", "symbol"]
        .astype(str).str.upper()
    ) if not existing.empty else set()
    symbols = sorted(candidates | open_symbols)

    try:
        source = AlpacaSource()
    except Exception as exc:  # noqa: BLE001
        append_status("data_failed", f"{type(exc).__name__}: {exc}")
        print(f"Gale data client failed: {exc}")
        return 1

    bars_by_symbol = {}
    for symbol in symbols:
        bars = source.fetch_1m(symbol, now - timedelta(days=10), now)
        if bars is not None and not bars.empty:
            bars_by_symbol[symbol] = bars

    settled = settle_open_signals(existing, bars_by_symbol)
    updated, new_count = discover_shadow_signals(
        screen, settled, bars_by_symbol, now
    )
    save_signals(updated)
    closed_count = 0
    if not existing.empty and not updated.empty:
        before = int((existing["status"].astype(str) == "closed").sum())
        after = int((updated["status"].astype(str) == "closed").sum())
        closed_count = max(0, after - before)
    detail = (
        f"screened={len(screen_rows)} tradeable={len(candidates)} "
        f"bars={len(bars_by_symbol)} new={new_count} closed={closed_count} "
        f"open={int((updated['status'].astype(str) == 'open').sum()) if not updated.empty else 0}"
    )
    append_status("pass_done", detail)
    print(f"Gale shadow: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
