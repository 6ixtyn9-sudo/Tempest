"""Durable backtest evidence contract."""

import json
import sys

import scripts.run_backtest as run_backtest_script


def test_run_backtest_writes_durable_json_report(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_backtest.py", "--symbols", "YXT", "--output", str(output)],
    )
    monkeypatch.setattr(
        run_backtest_script,
        "load_from_warehouse",
        lambda symbol: __import__("pandas").DataFrame(),
    )

    assert run_backtest_script.main() == 0

    payload = json.loads(output.read_text())
    assert payload["symbols"] == ["YXT"]
    assert payload["aggregate"] == {"n": 0}
    assert payload["reports"] == []
    assert payload["generated_at_utc"]
    assert payload["code_revision"]
