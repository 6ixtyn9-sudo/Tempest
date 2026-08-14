"""Point-in-time Gale backtest tests."""

import pandas as pd

from tempest.gale_backtest import run_gale_backtest
from tests.conftest import make_session_1m


def completed_gale_frame():
    opens = [10.00, 10.08, 10.12, 10.15, 10.18, 10.24, 10.31, 10.36, 10.42]
    highs = [10.12, 10.18, 10.20, 10.24, 10.30, 10.34, 10.38, 10.42, 11.10]
    lows = [9.98, 10.05, 10.09, 10.12, 10.16, 10.22, 10.29, 10.34, 10.40]
    closes = [10.08, 10.12, 10.15, 10.18, 10.24, 10.31, 10.36, 10.40, 11.00]
    volumes = [100_000, 110_000, 90_000, 100_000, 100_000, 200_000, 150_000, 140_000, 180_000]
    return make_session_1m(opens, highs, lows, closes, volumes, symbol="GALE")


def observation(captured_at="2026-08-03T13:30:30+00:00"):
    return pd.DataFrame([{
        "captured_at_utc": pd.Timestamp(captured_at),
        "session_date": "2026-08-03",
        "symbol": "GALE",
        "tradeable": True,
        "gap_pct": 8.0,
        "relvol": 12.0,
    }])


def test_gale_backtest_requires_available_screen_observation():
    bars = completed_gale_frame()

    valid = run_gale_backtest(bars, "GALE", observations=observation())
    future = run_gale_backtest(
        bars, "GALE", observations=observation("2026-08-03T14:00:00+00:00")
    )

    assert valid["n_trades"] == 1
    assert valid["trades"][0]["exit_reason"] == "target"
    assert future["n_trades"] == 0
    assert future["rejected_unavailable"] == 1
