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
from tempest.validation import CostModel, TradeResult, bucket_for, summarize
from tempest.strategy import detect_first_pullback, screen_pillars


def load_float_map(path=None) -> dict:
    """Point-in-time float observations keyed by ``(symbol, date_utc)``."""
    p = path if path is not None else DATA_DIR / "screen_log.csv"
    try:
        df = pd.read_csv(p)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return {}
    required = {"date_utc", "symbol", "float_shares"}
    if df.empty or not required.issubset(df.columns):
        return {}
    out = {}
    for _, row in df.iterrows():
        try:
            day = pd.Timestamp(row["date_utc"]).date().isoformat()
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
            "screen_stats": {"sessions": 0, "passed": 0, "reject_reasons": {}},
        }

    fmap = float_map if float_map is not None else load_float_map()
    trades: list[TradeResult] = []
    screen_stats = {"sessions": 0, "passed": 0, "reject_reasons": {}}

    for session, grp in df.groupby("session", sort=True):
        grp = grp.sort_values("bar_ts_utc").reset_index(drop=True)
        if len(grp) < 2:
            continue
        screen_stats["sessions"] += 1
        # Gap is a session-level prior (open vs prior close). Relvol and
        # volume must be as-of the SIGNAL bar — using the full session
        # total here would let an afternoon volume spike bless a morning
        # entry that had not yet printed 5x.
        gap = float(grp["gap_open"].iloc[0]) if "gap_open" in grp else np.nan
        session_passed = False
        for sig in detect_first_pullback(grp, symbol):
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
            entry_hour = _ny_hour(sig.entry_ts)
            bucket = bucket_for(gap, asof_relvol, sig.entry_price, entry_hour)
            trades.append(
                _simulate(sig, grp, cost_model, hold_bars, bucket)
            )
        if session_passed:
            screen_stats["passed"] += 1

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
        "n_signals": len(results),
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
    """Simulate one signal forward: stop / target / horizon. Look-ahead-free:
    only bars after the entry bar are used."""
    entry_idx = grp.index[grp["bar_ts_utc"] == sig.entry_ts]
    if len(entry_idx) == 0:
        return None
    start = entry_idx[0] + 1
    if start >= len(grp):
        return None
    entry = sig.entry_price
    stop = sig.stop_price
    target = sig.target_price

    for i in range(start, min(start + hold_bars, len(grp))):
        low, high = grp["low"].iloc[i], grp["high"].iloc[i]
        if low <= stop:
            return _mk(sig, entry, stop, "stop", cost_model, i - start, bucket)
        if high >= target:
            return _mk(sig, entry, target, "target", cost_model, i - start, bucket)
    # Horizon exit at the last bar's close.
    i = min(start + hold_bars - 1, len(grp) - 1)
    return _mk(sig, entry, float(grp["close"].iloc[i]), "horizon", cost_model, i - start, bucket)


def _mk(sig, entry, exit_px, reason, cost_model, held_bars, bucket) -> TradeResult:
    gross = (exit_px / entry) - 1.0
    net = cost_model.net_return(gross)
    r = (exit_px - entry) / (entry - sig.stop_price) if entry != sig.stop_price else 0.0
    return TradeResult(
        symbol=sig.symbol, session=sig.session, entry_ts=str(sig.entry_ts),
        entry_price=entry, exit_price=float(exit_px), exit_reason=reason,
        gross_return=gross, net_return=net, r_multiple=float(r),
        held_bars=held_bars, bucket=bucket,
    )
