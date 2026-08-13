"""Tests for the video-rule encodings: pillars and first-pullback detector."""

import numpy as np
import pandas as pd

from tempest.features import compute_features
from tempest.strategy import (
    detect_first_pullback, screen_pillars, PullbackSignal,
)
from tests.conftest import squeeze_pullback_break_frame


def test_screen_pillars_pass():
    p = screen_pillars(
        "YXT", relvol=14.0, total_volume=44_000_000, gap_open=0.08,
        price=8.0, float_shares=2_870_000,
    )
    assert p.passes is True
    assert p.reasons == []


def test_screen_pillars_fail_each():
    cases = [
        dict(relvol=3.0, total_volume=44_000_000, gap_open=0.08, price=8.0, float_shares=2_870_000),
        dict(relvol=14.0, total_volume=500_000, gap_open=0.08, price=8.0, float_shares=2_870_000),
        dict(relvol=14.0, total_volume=44_000_000, gap_open=0.01, price=8.0, float_shares=2_870_000),
        dict(relvol=14.0, total_volume=44_000_000, gap_open=0.08, price=25.0, float_shares=2_870_000),
        dict(relvol=14.0, total_volume=44_000_000, gap_open=0.08, price=8.0, float_shares=50_000_000),
    ]
    for kw in cases:
        p = screen_pillars("YXT", **kw)
        assert p.passes is False, kw


def test_detect_first_pullback_on_textbook_session():
    df = squeeze_pullback_break_frame()
    feat = compute_features(df)
    sigs = detect_first_pullback(feat, "YXT")
    assert len(sigs) == 1
    s = sigs[0]
    assert isinstance(s, PullbackSignal)
    # Entry should be at the break candle (~index 7, price ~10.70).
    assert s.entry_price > 10.60
    assert 10.45 <= s.stop_price <= 10.50   # pullback low
    assert s.target_price > s.entry_price


def test_no_signal_when_pullback_breaks_vwap_or_ema():
    df = squeeze_pullback_break_frame()
    # Poison: make the pullback candle dive below VWAP/EMA9.
    df.loc[5, "low"] = 9.90
    df.loc[5, "close"] = 9.95
    feat = compute_features(df)
    sigs = detect_first_pullback(feat, "YXT")
    assert sigs == []


def test_no_signal_when_no_squeeze():
    df = squeeze_pullback_break_frame()
    df.loc[1, "close"] = 10.10   # break the green run
    feat = compute_features(df)
    sigs = detect_first_pullback(feat, "YXT")
    assert sigs == []
