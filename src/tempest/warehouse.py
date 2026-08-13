"""Parquet warehouse for 1-minute bars. Mirrors Price's warehouse pattern:
partition by symbol, keep-last dedup by bar timestamp, canonical schema.

Schema (canonical columns):
  symbol, bar_ts_utc, open, high, low, close, volume,
  source, ingested_at_utc
"""

from datetime import datetime, timezone

import pandas as pd

from tempest.config import WAREHOUSE_DIR, SYMBOL_PATTERN

CANONICAL_COLUMNS = [
    "symbol", "bar_ts_utc", "open", "high", "low", "close", "volume",
    "source", "ingested_at_utc",
]


def sanitize_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if not SYMBOL_PATTERN.fullmatch(s):
        raise ValueError(f"invalid symbol: {symbol!r}")
    return s


def load_from_warehouse(symbol: str) -> pd.DataFrame:
    """Load all bars for a symbol, sorted by time. Empty frame if absent."""
    safe = sanitize_symbol(symbol)
    partition = WAREHOUSE_DIR / f"symbol={safe}"
    if not partition.exists():
        return pd.DataFrame()
    files = sorted(partition.glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["bar_ts_utc"] = pd.to_datetime(df["bar_ts_utc"], utc=True)
    return df.sort_values("bar_ts_utc").reset_index(drop=True)


def save_to_warehouse(df: pd.DataFrame) -> int:
    """Persist a canonical frame, dedup keep-last by bar_ts_utc. Returns rows."""
    if df is None or df.empty:
        return 0
    for col in CANONICAL_COLUMNS:
        if col not in df.columns:
            if col == "ingested_at_utc":
                df[col] = datetime.now(timezone.utc)
            elif col == "source":
                df[col] = "unknown"
            else:
                df[col] = None
    df = df[CANONICAL_COLUMNS].copy()
    df["bar_ts_utc"] = pd.to_datetime(df["bar_ts_utc"], utc=True)

    total = 0
    for symbol, group in df.groupby("symbol"):
        safe = sanitize_symbol(symbol)
        partition = WAREHOUSE_DIR / f"symbol={safe}"
        partition.mkdir(parents=True, exist_ok=True)

        existing = load_from_warehouse(symbol)
        if not existing.empty:
            combined = pd.concat([existing, group], ignore_index=True)
            combined = combined.sort_values(["bar_ts_utc", "ingested_at_utc"])
            combined = combined.drop_duplicates(subset=["bar_ts_utc"], keep="last")
        else:
            combined = group.sort_values("bar_ts_utc").reset_index(drop=True)

        for old in partition.glob("*.parquet"):
            old.unlink()
        out = combined.drop(columns=["symbol"]).copy()
        out.to_parquet(partition / "data.parquet", index=False)
        total += len(combined)
    return total
