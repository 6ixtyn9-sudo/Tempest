"""Gale: fixed-prior five-minute opening-range breakout hypothesis.

Gale shares Tempest's RTH features, captured mover universe and PAPER
broker lifecycle while retaining independent strategy identity and reports.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tempest.features import compute_features

STRATEGY_ID = "gale_orb5"
OPEN_MINUTE = 9 * 60 + 30
RANGE_END_MINUTE = 9 * 60 + 35
ENTRY_CUTOFF_MINUTE = 11 * 60
OPENING_RANGE_BARS = 5
MAX_RANGE_WIDTH = 0.05
MAX_CHASE = 0.005
VOLUME_MULTIPLIER = 1.5
RR_TARGET = 2.0
HOLD_BARS = 15


@dataclass
class GaleSignal:
    symbol: str
    session: object
    signal_ts: pd.Timestamp
    trigger_price: float
    stop_price: float
    opening_range_high: float
    opening_range_low: float
    opening_range_width: float
    breakout_volume_ratio: float
    vwap: float

    @property
    def entry_ts(self) -> pd.Timestamp:
        return self.signal_ts

    @property
    def entry_price(self) -> float:
        return self.trigger_price

    @property
    def target_price(self) -> float:
        return target_for(self.trigger_price, self.stop_price)

    @property
    def signal_id(self) -> str:
        stamp = pd.Timestamp(self.signal_ts).isoformat()
        return f"{STRATEGY_ID}|{self.session}|{self.symbol.upper()}|{stamp}"

    def to_dict(self) -> dict:
        return {
            "strategy_id": STRATEGY_ID,
            "signal_id": self.signal_id,
            "symbol": self.symbol.upper(),
            "session": str(self.session),
            "signal_ts": pd.Timestamp(self.signal_ts).isoformat(),
            "trigger_price": round(self.trigger_price, 4),
            "stop_price": round(self.stop_price, 4),
            "opening_range_high": round(self.opening_range_high, 4),
            "opening_range_low": round(self.opening_range_low, 4),
            "opening_range_width": round(self.opening_range_width, 6),
            "breakout_volume_ratio": round(self.breakout_volume_ratio, 4),
            "vwap": round(self.vwap, 4),
        }


def _ny_minute(ts: pd.Series) -> pd.Series:
    ny = pd.to_datetime(ts, utc=True).dt.tz_convert("America/New_York")
    return ny.dt.hour * 60 + ny.dt.minute


def detect_gale_orb(df: pd.DataFrame, symbol: str) -> list[GaleSignal]:
    """Return at most the first valid ORB5 signal per RTH session."""
    if df is None or df.empty:
        return []
    feat = df if {"vwap", "session"}.issubset(df.columns) else compute_features(df)
    if feat is None or feat.empty:
        return []
    out: list[GaleSignal] = []
    for session, raw in feat.groupby("session", sort=True):
        grp = raw.sort_values("bar_ts_utc").reset_index(drop=True).copy()
        grp["ny_minute"] = _ny_minute(grp["bar_ts_utc"])
        opening = grp[
            (grp["ny_minute"] >= OPEN_MINUTE)
            & (grp["ny_minute"] < RANGE_END_MINUTE)
        ]
        expected_minutes = set(range(OPEN_MINUTE, RANGE_END_MINUTE))
        if len(opening) != OPENING_RANGE_BARS or set(opening["ny_minute"]) != expected_minutes:
            continue
        range_high = float(opening["high"].max())
        range_low = float(opening["low"].min())
        if range_low <= 0:
            continue
        range_width = (range_high - range_low) / range_low
        if range_width > MAX_RANGE_WIDTH:
            continue

        candidates = grp[
            (grp["ny_minute"] >= RANGE_END_MINUTE)
            & (grp["ny_minute"] < ENTRY_CUTOFF_MINUTE)
        ]
        for idx in candidates.index:
            if idx <= 0:
                continue
            row = grp.loc[idx]
            prev = grp.loc[idx - 1]
            close = float(row["close"])
            vwap = float(row["vwap"])
            if not np.isfinite(vwap) or close <= vwap:
                continue
            if close <= range_high or float(prev["close"]) > range_high:
                continue
            chase = (close - range_high) / range_high
            if chase > MAX_CHASE:
                continue
            prior_volume = pd.to_numeric(
                grp.loc[max(0, idx - 5):idx - 1, "volume"], errors="coerce"
            ).dropna()
            baseline = float(prior_volume.median()) if not prior_volume.empty else 0.0
            if baseline <= 0:
                continue
            volume_ratio = float(row["volume"]) / baseline
            if volume_ratio < VOLUME_MULTIPLIER:
                continue
            out.append(GaleSignal(
                symbol=str(symbol).upper(),
                session=session,
                signal_ts=pd.Timestamp(row["bar_ts_utc"]),
                trigger_price=close,
                stop_price=range_low,
                opening_range_high=range_high,
                opening_range_low=range_low,
                opening_range_width=range_width,
                breakout_volume_ratio=volume_ratio,
                vwap=vwap,
            ))
            break
    return out


def shadow_entry_price(signal: GaleSignal, observed_price: float) -> float | None:
    """Price a signal when the poll observes it; reject a >0.5% chase."""
    price = float(observed_price)
    if price <= signal.stop_price or price < signal.opening_range_high or price <= 0:
        return None
    chase = (price - signal.opening_range_high) / signal.opening_range_high
    if chase > MAX_CHASE:
        return None
    return price


def target_for(entry_price: float, stop_price: float) -> float:
    risk = float(entry_price) - float(stop_price)
    if risk <= 0:
        raise ValueError("entry must be above stop")
    return float(entry_price) + RR_TARGET * risk
