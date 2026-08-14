"""Gale remains shadow-only and isolated from Tempest execution."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gale_shadow_script_has_no_order_path():
    script = (ROOT / "scripts/gale_shadow.py").read_text()
    assert "submit_bracket" not in script
    assert "close_position" not in script
    assert "TradingClient" not in script
    assert "AlpacaSource" in script


def test_paper_poll_runs_gale_as_non_blocking_shadow_step():
    workflow = (ROOT / ".github/workflows/paper_poll.yml").read_text()
    block = workflow.split("- name: Run Gale ORB5 shadow", 1)[1].split(
        "- name: Commit paper-trade and Gale evidence", 1
    )[0]
    assert "continue-on-error: true" in block
    assert "scripts/gale_shadow.py" in block
    assert "scripts/paper_trade.py" not in block
