"""Guards for the configurable reward:risk target (2026-08-14 exit probe).

At a 2% stop floor the 90-day exit mix was 6 targets / 3 stops -- two
thirds of trades hit exactly 2R and stopped, the signature of a target
that truncates its winners. Sweeping RR at constant 66.7% win rate:

    RR 1.5 -> +2.62%/trade      RR 3.0 -> +7.88%/trade
    RR 2.0 -> +4.37%/trade      RR 4.0 -> +9.28% (win rate falls to 55.6%)

Unproven (n=9, nested data, CI spans zero), so the workflows probe RR=3.0
to gather out-of-sample evidence. RR is the only lever that does not
reduce trade count: it moves the exit, not the entry gate.

Deliberately NOT applied to Gale: its best trade ever reached 1.34R, so a
target at 1.5R or beyond is unreachable and RR is not a lever there.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from tempest.trader import PaperTrader  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
RR_VAR = "TEMPEST_RR_TARGET"
PROBE_RR = 3.0


def paper_steps():
    steps = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        spec = yaml.safe_load(path.read_text())
        for job in (spec.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                if "paper_trade.py" in run:
                    steps.append((path.name, step.get("name", "?"),
                                  run, step.get("env") or {}))
    return steps


class TestRRTargetPlumbing:
    def test_default_is_the_course_prior(self, monkeypatch):
        monkeypatch.delenv(RR_VAR, raising=False)
        trader = PaperTrader.__new__(PaperTrader)
        assert trader._rr_target() == 2.0

    def test_env_override(self, monkeypatch):
        trader = PaperTrader.__new__(PaperTrader)
        monkeypatch.setenv(RR_VAR, "3.0")
        assert trader._rr_target() == 3.0

    def test_malformed_env_falls_back(self, monkeypatch):
        trader = PaperTrader.__new__(PaperTrader)
        monkeypatch.setenv(RR_VAR, "three")
        assert trader._rr_target() == 2.0

    def test_nonpositive_is_clamped(self, monkeypatch):
        """A zero or negative RR would place the target at/below entry."""
        trader = PaperTrader.__new__(PaperTrader)
        for bad in ["0", "-1", "-99"]:
            monkeypatch.setenv(RR_VAR, bad)
            assert trader._rr_target() > 0


class TestWorkflowWiring:
    def test_tempest_steps_probe_three_r(self):
        tempest = [(wf, name, env) for wf, name, run, env in paper_steps()
                   if "gale_paper_trade.py" not in run]
        assert tempest, "no Tempest paper steps found"
        for wf, name, env in tempest:
            assert env.get(RR_VAR) is not None, (
                f"{wf} step {name!r} does not set {RR_VAR}"
            )
            assert float(str(env[RR_VAR]).strip('"')) == PROBE_RR

    def test_gale_is_left_at_the_default(self):
        """Gale's best excursion was 1.34R; a 3R target is unreachable."""
        gale = [(wf, name, env) for wf, name, run, env in paper_steps()
                if "gale_paper_trade.py" in run]
        assert gale, "no Gale paper step found"
        for wf, name, env in gale:
            assert RR_VAR not in env, (
                f"{wf} step {name!r} sets {RR_VAR}. Gale never reaches even "
                "1.5R, so raising its target only widens the loss."
            )

    def test_risk_floor_still_set_everywhere(self):
        """The RR probe must not have displaced the risk-floor probe."""
        for wf, name, _run, env in paper_steps():
            assert "TEMPEST_MIN_RISK_BPS" in env, (
                f"{wf} step {name!r} lost TEMPEST_MIN_RISK_BPS"
            )


class TestTargetGeometry:
    @pytest.mark.parametrize("rr", [1.5, 2.0, 3.0, 4.0])
    def test_target_scales_with_rr(self, monkeypatch, rr):
        trader = PaperTrader.__new__(PaperTrader)
        monkeypatch.setenv(RR_VAR, str(rr))
        fill, stop = 10.0, 9.8
        risk = fill - stop
        assert fill + trader._rr_target() * risk == pytest.approx(
            fill + rr * risk
        )

    def test_higher_rr_needs_lower_win_rate(self):
        """Sanity: breakeven p = (r + c) / (r * (1 + R)) falls as R rises."""
        r, c = 0.02, 0.01
        p2 = (r + c) / (r * (1 + 2.0))
        p3 = (r + c) / (r * (1 + 3.0))
        assert p3 < p2
        assert p2 == pytest.approx(0.50)
        assert p3 == pytest.approx(0.375)
