"""Point-in-time backtest for Gale ORB5 shadow strategy."""

from pathlib import Path

import pandas as pd

from tempest.config import DATA_DIR
from tempest.features import compute_features
from tempest.gale import HOLD_BARS, STRATEGY_ID, detect_gale_orb, shadow_entry_price, target_for
from tempest.strategy import REL_VOL_TRADE_MAX
from tempest.validation import CostModel, TradeResult, bucket_for, summarize


def load_screen_observations(paths: list[Path] | None = None) -> pd.DataFrame:
    paths = paths or [DATA_DIR / "screen_log.csv", DATA_DIR / "gale_screen_log.csv"]
    frames = []
    for path in paths:
        try:
            df = pd.read_csv(path)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if "captured_at_utc" not in df.columns:
            continue
        if "session_date" not in df.columns:
            df["session_date"] = df.get("date_utc")
        if "tradeable" not in df.columns:
            passes = (
                df["passes"] if "passes" in df.columns
                else pd.Series(True, index=df.index)
            )
            relvol = pd.to_numeric(
                df["relvol"] if "relvol" in df.columns
                else pd.Series(float("nan"), index=df.index),
                errors="coerce",
            )
            df["tradeable"] = passes.astype(str).str.lower().isin(["true", "1"]) & (
                relvol <= REL_VOL_TRADE_MAX
            )
        df["captured_at_utc"] = pd.to_datetime(
            df["captured_at_utc"], utc=True, errors="coerce"
        )
        df = df[df["captured_at_utc"].notna()].copy()
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("captured_at_utc")


def _available_observation(
    observations: pd.DataFrame, symbol: str, session: str, signal_ts,
) -> pd.Series | None:
    if observations is None or observations.empty:
        return None
    available_at = pd.Timestamp(signal_ts) + pd.Timedelta(minutes=1)
    mask = (
        observations["symbol"].astype(str).str.upper().eq(str(symbol).upper())
        & observations["session_date"].astype(str).eq(str(session))
        & observations["tradeable"].astype(str).str.lower().isin(["true", "1"])
        & (observations["captured_at_utc"] <= available_at)
    )
    rows = observations[mask]
    return rows.iloc[0] if not rows.empty else None


def _simulate(signal, grp, signal_idx: int, cost: CostModel) -> TradeResult | None:
    entry_idx = signal_idx + 1
    if entry_idx >= len(grp):
        return None
    entry_bar = grp.iloc[entry_idx]
    limit_price = float(signal.trigger_price)
    if float(entry_bar["low"]) > limit_price:
        return None
    attainable = min(float(entry_bar["open"]), limit_price)
    entry = shadow_entry_price(signal, attainable)
    if entry is None:
        return None
    stop = signal.stop_price
    target = target_for(entry, stop)
    end = min(entry_idx + HOLD_BARS, len(grp))
    exit_price = None
    exit_reason = None
    exit_i = None
    for i in range(entry_idx, end):
        bar = grp.iloc[i]
        if float(bar["low"]) <= stop:
            exit_price, exit_reason, exit_i = stop, "stop", i
            break
        if float(bar["high"]) >= target:
            exit_price, exit_reason, exit_i = target, "target", i
            break
    if exit_price is None:
        if end - entry_idx < HOLD_BARS:
            return None
        exit_i = end - 1
        exit_price = float(grp.iloc[exit_i]["close"])
        exit_reason = "horizon"
    gross = exit_price / entry - 1.0
    risk = entry - stop
    return TradeResult(
        symbol=signal.symbol,
        session=signal.session,
        entry_ts=pd.Timestamp(entry_bar["bar_ts_utc"]).isoformat(),
        entry_price=entry,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_return=gross,
        net_return=cost.net_return(gross),
        r_multiple=(exit_price - entry) / risk if risk > 0 else 0.0,
        held_bars=exit_i - entry_idx,
        bucket={},
    )


def run_gale_backtest(
    warehouse_df: pd.DataFrame,
    symbol: str,
    observations: pd.DataFrame | None = None,
    cost_model: CostModel | None = None,
) -> dict:
    cost = cost_model or CostModel()
    obs = observations if observations is not None else load_screen_observations()
    feat = compute_features(warehouse_df)
    if feat is None or feat.empty:
        return {
            "strategy_id": STRATEGY_ID, "symbol": symbol, "n_signals": 0,
            "n_trades": 0, "summary": {"n": 0}, "bucket_summary": {},
            "trades": [], "rejected_unavailable": 0,
        }
    trades = []
    signal_count = 0
    rejected_unavailable = 0
    for session, raw in feat.groupby("session", sort=True):
        grp = raw.sort_values("bar_ts_utc").reset_index(drop=True)
        for signal in detect_gale_orb(grp, symbol):
            signal_count += 1
            observation = _available_observation(
                obs, symbol, str(session), signal.signal_ts
            )
            if observation is None:
                rejected_unavailable += 1
                continue
            indices = grp.index[grp["bar_ts_utc"] == signal.signal_ts]
            if len(indices) != 1:
                continue
            trade = _simulate(signal, grp, int(indices[0]), cost)
            if trade is None:
                continue
            gap = float(observation.get("gap_pct") or 0) / 100.0
            relvol = float(observation.get("relvol") or 0)
            entry_hour = pd.Timestamp(trade.entry_ts).tz_convert("America/New_York").hour
            trade.bucket = bucket_for(gap, relvol, trade.entry_price, entry_hour)
            trade.bucket["opening_range_width"] = (
                "0-2%" if signal.opening_range_width < 0.02
                else "2-3.5%" if signal.opening_range_width < 0.035
                else "3.5-5%"
            )
            trades.append(trade)
    summary = summarize(trades)
    buckets = {}
    for trade in trades:
        for key, value in trade.bucket.items():
            buckets.setdefault(key, {}).setdefault(str(value), []).append(trade)
    bucket_summary = {
        key: {value: summarize(rows) for value, rows in values.items()}
        for key, values in buckets.items()
    }
    return {
        "strategy_id": STRATEGY_ID,
        "symbol": str(symbol).upper(),
        "n_signals": signal_count,
        "n_trades": len(trades),
        "summary": summary,
        "bucket_summary": bucket_summary,
        "trades": [trade.to_dict() for trade in trades],
        "rejected_unavailable": rejected_unavailable,
    }
