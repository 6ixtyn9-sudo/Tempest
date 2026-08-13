"""Shared synthetic 1-minute frame builders for tests."""

import numpy as np
import pandas as pd

from tempest.features import add_session_id


def make_session_1m(
    opens, highs, lows, closes, volumes,
    start="2026-08-03 13:30:00+00:00", symbol="YXT",
) -> pd.DataFrame:
    """Build a 1-minute session frame from per-bar arrays (UTC timestamps)."""
    n = len(closes)
    ts = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame({
        "symbol": symbol,
        "bar_ts_utc": ts,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes, "source": "test", "ingested_at_utc": pd.Timestamp.utcnow(),
    })
    return add_session_id(df)


def squeeze_pullback_break_frame(symbol="YXT"):
    """A textbook first-pullback session:
       squeeze 10.00 -> 10.60 (3 green candles), pullback to ~10.52
       (holds VWAP and 9-EMA, light volume), then a break of the prior
       high to 10.80.
    """
    opens  = [10.00, 10.20, 10.40, 10.60, 10.58, 10.55, 10.45, 10.60, 10.70, 10.80]
    highs  = [10.20, 10.40, 10.60, 10.75, 10.60, 10.56, 10.55, 10.70, 10.85, 10.95]
    lows   = [ 9.98, 10.18, 10.38, 10.55, 10.52, 10.50, 10.40, 10.55, 10.65, 10.75]
    closes = [10.20, 10.40, 10.60, 10.60, 10.54, 10.52, 10.50, 10.70, 10.80, 10.90]
    vols   = [200_000, 250_000, 300_000, 400_000, 80_000, 60_000, 90_000,
              350_000, 380_000, 420_000]
    return make_session_1m(opens, highs, lows, closes, vols, symbol=symbol)
