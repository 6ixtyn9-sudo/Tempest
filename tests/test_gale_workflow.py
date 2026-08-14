"""Gale paper execution is explicit, paper-guarded and evidence-isolated."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gale_research_shadow_script_still_has_no_order_path():
    script = (ROOT / "scripts/gale_shadow.py").read_text()
    assert "submit_bracket" not in script
    assert "close_position" not in script
    assert "TradingClient" not in script


def test_gale_paper_execution_is_retired():
    """Retired 2026-08-14: n=62, mean -1.000%/trade, CI [-1.32, -0.67],
    and gross edge zero (net -1.000% + 100bps cost = +0.000%), so neither
    cheaper execution nor a wider target can rescue it. Guards against
    reintroduction without new evidence."""
    workflow = (ROOT / ".github/workflows/paper_poll.yml").read_text()
    assert "gale_paper_trade.py" not in workflow
    assert "Run Gale ORB5 paper trade" not in workflow


def test_gale_strategy_code_is_retained():
    """Retiring execution must not delete the evidence trail."""
    for path in ["src/tempest/gale.py", "src/tempest/gale_backtest.py",
                 "scripts/gale_shadow.py"]:
        assert (ROOT / path).exists(), f"{path} should be kept as evidence"
