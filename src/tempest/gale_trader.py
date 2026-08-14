"""Alpaca PAPER execution lane for Gale ORB5.

All broker lifecycle, account exposure, cooldown and loss controls are inherited
from the hardened shared PaperTrader. Gale overrides only signal discovery and
its narrower 09:35-11:00 ET entry window.
"""

import pandas as pd

from tempest.features import compute_features
from tempest.gale import STRATEGY_ID, detect_gale_orb, shadow_entry_price
from tempest.gale_shadow import first_seen_before
from tempest.trader import PaperTrader


class GalePaperTrader(PaperTrader):
    strategy_id = STRATEGY_ID
    setup_name = "orb5"
    order_prefix = "gale"
    no_signal_reason = "no fresh ORB5 signal"

    def __init__(self, broker, source, screen_evidence: pd.DataFrame, **kwargs):
        super().__init__(broker, source, **kwargs)
        self.screen_evidence = screen_evidence

    def _max_entry_slippage(self) -> float:
        return 0.005

    def _entry_window_open(self) -> tuple[bool, str]:
        now_et = pd.Timestamp(self._now()).tz_convert("America/New_York")
        if now_et.weekday() >= 5:
            return False, "weekend"
        minute = now_et.hour * 60 + now_et.minute
        if minute < 9 * 60 + 35:
            return False, "before 09:35 ET"
        if minute >= 11 * 60:
            return False, "after 11:00 ET"
        return True, ""

    def _fresh_signal(self, bars: pd.DataFrame, symbol: str):
        if bars is None or bars.empty:
            return None
        feat = compute_features(bars)
        if feat is None or feat.empty or "session" not in feat.columns:
            return None
        now = self._now()
        now_et = pd.Timestamp(now).tz_convert("America/New_York")
        session = feat["session"].iloc[-1]
        if session != now_et.date():
            return None
        latest_ts = pd.Timestamp(feat["bar_ts_utc"].iloc[-1]).tz_convert("UTC")
        lag = (pd.Timestamp(now) - latest_ts).total_seconds() / 60.0
        if lag < 0 or lag > self._max_bar_age_minutes():
            return None
        session_rows = feat[feat["session"] == session].reset_index(drop=True)
        signals = detect_gale_orb(session_rows, symbol)
        if not signals:
            return None
        signal = signals[-1]
        indices = session_rows.index[session_rows["bar_ts_utc"] == signal.signal_ts]
        if len(indices) != 1:
            return None
        age = len(session_rows) - 1 - int(indices[0])
        if age > self._max_signal_age_bars():
            return None
        first_seen = first_seen_before(
            self.screen_evidence,
            symbol,
            str(session),
            signal.signal_ts + pd.Timedelta(minutes=1),
        )
        if first_seen is None:
            return None
        observed_price = float(session_rows["close"].iloc[-1])
        if shadow_entry_price(signal, observed_price) is None:
            return None
        signal.age_bars = age
        signal.last_price = observed_price
        signal.first_seen_at_utc = first_seen.isoformat()
        return signal
