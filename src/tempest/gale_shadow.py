"""Shadow-only live evidence engine for Gale ORB5.

This module has no broker imports and no order path. It timestamps the screened
universe, records unique hypothetical entries, and settles them from later RTH
bars using conservative stop-first same-bar ordering.
"""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from tempest.config import DATA_DIR
from tempest.features import compute_features
from tempest.gale import (
    HOLD_BARS, STRATEGY_ID, detect_gale_orb, shadow_entry_price, target_for,
)
from tempest.validation import CostModel

SCREEN_PATH = DATA_DIR / "gale_screen_log.csv"
SIGNALS_PATH = DATA_DIR / "gale_shadow_signals.csv"
STATUS_PATH = DATA_DIR / "gale_status.csv"

SCREEN_COLUMNS = [
    "captured_at_utc", "session_date", "strategy_id", "symbol", "close",
    "gap_pct", "relvol", "float_shares", "volume", "tradeable",
]
SIGNAL_COLUMNS = [
    "strategy_id", "signal_id", "captured_at_utc", "first_seen_at_utc",
    "session", "symbol", "signal_ts", "status", "entry_ts", "entry_price",
    "stop_price", "target_price", "opening_range_high", "opening_range_low",
    "opening_range_width", "breakout_volume_ratio", "vwap", "exit_ts",
    "exit_price", "exit_reason", "gross_return", "net_return", "r_multiple",
]


def _read(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    try:
        df = pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df[columns]


def append_status(stage: str, detail: str, path: Path | None = None) -> None:
    target = path or STATUS_PATH
    row = pd.DataFrame([{
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "detail": detail,
    }])
    old = _read(target, ["timestamp_utc", "stage", "detail"])
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.concat([old, row], ignore_index=True).tail(500).to_csv(target, index=False)


def append_screen_rows(
    rows: list[dict], now: datetime, path: Path | None = None,
) -> pd.DataFrame:
    target = path or SCREEN_PATH
    now = now.astimezone(timezone.utc)
    session = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    additions = []
    for row in rows:
        additions.append({
            "captured_at_utc": now.isoformat(),
            "session_date": session,
            "strategy_id": STRATEGY_ID,
            "symbol": str(row.get("symbol") or "").upper(),
            "close": row.get("close"),
            "gap_pct": row.get("gap_pct"),
            "relvol": row.get("relvol"),
            "float_shares": row.get("float_shares"),
            "volume": row.get("volume"),
            "tradeable": bool(row.get("tradeable", True)),
        })
    old = _read(target, SCREEN_COLUMNS)
    added = pd.DataFrame(additions, columns=SCREEN_COLUMNS)
    combined = added if old.empty else (
        old if added.empty else pd.concat([old, added], ignore_index=True)
    )
    if not combined.empty:
        combined = combined.drop_duplicates(
            subset=["captured_at_utc", "symbol"], keep="last"
        ).sort_values(["captured_at_utc", "symbol"])
    target.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(target, index=False)
    return combined


def first_seen_before(
    screen: pd.DataFrame, symbol: str, session: str, available_at,
) -> pd.Timestamp | None:
    if screen is None or screen.empty:
        return None
    captured = pd.to_datetime(screen["captured_at_utc"], utc=True, errors="coerce")
    mask = (
        screen["symbol"].astype(str).str.upper().eq(str(symbol).upper())
        & screen["session_date"].astype(str).eq(str(session))
        & screen["tradeable"].astype(str).str.lower().isin(["true", "1"])
        & captured.notna()
        & (captured <= pd.Timestamp(available_at))
    )
    eligible = captured[mask]
    return eligible.min() if not eligible.empty else None


def settle_open_signals(
    signals: pd.DataFrame,
    bars_by_symbol: dict[str, pd.DataFrame],
    cost_model: CostModel | None = None,
) -> pd.DataFrame:
    if signals is None or signals.empty:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)
    out = signals.copy()
    for column in ("status", "exit_ts", "exit_reason"):
        out[column] = out[column].astype(object)
    cost = cost_model or CostModel()
    for idx, row in out[out["status"].astype(str) == "open"].iterrows():
        symbol = str(row["symbol"]).upper()
        bars = bars_by_symbol.get(symbol)
        if bars is None or bars.empty:
            continue
        feat = compute_features(bars)
        if feat is None or feat.empty:
            continue
        entry_ts = pd.Timestamp(row["entry_ts"])
        entry_ts = entry_ts.tz_localize("UTC") if entry_ts.tzinfo is None else entry_ts.tz_convert("UTC")
        later = feat[feat["bar_ts_utc"] > entry_ts].sort_values("bar_ts_utc").head(HOLD_BARS)
        if later.empty:
            continue
        entry = float(row["entry_price"])
        stop = float(row["stop_price"])
        target = float(row["target_price"])
        exit_row = None
        exit_price = None
        exit_reason = None
        for _, bar in later.iterrows():
            if float(bar["low"]) <= stop:
                exit_row, exit_price, exit_reason = bar, stop, "stop"
                break
            if float(bar["high"]) >= target:
                exit_row, exit_price, exit_reason = bar, target, "target"
                break
        if exit_row is None and len(later) >= HOLD_BARS:
            exit_row = later.iloc[-1]
            exit_price = float(exit_row["close"])
            exit_reason = "horizon"
        if exit_row is None:
            continue
        gross = exit_price / entry - 1.0
        risk = entry - stop
        out.loc[idx, [
            "status", "exit_ts", "exit_price", "exit_reason",
            "gross_return", "net_return", "r_multiple",
        ]] = [
            "closed", pd.Timestamp(exit_row["bar_ts_utc"]).isoformat(),
            round(exit_price, 4), exit_reason, round(gross, 6),
            round(cost.net_return(gross), 6),
            round((exit_price - entry) / risk, 3) if risk > 0 else None,
        ]
    return out[SIGNAL_COLUMNS]


def discover_shadow_signals(
    screen: pd.DataFrame,
    existing: pd.DataFrame,
    bars_by_symbol: dict[str, pd.DataFrame],
    now: datetime,
    max_signal_age_bars: int = 5,
    max_bar_age_minutes: float = 10.0,
) -> tuple[pd.DataFrame, int]:
    now = now.astimezone(timezone.utc)
    rows = existing.copy() if existing is not None else pd.DataFrame(columns=SIGNAL_COLUMNS)
    for col in SIGNAL_COLUMNS:
        if col not in rows.columns:
            rows[col] = None
    seen = set(rows["signal_id"].dropna().astype(str)) if not rows.empty else set()
    additions = []
    for symbol, bars in bars_by_symbol.items():
        feat = compute_features(bars)
        if feat is None or feat.empty:
            continue
        latest_ts = pd.Timestamp(feat["bar_ts_utc"].iloc[-1]).tz_convert("UTC")
        lag = (pd.Timestamp(now) - latest_ts).total_seconds() / 60.0
        if lag < 0 or lag > max_bar_age_minutes:
            continue
        signals = detect_gale_orb(feat, symbol)
        if not signals:
            continue
        sig = signals[-1]
        session_rows = feat[feat["session"] == sig.session].reset_index(drop=True)
        positions = {ts: i for i, ts in enumerate(session_rows["bar_ts_utc"])}
        signal_i = positions.get(sig.signal_ts)
        if signal_i is None or len(session_rows) - 1 - signal_i > max_signal_age_bars:
            continue
        first_seen = first_seen_before(
            screen, symbol, str(sig.session), sig.signal_ts + pd.Timedelta(minutes=1)
        )
        if first_seen is None or sig.signal_id in seen:
            continue
        observed_price = float(session_rows["close"].iloc[-1])
        entry = shadow_entry_price(sig, observed_price)
        if entry is None:
            continue
        target = target_for(entry, sig.stop_price)
        payload = sig.to_dict()
        payload.update({
            "captured_at_utc": now.isoformat(),
            "first_seen_at_utc": first_seen.isoformat(),
            "status": "open",
            "entry_ts": latest_ts.isoformat(),
            "entry_price": round(entry, 4),
            "target_price": round(target, 4),
        })
        additions.append(payload)
        seen.add(sig.signal_id)
    if additions:
        added = pd.DataFrame(additions, columns=SIGNAL_COLUMNS)
        rows = added if rows.empty else pd.concat([rows, added], ignore_index=True)
    return rows[SIGNAL_COLUMNS], len(additions)


def save_signals(signals: pd.DataFrame, path: Path | None = None) -> None:
    target = path or SIGNALS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    signals[SIGNAL_COLUMNS].to_csv(target, index=False)


def load_signals(path: Path | None = None) -> pd.DataFrame:
    return _read(path or SIGNALS_PATH, SIGNAL_COLUMNS)
