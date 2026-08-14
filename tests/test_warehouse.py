"""Tests for the parquet warehouse."""

import pandas as pd
import pytest

from tempest import warehouse
from tempest.warehouse import load_from_warehouse, sanitize_symbol, save_to_warehouse


def test_sanitize_symbol_rejects_bad(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "WAREHOUSE_DIR", tmp_path)
    with pytest.raises(ValueError):
        sanitize_symbol("bad/symbol")
    with pytest.raises(ValueError):
        sanitize_symbol("has space")


def test_save_then_load_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "WAREHOUSE_DIR", tmp_path)
    ts = pd.date_range("2026-08-03", periods=3, freq="1min", tz="UTC")
    df = pd.DataFrame({
        "symbol": ["YXT"] * 3,
        "bar_ts_utc": ts,
        "open": [10, 11, 12], "high": [10.5, 11.5, 12.5],
        "low": [9.5, 10.5, 11.5], "close": [10.2, 11.2, 12.2],
        "volume": [1000, 2000, 3000],
    })
    assert save_to_warehouse(df) == 3
    loaded = load_from_warehouse("YXT")
    assert len(loaded) == 3
    assert loaded["close"].tolist() == [10.2, 11.2, 12.2]

    # Re-save with an overlapping bar -> keep-last dedup.
    df2 = pd.DataFrame({
        "symbol": ["YXT"],
        "bar_ts_utc": [ts[1]],
        "open": [11.5], "high": [12.0], "low": [11.0], "close": [11.9],
        "volume": [2500],
    })
    save_to_warehouse(df2)
    loaded2 = load_from_warehouse("YXT")
    assert len(loaded2) == 3
    assert loaded2["close"].iloc[1] == 11.9
