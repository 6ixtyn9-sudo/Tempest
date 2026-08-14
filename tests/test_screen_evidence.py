"""Timestamped screen evidence contracts."""

import pandas as pd

import scripts.screen_market as screen_market
from tempest.gale_backtest import load_screen_observations


def test_legacy_screen_rows_are_marked_unknown(tmp_path, monkeypatch):
    path = tmp_path / "screen_log.csv"
    pd.DataFrame([{
        "date_utc": "2026-08-13", "symbol": "OLD", "float_shares": 1_000_000,
    }]).to_csv(path, index=False)
    monkeypatch.setattr(screen_market, "SCREEN_LOG_PATH", path)

    loaded = screen_market._load_log()

    assert loaded.iloc[0]["snapshot_quality"] == "legacy_unknown"
    assert pd.isna(loaded.iloc[0]["captured_at_utc"]) or loaded.iloc[0]["captured_at_utc"] == ""


def test_gale_excludes_legacy_rows_without_capture_timestamp(tmp_path):
    legacy = tmp_path / "legacy.csv"
    current = tmp_path / "current.csv"
    pd.DataFrame([{
        "date_utc": "2026-08-13", "symbol": "OLD", "relvol": 10,
        "passes": True,
    }]).to_csv(legacy, index=False)
    pd.DataFrame([{
        "captured_at_utc": "2026-08-14T13:30:10+00:00",
        "session_date": "2026-08-14", "symbol": "NEW", "relvol": 10,
        "passes": True,
    }]).to_csv(current, index=False)

    loaded = load_screen_observations([legacy, current])

    assert list(loaded["symbol"]) == ["NEW"]
