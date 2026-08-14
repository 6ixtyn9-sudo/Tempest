"""Paper risk rails for the momentum trader.

Fail-open on missing data; fail-closed on anything that looks like a
real-money or runaway path (halt flag, paper-env guard lives in broker.py).
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from tempest.config import DATA_DIR

HALT_FLAG_PATH = DATA_DIR / "HALT_TRADING.flag"
COOLDOWN_PATH = DATA_DIR / "cooldown.json"
JOURNAL_PATH = DATA_DIR / "trade_journal.csv"

_JOURNAL_COLUMNS = [
    "timestamp_utc", "strategy_id", "symbol", "action", "side", "qty", "price",
    "order_id", "status", "setup", "session", "signal_ts", "signal_age_bars",
    "entry_price", "stop_price", "target_price", "exit_price", "pnl", "reason",
]


@dataclass
class RiskLimits:
    max_open_positions: int = 3
    max_notional_per_position: float = 1000.0
    max_risk_per_position: float = 50.0
    max_daily_realized_loss: float = 200.0
    per_symbol_cooldown_seconds: int = 3600
    horizon_bars: int = 15
    close_before_market_close_minutes: int = 30


def is_halted() -> bool:
    return HALT_FLAG_PATH.exists()


def load_journal() -> pd.DataFrame:
    if not JOURNAL_PATH.exists():
        return pd.DataFrame(columns=_JOURNAL_COLUMNS)
    try:
        return pd.read_csv(JOURNAL_PATH)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=_JOURNAL_COLUMNS)


def append_journal(row: dict) -> None:
    df = load_journal()
    clean = {c: row.get(c) for c in _JOURNAL_COLUMNS}
    add = pd.DataFrame([clean], columns=_JOURNAL_COLUMNS)
    if df.empty:
        df = add
    else:
        df = pd.concat([df, add], ignore_index=True)
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(JOURNAL_PATH, index=False)


def open_journal_entries(journal: pd.DataFrame | None = None) -> dict:
    """Last confirmed filled entry per symbol with no later exit."""
    df = load_journal() if journal is None else journal
    if df is None or df.empty or "action" not in df.columns:
        return {}
    open_rows: dict = {}
    ordered = df.sort_values("timestamp_utc") if "timestamp_utc" in df.columns else df
    for _, row in ordered.iterrows():
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        action = str(row.get("action", ""))
        status = str(row.get("status", "") or "").lower()
        if action == "entry" and status in ("filled", "partially_filled"):
            open_rows[sym] = row
        elif action in ("exit", "stop_filled", "tp_filled", "broker_closed"):
            open_rows.pop(sym, None)
    return open_rows


def pending_journal_orders(journal: pd.DataFrame | None = None) -> dict:
    """Last unresolved submitted parent order per symbol."""
    df = load_journal() if journal is None else journal
    if df is None or df.empty or "action" not in df.columns:
        return {}
    pending: dict = {}
    ordered = df.sort_values("timestamp_utc") if "timestamp_utc" in df.columns else df
    terminal = {
        "entry", "entry_cancelled", "entry_expired", "entry_rejected",
    }
    for _, row in ordered.iterrows():
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        action = str(row.get("action", ""))
        status = str(row.get("status", "")).lower()
        if (
            action == "order_submitted" and status not in ("dry_run", "rejected")
        ) or (action == "entry" and status == "submitted"):
            pending[sym] = row
        elif action in terminal:
            pending.pop(sym, None)
    return pending


def pending_exit_orders(journal: pd.DataFrame | None = None) -> dict:
    """Last unresolved broker close request per symbol."""
    df = load_journal() if journal is None else journal
    if df is None or df.empty or "action" not in df.columns:
        return {}
    pending: dict = {}
    ordered = df.sort_values("timestamp_utc") if "timestamp_utc" in df.columns else df
    terminal = {
        "exit", "stop_filled", "tp_filled", "broker_closed",
        "exit_cancelled", "exit_expired", "exit_rejected",
    }
    for _, row in ordered.iterrows():
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        action = str(row.get("action", ""))
        if action == "exit_submitted":
            pending[sym] = row
        elif action in terminal:
            pending.pop(sym, None)
    return pending


def today_realized_pnl() -> float:
    df = load_journal()
    if df.empty or "action" not in df.columns:
        return 0.0
    exits = df[df["action"].isin([
        "exit", "stop_filled", "tp_filled", "broker_closed",
    ])]
    today = datetime.now(timezone.utc).date().isoformat()
    mask = exits["timestamp_utc"].astype(str).str.startswith(today)
    return float(pd.to_numeric(exits[mask]["pnl"], errors="coerce").fillna(0).sum())


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cooldown_remaining(symbol: str, cooldown_seconds: int = 3600) -> float:
    """Seconds still remaining before `symbol` may re-enter. The cooldown is
    measured from the LAST EXIT/ENTRY stamp; elapsed time is subtracted from
    the configured window."""
    if not COOLDOWN_PATH.exists():
        return 0.0
    try:
        state = json.loads(COOLDOWN_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return 0.0
    last = state.get(str(symbol).upper())
    if not last:
        return 0.0
    try:
        last_dt = _utc(datetime.fromisoformat(str(last).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return 0.0
    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
    return max(0.0, float(cooldown_seconds) - elapsed)


def record_cooldown(symbol: str) -> None:
    state = {}
    if COOLDOWN_PATH.exists():
        try:
            state = json.loads(COOLDOWN_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
    state[str(symbol).upper()] = datetime.now(timezone.utc).isoformat()
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(json.dumps(state, indent=2))


def check_entry_ok(
    symbol: str,
    qty: int,
    price: float,
    limits: RiskLimits,
    open_positions: pd.DataFrame,
) -> tuple[bool, list[str]]:
    """Run the entry gate. Returns (allowed, reasons)."""
    reasons: list[str] = []
    if is_halted():
        reasons.append("halt flag set")
    if not open_positions.empty and symbol.upper() in set(
        open_positions["symbol"].astype(str).str.upper()
    ):
        reasons.append("already open")
    else:
        n_open = len(open_positions) if not open_positions.empty else 0
        if n_open >= limits.max_open_positions:
            reasons.append(f"max open positions ({limits.max_open_positions})")
    notional = qty * price
    if notional > limits.max_notional_per_position:
        reasons.append(f"notional ${notional:.2f} > ${limits.max_notional_per_position:.2f}")
    loss = today_realized_pnl()
    if -loss >= limits.max_daily_realized_loss:
        reasons.append(f"daily loss cap (${-loss:.2f} >= ${limits.max_daily_realized_loss:.2f})")
    cool = cooldown_remaining(symbol, limits.per_symbol_cooldown_seconds)
    if cool > 0:
        reasons.append(f"cooldown {cool:.0f}s")
    return (len(reasons) == 0, reasons)
