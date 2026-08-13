"""Event-driven backtest of the momentum methodology.

For every session in the warehouse that passes the five pillars, detect
first-pullback signals and simulate the trade forward bar by bar: stop,
target (2R), or horizon close — whichever comes first. Costs are netted via
the CostModel. Output is a list of TradeResults plus per-bucket breakdowns
(the discovery surface beyond the course's rules).
"""

import numpy as np
import pandas as pd

from tempest.features import compute_features
from tempest.validation import CostModel, TradeResult, bucket_for, summarize
from tempest.strategy import detect_first_pullback, screen_pillars


def _symbol_meta(symbol: str) -> dict:
    """Best-effort metadata (float, etc.). v1: float unknown -> None (the
    float pillar is skipped rather than assumed). Later: from yfinance info
    or the Finviz scraper."""
    return {"float_shares": None}


def run_backtest(
    warehouse_df: pd.DataFrame,
    symbol: str,
    cost_model: CostModel | None = None,
    hold_bars: int = 15,
    relax: bool = False,
) -> dict:
    """Run the full backtest for one symbol's 1m frame. Returns the report
    dict: signals, trades, summary, per-bucket summaries."""
    cost_model = cost_model or CostModel()
    df = compute_features(warehouse_df)
    if df is None or df.empty:
        return {
            "symbol": symbol, "n_signals": 0, "n_trades": 0,
            "summary": {"n": 0}, "bucket_summary": {}, "trades": [],
        }

    meta = _symbol_meta(symbol)
    trades: list[TradeResult] = []

    for _, grp in df.groupby("session", sort=True):
        grp = grp.sort_values("bar_ts_utc").reset_index(drop=True)
        if len(grp) < 2:
            continue
        open_px = float(grp["open"].iloc[0])
        gap = float(grp["gap_open"].iloc[0]) if "gap_open" in grp else np.nan
        relvol = float(grp["relvol"].iloc[0]) if "relvol" in grp else np.nan
        total_vol = float(grp["volume"].sum())
        pillars = screen_pillars(
            symbol, relvol=relvol, total_volume=total_vol,
            gap_open=gap, price=open_px, float_shares=meta["float_shares"],
            relax=relax,
        )
        if not pillars.passes:
            continue

        for sig in detect_first_pullback(grp, symbol):
            entry_hour = _ny_hour(sig.entry_ts)
            bucket = bucket_for(gap, relvol, sig.entry_price, entry_hour)
            trades.append(
                _simulate(sig, grp, cost_model, hold_bars, bucket)
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
        "n_signals": len(results),
        "n_trades": len(results),
        "summary": summary,
        "bucket_summary": bucket_summary,
        "trades": [t.to_dict() for t in results],
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
