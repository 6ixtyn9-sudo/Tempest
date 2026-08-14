"""Guards for the minimum-risk rails added 2026-08-14.

Motivation (measured, not hypothetical). Replaying live 1m bars on
2026-08-14 through detect_first_pullback produced these signals:

    RRGB  entry 9.725  stop 9.715  -> risk $0.0100/sh (10.3 bps)
    RRGB  entry 8.090  stop 8.080  -> risk $0.0100/sh (12.4 bps)
    RRGB  entry 10.230 stop 10.220 -> risk $0.0100/sh ( 9.8 bps)

CostModel models 100 bps round-trip. A 10 bps stop is inside the spread:
it is hit by the bid/ask rather than by price, and the "2R win" of $2.04 on
a $992 position cannot cover the round trip. These are not trades, they are
guaranteed losses.

Two independent rails:
  * strategy.MIN_RISK_FRACTION - geometric, rejects degenerate patterns
  * PaperTrader._min_risk_per_share - economic, cost-aware, blocks entries
"""

import numpy as np
import pandas as pd
import pytest

from tempest.strategy import (
    MIN_RISK_FRACTION,
    detect_first_pullback,
    screen_pillars,
)
from tempest.trader import PaperTrader
from tempest.validation import CostModel


def make_bars(rows, session="2026-08-14"):
    """rows: list of (open, high, low, close, volume)."""
    start = pd.Timestamp(f"{session} 13:30:00", tz="UTC")
    frame = pd.DataFrame(
        rows, columns=["open", "high", "low", "close", "volume"]
    ).astype(float)
    frame["bar_ts_utc"] = [start + pd.Timedelta(minutes=i) for i in range(len(frame))]
    frame["session"] = pd.Timestamp(session).date()
    # Features the detector reads. Keep vwap/ema9 permissive so the tests
    # isolate the risk floor rather than the pullback filters.
    frame["vwap"] = frame["low"].min() - 1.0
    frame["ema9"] = frame["low"].min() - 1.0
    return frame


def squeeze_then_pullback(entry_px, stop_px):
    """Build a 3-green squeeze, a light pullback to stop_px, then a breakout.

    The squeeze base is placed just under stop_px so the pullback retrace
    stays inside MAX_RETRACE (0.50); otherwise the retrace check, not the
    risk floor, would be the binding constraint and the test would be
    measuring the wrong thing. Resulting risk == entry_px - stop_px.
    """
    depth = max(entry_px - stop_px, 0.001)
    base = stop_px - depth          # retrace = depth / 2*depth = 0.50 exactly
    g1 = base + depth * 0.30
    g2 = base + depth * 0.65
    return make_bars([
        (base, g1, base - 0.001, g1 - 0.001, 5000),                 # green 1
        (g1 - 0.001, g2, g1 - 0.002, g2 - 0.001, 5000),             # green 2
        (g2 - 0.001, entry_px, g2 - 0.002, entry_px - 0.001, 5000),  # green 3
        (entry_px - 0.001, entry_px - 0.002, stop_px, stop_px + 0.0005, 500),
        (stop_px + 0.0005, entry_px + depth * 3, stop_px, entry_px + depth * 2, 9000),
    ])


class TestDetectorGeometricFloor:
    def test_degenerate_penny_stop_is_rejected(self):
        """The exact RRGB geometry that fired in production."""
        bars = squeeze_then_pullback(entry_px=9.725, stop_px=9.715)
        assert detect_first_pullback(bars, "RRGB") == []

    def test_healthy_stop_still_fires(self):
        """The floor must not suppress legitimate setups."""
        bars = squeeze_then_pullback(entry_px=9.80, stop_px=9.50)
        signals = detect_first_pullback(bars, "TEST")
        assert len(signals) == 1
        sig = signals[0]
        risk = sig.entry_price - sig.stop_price
        assert risk >= MIN_RISK_FRACTION * sig.entry_price

    @pytest.mark.parametrize("fraction", [0.0005, 0.001, 0.002])
    def test_below_floor_always_rejected(self, fraction):
        entry = 10.0
        bars = squeeze_then_pullback(entry_px=entry, stop_px=entry * (1 - fraction))
        assert detect_first_pullback(bars, "TEST") == []

    def test_floor_is_tunable(self):
        """A caller may tighten the floor; it must be honoured."""
        bars = squeeze_then_pullback(entry_px=9.80, stop_px=9.50)
        assert detect_first_pullback(bars, "TEST", min_risk_fraction=0.0) != []
        assert detect_first_pullback(bars, "TEST", min_risk_fraction=0.50) == []

    def test_every_emitted_signal_clears_the_floor(self):
        """Property: no signal may ever carry sub-floor risk."""
        rng = np.random.default_rng(20260814)
        emitted = 0
        for _ in range(300):
            px = float(rng.uniform(2.0, 20.0))
            rows = []
            for _ in range(14):
                o = px
                c = px * (1 + float(rng.normal(0, 0.004)))
                hi = max(o, c) * (1 + abs(float(rng.normal(0, 0.002))))
                lo = min(o, c) * (1 - abs(float(rng.normal(0, 0.002))))
                rows.append((o, hi, lo, c, float(rng.integers(500, 9000))))
                px = c
            for sig in detect_first_pullback(make_bars(rows), "RAND"):
                emitted += 1
                risk = sig.entry_price - sig.stop_price
                assert risk >= MIN_RISK_FRACTION * sig.entry_price, sig.to_dict()
        assert emitted > 0, "generator produced no signals - test is vacuous"


class TestTraderEconomicRail:
    def test_default_is_the_break_even_line(self, monkeypatch):
        """2 * risk must clear the round trip: risk > round_trip / 2."""
        monkeypatch.delenv("TEMPEST_MIN_RISK_BPS", raising=False)
        trader = PaperTrader.__new__(PaperTrader)
        assert trader._min_risk_bps() == pytest.approx(
            0.5 * CostModel().round_trip_bps()
        )

    def test_floor_is_exactly_break_even(self, monkeypatch):
        """At the floor, a 2R win nets exactly zero. Below it, negative."""
        monkeypatch.delenv("TEMPEST_MIN_RISK_BPS", raising=False)
        trader = PaperTrader.__new__(PaperTrader)
        cost = CostModel().round_trip_bps()
        assert 2.0 * trader._min_risk_bps() == pytest.approx(cost)

    def test_textbook_setup_is_not_blocked(self, monkeypatch):
        """Regression: the shared textbook fixture (94.3 bps of risk) is a
        genuinely profitable setup and must survive the rail."""
        monkeypatch.delenv("TEMPEST_MIN_RISK_BPS", raising=False)
        trader = PaperTrader.__new__(PaperTrader)
        assert 0.10 > trader._min_risk_per_share(10.60)

    def test_penny_stop_is_below_the_cost_floor(self):
        """RRGB: $0.0100 risk on a $9.725 entry must not be tradeable."""
        trader = PaperTrader.__new__(PaperTrader)
        assert 0.0100 < trader._min_risk_per_share(9.725)

    def test_env_override(self, monkeypatch):
        trader = PaperTrader.__new__(PaperTrader)
        monkeypatch.setenv("TEMPEST_MIN_RISK_BPS", "50")
        assert trader._min_risk_bps() == 50.0
        assert trader._min_risk_per_share(10.0) == pytest.approx(0.05)

    def test_malformed_env_falls_back_to_default(self, monkeypatch):
        trader = PaperTrader.__new__(PaperTrader)
        monkeypatch.setenv("TEMPEST_MIN_RISK_BPS", "not-a-number")
        assert trader._min_risk_bps() == pytest.approx(
            0.5 * CostModel().round_trip_bps()
        )

    def test_negative_env_is_clamped(self, monkeypatch):
        trader = PaperTrader.__new__(PaperTrader)
        monkeypatch.setenv("TEMPEST_MIN_RISK_BPS", "-100")
        assert trader._min_risk_bps() == 0.0


class TestFloatPolicy:
    def test_missing_float_rejects_by_default(self):
        pillars = screen_pillars(
            "X", relvol=8.0, total_volume=2e6, gap_open=0.05,
            price=5.0, float_shares=None,
        )
        assert not pillars.passes
        assert "float missing" in pillars.reasons

    def test_missing_float_becomes_a_caveat_when_not_required(self):
        pillars = screen_pillars(
            "X", relvol=8.0, total_volume=2e6, gap_open=0.05,
            price=5.0, float_shares=None, require_float=False,
        )
        assert pillars.passes
        assert pillars.reasons == []
        assert any("float missing" in c for c in pillars.caveats)

    def test_oversized_float_still_rejects_when_not_required(self):
        """Downgrading MISSING data must not weaken a real pillar failure."""
        pillars = screen_pillars(
            "X", relvol=8.0, total_volume=2e6, gap_open=0.05,
            price=5.0, float_shares=50_000_000, require_float=False,
        )
        assert not pillars.passes
        assert any("float" in r for r in pillars.reasons)

    def test_caveats_are_serialised(self):
        pillars = screen_pillars(
            "X", relvol=8.0, total_volume=2e6, gap_open=0.05,
            price=5.0, float_shares=None, require_float=False,
        )
        assert "caveats" in pillars.to_dict()
