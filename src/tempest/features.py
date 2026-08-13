"""1-minute bar features needed by the momentum rules. All fixed-prior,
no look-ahead: each bar's features use only bars at/before it.

  gap_open     : first bar of the session open vs prior session close (NaN
                 elsewhere). The overnight demand signal.
  relvol       : bar volume / 50-day average daily volume proxy (expanding
                 mean of per-session total volume, computed as-of).
  vwap         : session cumulative VWAP, as-of each bar.
  ema9         : 9-period EMA of close (intraday, reset per session).
  cum_ret      : cumulative return from session open.
  session_ret  : return over the trailing N bars (for squeeze detection).
"""

import numpy as np
import pandas as pd


def add_session_id(df: pd.DataFrame) -> pd.DataFrame:
    """Tag each bar with its NYSE session date (for intraday resets)."""
    out = df.copy()
    ny = out["bar_ts_utc"].dt.tz_convert("America/New_York")
    out["session"] = ny.dt.date
    return out


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    df = df.sort_values("bar_ts_utc").reset_index(drop=True)
    df = add_session_id(df)

    # Per-session VWAP and EMA9 (reset each session, no look-ahead).
    vwap = []
    ema9 = []
    for _, grp in df.groupby("session", sort=True):
        grp = grp.sort_values("bar_ts_utc")
        tp = (grp["high"] + grp["low"] + grp["close"]) / 3.0
        cum_pv = (tp * grp["volume"]).cumsum()
        cum_v = grp["volume"].cumsum().replace(0, np.nan)
        vwap.append(cum_pv / cum_v)
        ema9.append(grp["close"].ewm(span=9, adjust=False).mean())
    df["vwap"] = pd.concat(vwap).sort_index()
    df["ema9"] = pd.concat(ema9).sort_index()

    # True overnight gap: the FIRST bar's open vs the PRIOR session's close.
    # All other bars in the session are NaN (the gap is a session-level
    # signal, and the course's pillar references the open vs prior close).
    sessions = sorted(df["session"].unique())
    gap_vals = pd.Series(np.nan, index=df.index)
    prev_close = None
    for sess in sessions:
        grp = df[df["session"] == sess].sort_values("bar_ts_utc")
        first_idx = grp.index[0]
        if prev_close is not None and prev_close > 0:
            gap_vals.loc[first_idx] = (grp.loc[first_idx, "open"] / prev_close) - 1.0
        prev_close = float(grp["close"].iloc[-1])
    df["gap_open"] = gap_vals

    # Relative volume. Two columns:
    #   relvol_asof — cumulative session volume / prior-session ADV, at
    #                 THIS bar. No look-ahead. This is what the screen
    #                 and the backtest must use to decide a signal.
    #   relvol      — full-session volume / prior ADV (EOD diagnostic).
    #                 Using this to pass a morning signal is look-ahead.
    sess_totals = df.groupby("session")["volume"].sum()
    prior_mean = sess_totals.expanding().mean().shift(1)
    relvol_map = {}
    for sess in sess_totals.index:
        pm = prior_mean.loc[sess]
        relvol_map[sess] = sess_totals.loc[sess] / pm if pd.notna(pm) and pm > 0 else np.nan
    df["relvol"] = df["session"].map(relvol_map)
    cum = df.groupby("session")["volume"].cumsum()
    asof = []
    for idx, sess in zip(df.index, df["session"]):
        pm = prior_mean.loc[sess] if sess in prior_mean.index else np.nan
        asof.append(float(cum.loc[idx] / pm) if pd.notna(pm) and pm > 0 else np.nan)
    df["relvol_asof"] = asof

    # Momentum helpers.
    df["cum_ret"] = df.groupby("session")["close"].transform(
        lambda c: c / c.iloc[0] - 1.0
    )
    df["ret_5"] = df.groupby("session")["close"].pct_change(5)
    return df
