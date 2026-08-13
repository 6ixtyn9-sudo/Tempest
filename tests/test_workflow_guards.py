"""Static safety contracts for the externally dispatched workflows."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_daily_capture_always_checks_et_window_and_fails_closed():
    text = (ROOT / ".github/workflows/daily_capture.yml").read_text()
    check_block = text.split("- name: Check ET trading window", 1)[1].split(
        "- name: Checkout", 1
    )[0]

    assert "workflow_dispatch:" in text
    assert "github.event_name == 'schedule'" not in check_block
    assert "if:" not in check_block
    assert "steps.window.outputs.run != 'false'" not in text
    assert text.count("steps.window.outputs.run == 'true'") == 9


def test_paper_poll_always_checks_et_window_and_fails_closed():
    text = (ROOT / ".github/workflows/paper_poll.yml").read_text()
    check_block = text.split("- name: Check ET trading window", 1)[1].split(
        "- name: Checkout", 1
    )[0]

    assert "if:" not in check_block
    assert "steps.window.outputs.run != 'false'" not in text
    assert "steps.window.outputs.run == 'true'" in text
