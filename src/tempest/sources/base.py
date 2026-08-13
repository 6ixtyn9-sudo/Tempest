"""Data source interface. The pilot uses yfinance 1m; a Polygon adapter can
slot in later (paid, full history) without touching downstream code."""

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd


class BarSource(ABC):
    """Fetch 1-minute bars for a symbol in [start, end)."""

    @abstractmethod
    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Return canonical 1m bars: symbol, bar_ts_utc, open, high, low,
        close, volume, source. Empty frame on failure (never raise)."""


class PolygonSource(BarSource):
    """Paid full-history adapter. Not wired yet: requires POLYGON_API_KEY
    and a paid plan. Intentionally a stub so the interface is real but the
    spend decision is deferred until the pilot shows promise."""

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        raise NotImplementedError(
            "Polygon adapter is not wired; run the yfinance pilot first. "
            "See HANDOVER.md 'Deferred'."
        )
