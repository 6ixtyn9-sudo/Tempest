"""Alpaca market-data + trading adapter — PAPER ONLY.

Data: full 1-minute history via StockHistoricalDataClient (free tier).
Trading: TradingClient constructed with paper=True ALWAYS. The paper keys
only work against the paper endpoint; the code additionally refuses to
build a trading client unless TEMPEST_PAPER=1 is set — belt and braces.
"""

import os
from datetime import datetime, timezone

import pandas as pd

from tempest.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
from tempest.sources.base import BarSource


def require_paper_env() -> None:
    """Fail closed unless TEMPEST_PAPER=1 is explicitly set."""
    if os.getenv("TEMPEST_PAPER") != "1":
        raise RuntimeError(
            "TEMPEST_PAPER=1 required: Tempest trades PAPER accounts only. "
            "Set it in .env (never unset it for a live account)."
        )


def get_trading_client():
    """Build the Alpaca PAPER trading client. Raises unless paper env is set
    and keys exist."""
    require_paper_env()
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing in .env")
    from alpaca.trading.client import TradingClient
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)


class AlpacaSource(BarSource):
    """1-minute bars from Alpaca (full history on the free tier)."""

    def __init__(self, api_key: str | None = None, secret_key: str | None = None):
        self._key = api_key or ALPACA_API_KEY
        self._secret = secret_key or ALPACA_SECRET_KEY
        if not self._key or not self._secret:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")

    def _client(self):
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(self._key, self._secret)

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            req = StockBarsRequest(
                symbol_or_symbols=symbol.upper(),
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                adjustment="raw",
            )
            df = self._client().get_stock_bars(req).df
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.reset_index()
        out = pd.DataFrame({
            "symbol": symbol.upper(),
            "bar_ts_utc": pd.to_datetime(df["timestamp"], utc=True),
            "open": df["open"].astype(float),
            "high": df["high"].astype(float),
            "low": df["low"].astype(float),
            "close": df["close"].astype(float),
            "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0),
        })
        out["source"] = "alpaca_1m"
        out["ingested_at_utc"] = datetime.now(timezone.utc)
        return out.sort_values("bar_ts_utc").reset_index(drop=True)
