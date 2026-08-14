"""Fixed-prior Gale ORB5 strategy tests."""

import pandas as pd

from tempest.features import compute_features
from tempest.gale import STRATEGY_ID, detect_gale_orb, shadow_entry_price, target_for
from tests.conftest import make_session_1m


def gale_frame():
    opens = [10.00, 10.08, 10.12, 10.15, 10.18, 10.24, 10.31, 10.36]
    highs = [10.12, 10.18, 10.20, 10.24, 10.30, 10.34, 10.38, 10.42]
    lows = [9.98, 10.05, 10.09, 10.12, 10.16, 10.22, 10.29, 10.34]
    closes = [10.08, 10.12, 10.15, 10.18, 10.24, 10.31, 10.36, 10.40]
    volumes = [100_000, 110_000, 90_000, 100_000, 100_000, 200_000, 150_000, 140_000]
    return make_session_1m(opens, highs, lows, closes, volumes, symbol="GALE")


def test_gale_detects_first_confirmed_opening_range_breakout():
    signals = detect_gale_orb(gale_frame(), "GALE")

    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_id.startswith(f"{STRATEGY_ID}|")
    assert sig.signal_ts == pd.Timestamp("2026-08-03 13:35:00+00:00")
    assert sig.opening_range_high == 10.30
    assert sig.opening_range_low == 9.98
    assert sig.breakout_volume_ratio >= 1.5
    assert sig.trigger_price > sig.vwap


def test_gale_rejects_wide_opening_range():
    df = gale_frame()
    df.loc[0, "low"] = 9.0
    assert detect_gale_orb(df, "GALE") == []


def test_gale_rejects_breakout_without_volume_confirmation():
    df = gale_frame()
    df.loc[5, "volume"] = 100_000
    assert detect_gale_orb(df, "GALE") == []


def test_gale_rejects_breakout_close_below_vwap():
    feat = compute_features(gale_frame())
    feat.loc[5, "vwap"] = feat.loc[5, "close"] + 0.01
    assert detect_gale_orb(feat, "GALE") == []


def test_gale_shadow_price_rejects_chase_and_broken_setup():
    sig = detect_gale_orb(gale_frame(), "GALE")[0]
    assert shadow_entry_price(sig, 10.32) == 10.32
    assert shadow_entry_price(sig, 10.40) is None
    assert shadow_entry_price(sig, 10.20) is None
    assert shadow_entry_price(sig, sig.stop_price) is None
    assert target_for(10.32, 9.98) > 10.32
