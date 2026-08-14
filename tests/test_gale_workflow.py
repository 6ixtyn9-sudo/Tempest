"""Gale paper execution is explicit, paper-guarded and evidence-isolated."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gale_research_shadow_script_still_has_no_order_path():
    script = (ROOT / "scripts/gale_shadow.py").read_text()
    assert "submit_bracket" not in script
    assert "close_position" not in script
    assert "TradingClient" not in script


def test_paper_poll_runs_and_audits_gale_paper_step():
    workflow = (ROOT / ".github/workflows/paper_poll.yml").read_text()
    block = workflow.split("- name: Run Gale ORB5 paper trade", 1)[1].split(
        "- name: Commit Tempest and Gale evidence", 1
    )[0]
    assert "id: gale" in block
    assert 'TEMPEST_PAPER: "1"' in block
    assert "scripts/gale_paper_trade.py" in block
    assert "gale_exit=$code" in block
    assert "continue-on-error" not in block
    assert "Fail run if Gale paper trade failed" in workflow
