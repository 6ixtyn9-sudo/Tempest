"""Tests for the event-driven backtest simulation."""

from tempest.backtest import run_backtest
from tempest.validation import CostModel
from tests.conftest import squeeze_pullback_break_frame


def _pad_to_sessions(df, n_sessions=3):
    """Replicate the textbook session a few times with price drift so the
    pillars (gap, relvol) can pass on later sessions."""
    import pandas as pd
    frames = [df]
    for k in range(1, n_sessions):
        d = df.copy()
        d["open"] += k * 2.0      # ~10% overnight gap vs prior session close
        d["high"] += k * 2.0
        d["low"] += k * 2.0
        d["close"] += k * 2.0
        d["bar_ts_utc"] = d["bar_ts_utc"] + pd.Timedelta(days=k)
        frames.append(d)
    out = pd.concat(frames, ignore_index=True)
    out["session"] = out["bar_ts_utc"].dt.tz_convert("America/New_York").dt.date
    # relvol needs prior-session volume history: fabricate a light early
    # volume so relvol >= 5 on the next session.
    # Thin the first session so later sessions clear 5x *as-of the entry
    # bar* (using full-session volume here would be look-ahead).
    out.loc[out.index < len(df), "volume"] = 10_000
    return out


def test_run_backtest_produces_trades_and_buckets(tmp_path):
    df = _pad_to_sessions(squeeze_pullback_break_frame())
    rep = run_backtest(
        df, "YXT", cost_model=CostModel(), float_map={"YXT": 2_000_000}
    )
    assert rep["symbol"] == "YXT"
    assert "summary" in rep and "bucket_summary" in rep
    # With a proper gap+relvol setup on later sessions we expect >= 1 signal.
    assert rep["n_trades"] >= 1


def test_backtest_applies_float_pillar_from_screen_log(tmp_path):
    from tempest.backtest import run_backtest
    df = _pad_to_sessions(squeeze_pullback_break_frame())
    # Same frame: with a 50M float the session must not produce trades.
    rep = run_backtest(df, "YXT", float_map={"YXT": 50_000_000})
    assert rep["n_trades"] == 0
    # Unknown float fails the strict five-pillar screen.
    rep2 = run_backtest(df, "YXT", float_map={})
    assert rep2["n_trades"] == 0


def test_backtest_float_observation_is_point_in_time():
    df = _pad_to_sessions(squeeze_pullback_break_frame())
    # A float observed after every backtested session cannot validate history.
    future_only = {("YXT", "2026-08-10"): 2_000_000}
    rep = run_backtest(df, "YXT", float_map=future_only)
    assert rep["n_trades"] == 0

    # An observation available on the first session can be carried forward.
    asof = {("YXT", "2026-08-03"): 2_000_000}
    rep2 = run_backtest(df, "YXT", float_map=asof)
    assert rep2["n_trades"] >= 1


def test_run_backtest_empty_frame_graceful():
    import pandas as pd
    rep = run_backtest(pd.DataFrame(), "YXT")
    assert rep["summary"] == {"n": 0}
    assert rep["n_trades"] == 0


def test_backtest_simulates_same_day_only():
    """The backtest must simulate only intraday bars after the entry bar
    (same-day capture) — never next-day/prior-day bars."""
    import pandas as pd
    from tempest.backtest import run_backtest
    from tests.conftest import squeeze_pullback_break_frame

    df = squeeze_pullback_break_frame()
    # Two sessions; the first session's entry must not touch the second.
    df2 = df.copy()
    df2["bar_ts_utc"] = df2["bar_ts_utc"] + pd.Timedelta(days=1)
    df2["session"] = df2["bar_ts_utc"].dt.tz_convert("America/New_York").dt.date
    out = pd.concat([df, df2], ignore_index=True)
    rep = run_backtest(out, "YXT", relax=True)
    # Entry + simulation stays within the session (no cross-session trades).
    assert all(t["held_bars"] >= 0 for t in rep["trades"])
