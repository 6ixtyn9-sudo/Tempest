"""Durable Gale backtest report contract."""

import json
import sys

import pandas as pd

import scripts.run_gale_backtest as gale_report


def test_gale_backtest_writes_separate_strategy_report(tmp_path, monkeypatch):
    output = tmp_path / "gale.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_gale_backtest.py", "--symbols", "GALE", "--output", str(output)],
    )
    monkeypatch.setattr(gale_report, "load_from_warehouse", lambda symbol: pd.DataFrame())
    monkeypatch.setattr(gale_report, "load_screen_observations", lambda: pd.DataFrame())

    assert gale_report.main() == 0

    payload = json.loads(output.read_text())
    assert payload["strategy_id"] == "gale_orb5"
    assert payload["symbols"] == ["GALE"]
    assert payload["aggregate"] == {"n": 0}
    assert payload["reports"] == []
