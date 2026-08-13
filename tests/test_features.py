"""Tests for the 1m feature computation."""

import numpy as np
import pandas as pd

from tempest.features import compute_features
from tests.conftest import squeeze_pullback_break_frame


def test_features_present_and_lookahead_free():
    df = squeeze_pullback_break_frame()
    feat = compute_features(df)
    for col in ("vwap", "ema9", "gap_open", "relvol", "relvol_asof", "cum_ret", "ret_5"):
        assert col in feat.columns, col
    # No look-ahead: features at bar i depend only on bars <= i.
    # vwap at the first bar equals the first bar's typical price.
    assert not np.isnan(feat["vwap"].iloc[0])


def test_compute_features_excludes_extended_hours_bars():
    df = squeeze_pullback_break_frame()
    pre = df.iloc[[0]].copy()
    pre["bar_ts_utc"] = pd.Timestamp("2026-08-03 12:00:00+00:00")
    post = df.iloc[[0]].copy()
    post["bar_ts_utc"] = pd.Timestamp("2026-08-03 20:30:00+00:00")

    feat = compute_features(pd.concat([pre, df, post], ignore_index=True))

    assert len(feat) == len(df)
    ny = feat["bar_ts_utc"].dt.tz_convert("America/New_York")
    minute = ny.dt.hour * 60 + ny.dt.minute
    assert minute.min() >= 9 * 60 + 30
    assert minute.max() < 16 * 60


def test_gap_open_is_overnight_gap_on_first_bar_only():
    import pandas as pd
    df = squeeze_pullback_break_frame()
    df2 = df.copy()
    df2["open"] += 1.5
    df2["high"] += 1.5
    df2["low"] += 1.5
    df2["close"] += 1.5
    df2["bar_ts_utc"] = df2["bar_ts_utc"] + pd.Timedelta(days=1)
    df2["session"] = df2["bar_ts_utc"].dt.tz_convert("America/New_York").dt.date
    feat = compute_features(pd.concat([df, df2], ignore_index=True))
    # First session's first bar has no prior close -> NaN.
    assert np.isnan(feat["gap_open"].iloc[0])
    # Second session's first bar carries the overnight gap (~+1.5/10.9).
    second_first = feat["session"].ne(feat["session"].iloc[0]).idxmax()
    assert not np.isnan(feat["gap_open"].iloc[second_first])
    assert feat["gap_open"].iloc[second_first] > 0.03
    # All other bars are NaN.
    nonfirst = feat.drop(index=[0, second_first])
    assert nonfirst["gap_open"].isna().all()
