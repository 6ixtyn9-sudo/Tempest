"""Canonical one-minute bar source interface."""

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd


class BarSource(ABC):
    """Fetch 1-minute bars for a symbol in [start, end)."""

    @abstractmethod
    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Return canonical 1m bars: symbol, bar_ts_utc, open, high, low,
        close, volume, source. Empty frame on failure (never raise)."""
