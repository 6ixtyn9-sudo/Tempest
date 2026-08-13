"""yfinance 1-minute pilot adapter.

Yahoo serves 1m bars in ~7-day windows per request (server-side cap,
"Only 8 days worth of 1m granularity data are allowed per request").
The adapter chunks the requested range into <=7-day windows and stitches
the results. Rate-limited; retries with backoff on transient failures.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from tempest.config import PILOT_MAX_DAYS
from tempest.sources.base import BarSource

_MAX_ATTEMPTS = 3
_BACKOFF = (2.0, 6.0)
_CHUNK_DAYS = 7


def _clamp_start(start: datetime, end: datetime) -> datetime:
    floor = end - timedelta(days=PILOT_MAX_DAYS)
    return max(start, floor)


def _chunks(start: datetime, end: datetime):
    """Yield (chunk_start, chunk_end) windows of <= _CHUNK_DAYS."""
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=_CHUNK_DAYS), end)
        yield cur, nxt
        cur = nxt


class YFinance1mSource(BarSource):
    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError:
            return pd.DataFrame()
        start = _clamp_start(start, end)
        ticker = symbol.upper()
        frames = []
        for cstart, cend in _chunks(start, end):
            df = self._fetch_chunk(yf, ticker, cstart, cend)
            if df is not None and not df.empty:
                frames.append(self._normalize(df, symbol))
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset=["bar_ts_utc"], keep="last")
        return out.sort_values("bar_ts_utc").reset_index(drop=True)

    def _fetch_chunk(self, yf, ticker: str, start: datetime, end: datetime):
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                df = yf.Ticker(ticker).history(
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval="1m",
                    auto_adjust=False,
                )
                if df is not None and not df.empty:
                    return df
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
