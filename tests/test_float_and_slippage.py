"""Guards for the 2026-08-14 breadth change and the slippage measurement.

Float pillar raised 20M -> 100M. The scanner constant and the validation
constant must agree: tradingview.build_filter selects the universe and
strategy.screen_pillars re-validates it, so a mismatch would silently
reject every symbol the scan just admitted.
"""

import numpy as np
import pytest

from tempest.sources.tradingview import FLOAT_MAX_DEFAULT, build_filter
from tempest.strategy import FLOAT_MAX, screen_pillars


def filter_value(filters, field):
    for f in filters:
        if f["left"] == field:
            return f["right"]
    raise AssertionError(f"{field} not in filter payload")


class TestFloatPillarConsistency:
    def test_scanner_and_validator_agree(self):
        """The single most important invariant of this change."""
        assert FLOAT_MAX == FLOAT_MAX_DEFAULT, (
            "strategy.FLOAT_MAX and tradingview.FLOAT_MAX_DEFAULT differ. "
            "The scan would admit symbols that screen_pillars then rejects."
        )

    def test_float_max_is_100m(self):
        assert FLOAT_MAX == 100_000_000

    def test_build_filter_uses_the_new_default(self):
        assert filter_value(build_filter(), "float_shares_outstanding") == \
            FLOAT_MAX_DEFAULT

    def test_other_pillars_unchanged(self):
        """Breadth came from float alone; nothing else was loosened."""
        filters = build_filter()
        assert filter_value(filters, "close") == [2.0, 20.0]
        assert filter_value(filters, "gap") == 2.0
        assert filter_value(filters, "relative_volume_10d_calc") == 5.0
        assert filter_value(filters, "volume") == 1_000_000

    @pytest.mark.parametrize("float_shares,expected", [
        (500_000, True),        # the microcaps that already qualified
        (3_900_000, True),
        (20_500_000, True),     # newly admitted by the change
        (52_300_000, True),
        (99_000_000, True),
        (100_000_000, False),   # boundary is exclusive
        (150_000_000, False),
    ])
    def test_float_gate_boundary(self, float_shares, expected):
        pillars = screen_pillars(
            "X", relvol=8.0, total_volume=2e6, gap_open=0.05,
            price=5.0, float_shares=float_shares,
        )
        assert pillars.passes is expected

    def test_caller_can_still_override(self):
        """Comparability: the old 20M universe must remain reproducible."""
        assert filter_value(
            build_filter(float_max=20_000_000), "float_shares_outstanding"
        ) == 20_000_000


class TestSlippageMath:
    """The bps helper drives every cost conclusion; verify its sign."""

    def setup_method(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "scripts" / "measure_slippage.py"
        spec = importlib.util.spec_from_file_location("ms", path)
        self.ms = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ms)

    def test_paying_more_than_intended_is_positive(self):
        """Adverse fills must read positive, or costs cancel out."""
        assert self.ms.bps(10.05, 10.00) == pytest.approx(50.0)

    def test_price_improvement_is_negative(self):
        assert self.ms.bps(9.95, 10.00) == pytest.approx(-50.0)

    def test_exact_fill_is_zero(self):
        assert self.ms.bps(10.00, 10.00) == 0.0

    @pytest.mark.parametrize("intended", [0, -1, None, float("nan")])
    def test_invalid_reference_is_nan(self, intended):
        assert np.isnan(self.ms.bps(10.0, intended))

    def test_describe_ignores_nan(self):
        stats = self.ms.describe([10.0, float("nan"), 20.0], "x")
        assert stats["n"] == 2
        assert stats["mean_bps"] == pytest.approx(15.0)

    def test_describe_empty_returns_none(self):
        assert self.ms.describe([], "x") is None
        assert self.ms.describe([float("nan")], "x") is None
