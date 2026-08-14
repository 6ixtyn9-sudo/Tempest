"""Event-driven backtest of the momentum methodology.

For every session in the warehouse that passes the five pillars, detect
first-pullback signals and simulate the trade forward bar by bar: stop,
target (2R), or horizon close — whichever comes first. Costs are netted via
the CostModel. Output is a list of TradeResults plus per-bucket breakdowns
(the discovery surface beyond the course's rules).
"""

import numpy as np
import pandas as pd

from tempest.config import DATA_DIR
from tempest.features import compute_features
from tempest.strategy import detect_first_pullback, screen_pillars
from tempest.validation import CostModel, TradeResult, bucket_for, summarize


def load_float_map(path=None) -> dict:
    """Point-in-time float observations keyed by ``(symbol, date_utc)``."""
    p = path if path is not None else DATA_DIR / "screen_log.csv"
    try:
        df = pd.read_csv(p)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return {}
    required = {
        "captured_at_utc", "session_date", "symbol", "float_shares",
    }
    if df.empty or not required.issubset(df.columns):
        return {}
    captured = pd.to_datetime(df["captured_at_utc"], utc=True, errors="coerce")
    quality = df.get("snapshot_quality", "timestamped")
    valid = captured.notna() & pd.Series(quality, index=df.index).astype(str).eq("timestamped")
    out = {}
    for _, row in df[valid].iterrows():
        try:
            day = pd.Timestamp(row["session_date"]).date().isoformat()
            out[(str(row["symbol"]).upper(), day)] = float(row["float_shares"])
        except (TypeError, ValueError):
            continue
    return out


def _float_asof(symbol: str, session, float_map: dict) -> float | None:
    """Latest float captured no later than the backtested session."""
    sym = str(symbol).upper()
    # Compatibility for explicit test/research overrides: {"ABC": 1_000_000}.
    direct = float_map.get(sym)
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            return None
    try:
        day = pd.Timestamp(session).date().isoformat()
    except (TypeError, ValueError):
        return None
    observations = []
    for key, value in float_map.items():
        if not isinstance(key, tuple) or len(key) != 2 or str(key[0]).upper() != sym:
            continue
        try:
            observed_day = pd.Timestamp(key[1]).date().isoformat()
            if observed_day <= day:
                observations.append((observed_day, float(value)))
        except (TypeError, ValueError):
            continue
    return max(observations, default=(None, None), key=lambda item: item[0])[1]


def _no_signal_reason(grp, symbol: str) -> str:
    """Classify why a session produced no first-pullback signal.

    Mirrors the stages of strategy._detect_session so the backtest report
    explains its own emptiness instead of silently dropping the session.
    Returns the FIRST binding constraint, which is the actionable one.
    """
    from tempest.strategy import (
        MAX_RETRACE,
        MIN_RISK_FRACTION,
        SQUEEZE_BARS,
    )

    if len(grp) < SQUEEZE_BARS + 2:
        return "session too short"

    o = grp["open"].values
    close = grp["close"].values
    high = grp["high"].values
    low = grp["low"].values
    vol = grp["volume"].values
    vwap = grp["vwap"].values if "vwap" in grp else None
    ema9 = grp["ema9"].values if "ema9" in grp else None
    if vwap is None or ema9 is None:
        return "features missing (vwap/ema9)"

    n = len(grp)
    runs = []
    i = 0
    while i < n - 1:
        if close[i] <= o[i]:
            i += 1
            continue
        run = 1
        while i + run < n and close[i + run] > o[i + run] and run < 12:
            run += 1
        runs.append(run)
        i += run

    if not runs:
        return "no green candles"
    if max(runs) < SQUEEZE_BARS:
        return f"no squeeze: longest green run {max(runs)} < {SQUEEZE_BARS}"

    # A squeeze existed; find which downstream stage killed every attempt.
    stages = {"pullback broke vwap": 0, "pullback broke ema9": 0,
              "pullback volume heavy": 0, "no pullback candle": 0,
              "retrace > max": 0, "risk below floor": 0, "no breakout": 0}
    i = 0
    while i < n - 1:
        if close[i] <= o[i]:
            i += 1
            continue
        run = 1
        while i + run < n and close[i + run] > o[i + run] and run < 12:
            run += 1
        if run < SQUEEZE_BARS:
            i += run
            continue
        squeeze_high = max(high[i:i + run])
        base = min(low[i:i + run])
        avg_vol = float(np.mean(vol[i:i + run]))
        j = i + run
        pb_start = j
        killed = None
        while j < n and close[j] <= o[j]:
            if low[j] < vwap[j]:
                killed = "pullback broke vwap"
                break
            if close[j] < ema9[j]:
                killed = "pullback broke ema9"
                break
            if close[j] < o[j] and vol[j] > avg_vol:
                killed = "pullback volume heavy"
                break
            j += 1
        if killed:
            stages[killed] += 1
            i = max(j, i + 1)
            continue
        if j == pb_start:
            stages["no pullback candle"] += 1
            i = max(j, i + 1)
            continue
        pb_low = min(low[pb_start:j])
        retrace = (squeeze_high - pb_low) / (squeeze_high - base + 1e-9)
        if retrace > MAX_RETRACE:
            stages["retrace > max"] += 1
            i = j
            continue
        k = j
        broke = False
        while k < n:
            if high[k] > high[k - 1] and high[k] > squeeze_high * 0.98:
                broke = True
                entry = max(float(high[k - 1]), float(o[k]))
                if entry - pb_low < MIN_RISK_FRACTION * entry:
                    stages["risk below floor"] += 1
                break
            k += 1
        if not broke:
            stages["no breakout"] += 1
        i = j

    hit = {k: v for k, v in stages.items() if v}
    if not hit:
        return "squeeze found, unclassified"
    return max(hit.items(), key=lambda kv: kv[1])[0]


def run_backtest(
    warehouse_df: pd.DataFrame,
    symbol: str,
    cost_model: CostModel | None = None,
    hold_bars: int = 15,
    relax: bool = False,
    float_map: dict | None = None,
) -> dict:
    """Run the full backtest for one symbol's 1m frame. Returns the report
    dict: signals, trades, summary, per-bucket summaries."""
    cost_model = cost_model or CostModel()
    df = compute_features(warehouse_df)
    if df is None or df.empty:
        return {
            "symbol": symbol, "n_signals": 0, "n_trades": 0,
            "summary": {"n": 0}, "bucket_summary": {}, "trades": [],
            "screen_stats": {
                "sessions": 0, "passed": 0, "reject_reasons": {},
                "no_signal_sessions": 0, "no_signal_reasons": {},
            },
        }

    fmap = float_map if float_map is not None else load_float_map()
    trades: list[TradeResult] = []
    eligible_signals = 0
    screen_stats = {
        "sessions": 0, "passed": 0, "reject_reasons": {},
        "no_signal_sessions": 0, "no_signal_reasons": {},
    }

    for session, grp in df.groupby("session", sort=True):
        grp = grp.sort_values("bar_ts_utc").reset_index(drop=True)
        if len(grp) < 2:
            continue
        screen_stats["sessions"] += 1
        session_signals = 0
        # Gap is a session-level prior (open vs prior close). Relvol and
        # volume must be as-of the SIGNAL bar — using the full session
        # total here would let an afternoon volume spike bless a morning
        # entry that had not yet printed 5x.
        gap = float(grp["gap_open"].iloc[0]) if "gap_open" in grp else np.nan
        session_passed = False
        for sig in detect_first_pullback(grp, symbol):
            session_signals += 1
            row = grp.loc[grp["bar_ts_utc"] == sig.entry_ts]
            if row.empty:
                continue
            asof_relvol = float(row["relvol_asof"].iloc[0]) if "relvol_asof" in row else np.nan
            asof_vol = float(grp.loc[grp["bar_ts_utc"] <= sig.entry_ts, "volume"].sum())
            pillars = screen_pillars(
                symbol, relvol=asof_relvol, total_volume=asof_vol,
                gap_open=gap, price=float(sig.entry_price),
                float_shares=_float_asof(symbol, session, fmap),
                relax=relax,
            )
            if not pillars.passes:
                for r in pillars.reasons:
                    key = r.split(":")[0].strip() if ":" in r else r.strip()
                    screen_stats["reject_reasons"][key] = screen_stats["reject_reasons"].get(key, 0) + 1
                continue
            session_passed = True
            eligible_signals += 1
            entry_hour = _ny_hour(sig.entry_ts)
            bucket = bucket_for(gap, asof_relvol, sig.entry_price, entry_hour)
            trades.append(
                _simulate(sig, grp, cost_model, hold_bars, bucket)
            )
        if session_passed:
            screen_stats["passed"] += 1
        if session_signals == 0:
            # Previously invisible: reject_reasons only recorded a reason
            # inside the signal loop, so a session where the PATTERN never
            # fired contributed nothing and looked identical to a session
            # rejected on pillars. 116 of 140 sessions were dark this way.
            # Attribute them so the funnel adds up:
            #   sessions == passed + sum(session_outcomes.values())
            screen_stats["no_signal_sessions"] += 1
            reason = _no_signal_reason(grp, symbol)
            screen_stats["no_signal_reasons"][reason] = (
                screen_stats["no_signal_reasons"].get(reason, 0) + 1
            )

    results = [t for t in trades if t is not None]
    summary = summarize(results)
    buckets = {}
    for r in results:
        for key, val in r.bucket.items():
            buckets.setdefault(key, {}).setdefault(str(val), []).append(r)
    bucket_summary = {
        k: {kk: summarize(v) for kk, v in v.items()}
        for k, v in buckets.items()
    }
    return {
        "symbol": symbol,
        "n_signals": eligible_signals,
        "n_trades": len(results),
        "summary": summary,
        "bucket_summary": bucket_summary,
        "trades": [t.to_dict() for t in results],
        "screen_stats": screen_stats,
    }


def _ny_hour(ts) -> int:
    """Hour of the entry timestamp in America/New_York."""
    try:
        return ts.tz_convert("America/New_York").hour
    except Exception:
        return 0


def _simulate(sig, grp, cost_model: CostModel, hold_bars: int, bucket: dict) -> TradeResult | None:
    """Simulate the earliest fill attainable after the signal candle closes."""
    signal_indices = grp.index[grp["bar_ts_utc"] == sig.entry_ts]
    if len(signal_indices) == 0:
        return None
    signal_idx = int(signal_indices[0])
    start = signal_idx + 1
    if start >= len(grp):
        return None

    # Paper execution submits a buy limit only after the crossing candle is
    # complete. The backtest therefore cannot fill inside that signal candle.
    limit_price = float(grp["close"].iloc[signal_idx])
    trigger_price = float(sig.entry_price)
    if (limit_price - trigger_price) / max(trigger_price, 1e-9) > 0.01:
        return None
    next_bar = grp.iloc[start]
    if float(next_bar["low"]) > limit_price:
        return None  # the post-signal limit was never reachable
    entry = min(float(next_bar["open"]), limit_price)
    stop = float(sig.stop_price)
    risk = entry - stop
    if risk <= 0:
        return None
    target = entry + 2.0 * risk

    for i in range(start, min(start + hold_bars, len(grp))):
        low, high = float(grp["low"].iloc[i]), float(grp["high"].iloc[i])
        # Same-bar path is unknown; stop-first is the conservative convention.
        if low <= stop:
            return _mk(
                sig, entry, stop, "stop", cost_model, i - start, bucket,
                entry_ts=next_bar["bar_ts_utc"],
            )
        if high >= target:
            return _mk(
                sig, entry, target, "target", cost_model, i - start, bucket,
                entry_ts=next_bar["bar_ts_utc"],
            )
    i = min(start + hold_bars - 1, len(grp) - 1)
    return _mk(
        sig, entry, float(grp["close"].iloc[i]), "horizon",
        cost_model, i - start, bucket, entry_ts=next_bar["bar_ts_utc"],
    )


def _mk(
    sig, entry, exit_px, reason, cost_model, held_bars, bucket, entry_ts,
) -> TradeResult:
    gross = (exit_px / entry) - 1.0
    net = cost_model.net_return(gross)
    r = (exit_px - entry) / (entry - sig.stop_price) if entry != sig.stop_price else 0.0
    return TradeResult(
        symbol=sig.symbol, session=sig.session, entry_ts=str(entry_ts),
        entry_price=entry, exit_price=float(exit_px), exit_reason=reason,
        gross_return=gross, net_return=net, r_multiple=float(r),
        held_bars=held_bars, bucket=bucket,
    )
