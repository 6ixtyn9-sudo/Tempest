"""yfinance 1-minute pilot adapter.

Limitations (documented, not hidden):
  - yfinance serves ~30 days of 1m bars; requests beyond that are clamped.
  - Rate-limited; retry with backoff on empty/transient failures.
  - Microcap bars can contain halts/gaps/phantom prints; the warehouse keeps
    raw values and downstream sanitisation is a separate concern.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from tempest.config import PILOT_MAX_DAYS
from tempest.sources.base import BarSource

_MAX_ATTEMPTS = 3
_BACKOFF = (2.0, 6.0)


def _clamp_start(start: datetime, end: datetime) -> datetime:
    floor = end - timedelta(days=PILOT_MAX_DAYS)
    return max(start, floor)


class YFinance1mSource(BarSource):
    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError:
            return pd.DataFrame()
        start = _clamp_start(start, end)
        ticker = symbol.upper().replace("-", "-")  # BRK-B stays BRK-B for yahoo
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                df = yf.Ticker(ticker).history(
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval="1m",
                    auto_adjust=False,
                )
                if df is not None and not df.empty:
                    return self._normalize(df, symbol)
            except Exception:
                pass
            if attempt < _MAX_ATTEMPTS:
                import time
                time.sleep(_BACKOFF[attempt - 1])
        return pd.DataFrame()

    def _normalize(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        df = df.reset_index()
        ts_col = "Datetime" if "Datetime" in df.columns else "Date"
        out = pd.DataFrame({
            "symbol": symbol.upper(),
            "bar_ts_utc": pd.to_datetime(df[ts_col], utc=True),
            "open": pd.to_numeric(df["Open"], errors="coerce"),
            "high": pd.to_numeric(df["High"], errors="coerce"),
            "low": pd.to_numeric(df["Low"], errors="coerce"),
            "close": pd.to_numeric(df["Close"], errors="coerce"),
            "volume": pd.to_numeric(df["Volume"], errors="coerce").fillna(0),
        })
        out["source"] = "yfinance_1m"
        out["ingested_at_utc"] = datetime.now(timezone.utc)
        out = out.dropna(subset=["open", "high", "low", "close"])
        return out.sort_values("bar_ts_utc").reset_index(drop=True)
