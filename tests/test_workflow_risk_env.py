"""The paper-trading risk floor is set in CI, so CI must stay consistent.

Context: the 90-day sweep (792 sessions, 1264 trades) proved every stop
floor <= 0.5% loses money. The live effective floor was 50 bps, i.e. inside
the proven-negative band. TEMPEST_MIN_RISK_BPS=200 in the workflows points
paper trading at the untested >= 1% region to gather out-of-sample evidence.

That value lives in YAML, far from the code that reads it. These tests fail
if the two drift apart, if a new paper-trading step forgets the variable, or
if someone sets a value back inside the proven-negative band.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from tempest.trader import PaperTrader  # noqa: E402
from tempest.validation import CostModel  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
ENV_VAR = "TEMPEST_MIN_RISK_BPS"
EXPECTED_BPS = 200.0

# Proven loss-making on 90 days of data: min_risk <= 0.005 (= 50 bps).
PROVEN_NEGATIVE_MAX_BPS = 50.0


def paper_steps():
    """Every workflow step that runs a paper trader, with its env block."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        spec = yaml.safe_load(path.read_text())
        for job in (spec.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                if "paper_trade.py" in run:
                    found.append((path.name, step.get("name", "?"),
                                  step.get("env") or {}))
    return found


def test_paper_steps_exist():
    steps = paper_steps()
    assert steps, "no paper-trading steps found - did the workflows move?"
    # Was 3 (capture, poll, gale). Gale paper execution retired 2026-08-14
    # on measured evidence, leaving Tempest's capture and poll steps.
    assert len(steps) >= 2, (
        f"expected >=2 paper steps (capture, poll), found {len(steps)}"
    )


@pytest.mark.parametrize("wf,name,env", paper_steps())
def test_every_paper_step_sets_the_floor(wf, name, env):
    assert ENV_VAR in env, (
        f"{wf} step {name!r} runs a paper trader without {ENV_VAR}. "
        "It would silently fall back to the 50 bps default, which the "
        "90-day sweep proved loss-making."
    )


@pytest.mark.parametrize("wf,name,env", paper_steps())
def test_floor_is_outside_the_proven_negative_band(wf, name, env):
    value = float(str(env[ENV_VAR]).strip().strip('"'))
    assert value > PROVEN_NEGATIVE_MAX_BPS, (
        f"{wf} step {name!r} sets {ENV_VAR}={value}, inside the band proven "
        f"loss-making (<= {PROVEN_NEGATIVE_MAX_BPS} bps)."
    )


@pytest.mark.parametrize("wf,name,env", paper_steps())
def test_all_steps_agree(wf, name, env):
    value = float(str(env[ENV_VAR]).strip().strip('"'))
    assert value == EXPECTED_BPS, (
        f"{wf} step {name!r} sets {value}, expected {EXPECTED_BPS}. "
        "Tempest and Gale share a broker and account-level risk rails; "
        "differing floors make the evidence hard to attribute."
    )


def test_code_honours_the_env_var(monkeypatch):
    trader = PaperTrader.__new__(PaperTrader)
    monkeypatch.setenv(ENV_VAR, str(EXPECTED_BPS))
    assert trader._min_risk_bps() == EXPECTED_BPS
    assert trader._min_risk_per_share(10.0) == pytest.approx(0.20)


def test_gale_inherits_the_same_rail(monkeypatch):
    """Gale subclasses PaperTrader and must not bypass the floor."""
    from tempest.gale_trader import GalePaperTrader
    assert GalePaperTrader._min_risk_bps is PaperTrader._min_risk_bps
    gale = GalePaperTrader.__new__(GalePaperTrader)
    monkeypatch.setenv(ENV_VAR, str(EXPECTED_BPS))
    assert gale._min_risk_bps() == EXPECTED_BPS


def test_probe_target_is_economically_coherent():
    """200 bps must clear the breakeven line for a 2R target."""
    round_trip = CostModel().round_trip_bps()
    assert EXPECTED_BPS > round_trip / 2.0, (
        f"{EXPECTED_BPS} bps does not clear breakeven "
        f"({round_trip / 2.0} bps) for a 2R exit"
    )
    # Breakeven win rate p = (r + c) / (r * (1 + R)) must be attainable.
    r = EXPECTED_BPS / 10000.0
    c = round_trip / 10000.0
    assert (r + c) / (r * 3.0) < 0.60, "required win rate is implausibly high"
