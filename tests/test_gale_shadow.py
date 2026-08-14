"""Gale shadow evidence is timestamped, idempotent and broker-free."""

from datetime import datetime, timezone

import pandas as pd

from tempest.gale_shadow import (
    append_screen_rows, discover_shadow_signals, load_signals, save_signals,
    settle_open_signals,
)
from tests.test_gale import gale_frame
from tests.test_gale_backtest import completed_gale_frame


def test_gale_shadow_discovers_once_and_settles(tmp_path):
    screen_path = tmp_path / "screen.csv"
    signals_path = tmp_path / "signals.csv"
    first_seen = datetime(2026, 8, 3, 13, 30, 30, tzinfo=timezone.utc)
    screen = append_screen_rows([{
        "symbol": "GALE", "close": 10.1, "gap_pct": 8.0,
        "relvol": 12.0, "float_shares": 2_000_000, "volume": 1_500_000,
        "tradeable": True,
    }], first_seen, path=screen_path)
    signal_bars = gale_frame().iloc[:6].copy()
    now = datetime(2026, 8, 3, 13, 36, tzinfo=timezone.utc)

    signals, added = discover_shadow_signals(
        screen, pd.DataFrame(), {"GALE": signal_bars}, now
    )
    signals2, added2 = discover_shadow_signals(
        screen, signals, {"GALE": signal_bars}, now
    )

    assert added == 1
    assert added2 == 0
    assert len(signals2) == 1
    assert signals2.iloc[0]["status"] == "open"
    assert signals2.iloc[0]["strategy_id"] == "gale_orb5"

    settled = settle_open_signals(signals2, {"GALE": completed_gale_frame()})
    assert settled.iloc[0]["status"] == "closed"
    assert settled.iloc[0]["exit_reason"] == "target"
    assert float(settled.iloc[0]["net_return"]) < float(settled.iloc[0]["gross_return"])

    save_signals(settled, path=signals_path)
    loaded = load_signals(path=signals_path)
    assert len(loaded) == 1
    assert loaded.iloc[0]["signal_id"] == settled.iloc[0]["signal_id"]


def test_gale_shadow_rejects_signal_not_known_by_screen(tmp_path):
    screen = append_screen_rows([{
        "symbol": "OTHER", "tradeable": True,
    }], datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
        path=tmp_path / "screen.csv")
    signals, added = discover_shadow_signals(
        screen,
        pd.DataFrame(),
        {"GALE": gale_frame().iloc[:6]},
        datetime(2026, 8, 3, 13, 36, tzinfo=timezone.utc),
    )
    assert added == 0
    assert signals.empty
