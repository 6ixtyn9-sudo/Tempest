"""Mechanical encoding of the momentum methodology (the video's rules as
HYPOTHESES, not mandates). All thresholds are fixed priors from the course;
the backtest measures whether they actually select winners.

Five selection pillars (all demand except float):
  relvol >= 5x, total volume >= 1M, gap >= +2%, price $2-20, float < 20M.
  (news catalyst is treated as OPTIONAL metadata, not a pillar — the course
  itself says the exception is allowed; whether news matters is a measured
  bucket, not an assumption.)

Entry pattern — the first pullback, on 1-minute bars:
  1. Squeeze: N consecutive green candles (default 3) closing near high.
  2. Pullback: retraces <= 50% of the squeeze move, holds VWAP and 9-EMA,
     green-candle volume > red-candle volume on the pullback candles.
  3. Entry: first candle to trade above the high of the prior candle
     (the crossing candle) after the pullback base.
  4. Stop: the low of the pullback. Target: >= 2:1 reward:risk.
  5. Exit simulation (backtest): stop or 2R target, or horizon close
     (default 15 min). Topping-tail / volume-burst exits are approximations
     of the course's Level-2 tape reading and are NOT simulated in v1.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

# --- Pillar thresholds (fixed priors from the course) ---------------------
REL_VOL_MIN = 5.0          # relative volume >= 5x
TOTAL_VOL_MIN = 1_000_000  # total shares traded (session running total)
GAP_MIN = 0.02             # open vs prior close >= +2%
PRICE_MIN = 2.0
PRICE_MAX = 20.0
FLOAT_MAX = 20_000_000     # shares outstanding (supply pillar)

# --- Pattern parameters (fixed priors, tunable later only with evidence) --
SQUEEZE_BARS = 3           # consecutive green candles
MAX_RETRACE = 0.50         # pullback may not retrace more than 50%
HOLD_MINUTES = 15          # default exit horizon
RR_TARGET = 2.0            # reward:risk target for the exit simulation


@dataclass
class Pillars:
    symbol: str
    relvol: float
    total_volume: float
    gap_open: float
    price: float
    float_shares: Optional[float]
    passes: bool
    reasons: list

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "relvol": round(self.relvol, 2),
            "total_volume": int(self.total_volume),
            "gap_open": round(self.gap_open, 4), "price": round(self.price, 2),
            "float_shares": self.float_shares, "passes": self.passes,
            "reasons": self.reasons,
        }


def screen_pillars(
    symbol: str,
    relvol: float,
    total_volume: float,
    gap_open: float,
    price: float,
    float_shares: Optional[float] = None,
) -> Pillars:
    """Evaluate the five pillars for one symbol on one session."""
    reasons = []
    if not np.isfinite(relvol) or relvol < REL_VOL_MIN:
        reasons.append(f"relvol {relvol:.1f} < {REL_VOL_MIN}")
    if total_volume < TOTAL_VOL_MIN:
        reasons.append(f"total_volume {int(total_volume):,} < {TOTAL_VOL_MIN:,}")
    if not np.isfinite(gap_open) or gap_open < GAP_MIN:
        reasons.append(f"gap {gap_open:.4f} < {GAP_MIN}")
    if not (PRICE_MIN <= price <= PRICE_MAX):
        reasons.append(f"price {price:.2f} outside [{PRICE_MIN},{PRICE_MAX}]")
    if float_shares is not None and float_shares >= FLOAT_MAX:
        reasons.append(f"float {float_shares:,} >= {FLOAT_MAX:,}")
    return Pillars(
        symbol=symbol, relvol=relvol, total_volume=total_volume,
        gap_open=gap_open, price=price, float_shares=float_shares,
        passes=len(reasons) == 0, reasons=reasons,
    )


@dataclass
class PullbackSignal:
    symbol: str
    session: object
    entry_ts: pd.Timestamp
    entry_price: float
    stop_price: float
    target_price: float
    squeeze_high: float
    pullback_low: float

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "session": str(self.session),
            "entry_ts": str(self.entry_ts), "entry_price": round(self.entry_price, 4),
            "stop_price": round(self.stop_price, 4),
            "target_price": round(self.target_price, 4),
            "squeeze_high": round(self.squeeze_high, 4),
            "pullback_low": round(self.pullback_low, 4),
        }


def detect_first_pullback(
    df: pd.DataFrame,
    symbol: str,
    squeeze_bars: int = SQUEEZE_BARS,
    max_retrace: float = MAX_RETRACE,
    rr_target: float = RR_TARGET,
) -> list[PullbackSignal]:
    """Detect first-pullback entries on a featured 1m frame (from
    features.compute_features). Returns one signal per qualifying pattern.

    Mechanics (per session, look-ahead-free):
      - find a run of >= squeeze_bars green candles (squeeze)
      - the pullback is the next red candle(s) that retrace <= max_retrace
        of the squeeze's high-to-low range, hold VWAP and 9-EMA, and have
        lower volume than the squeeze's green candles
      - entry = first candle whose high breaks the prior candle's high
      - stop = pullback low; target = entry + rr_target * (entry - stop)
    """
    if df is None or df.empty or "vwap" not in df.columns:
        return []
    signals: list[PullbackSignal] = []
    for _, grp in df.groupby("session", sort=True):
        grp = grp.sort_values("bar_ts_utc").reset_index(drop=True)
        signals.extend(_detect_session(grp, symbol, squeeze_bars, max_retrace, rr_target))
    return signals


def _detect_session(grp, symbol, squeeze_bars, max_retrace, rr_target):
    out = []
    o = grp["open"].values
    close = grp["close"].values
    high = grp["high"].values
    low = grp["low"].values
    vol = grp["volume"].values
    vwap = grp["vwap"].values
    ema9 = grp["ema9"].values
    n = len(grp)
    i = 0
    while i < n - 1:
        # 1. Squeeze: run of green candles.
        if close[i] <= o[i]:
            i += 1
            continue
        run = 1
        while i + run < n and close[i + run] > o[i + run] and run < 12:
            run += 1
        if run < squeeze_bars:
            i += run
            continue
        squeeze_high = max(high[i:i + run])
        base = min(low[i:i + run])

        # 2. Pullback: red (or stalling doji) candles after the squeeze,
        #    retrace <= max_retrace, hold VWAP and 9-EMA, and red candles
        #    must be LIGHTER than the squeeze's average volume (heavy red =
        #    distribution, not a pullback). Any rule break invalidates the
        #    whole pattern (the course: "if it breaks VWAP, it's not a
        #    pullback anymore").
        squeeze_avg_vol = float(np.mean(vol[i:i + run]))
        j = i + run
        pb_start = j
        pullback_valid = True
        while j < n and close[j] <= o[j]:
            if low[j] < vwap[j] or close[j] < ema9[j]:
                pullback_valid = False
                break
            if close[j] < o[j] and vol[j] > squeeze_avg_vol:
                pullback_valid = False
                break
            j += 1
        if not pullback_valid or j == pb_start:
            i = max(j, i + 1)
            continue
        pb_low = min(low[pb_start:j])
        retrace = (squeeze_high - pb_low) / (squeeze_high - base + 1e-9)
        if retrace > max_retrace:
            i = j
            continue

        # 3. Entry: first candle to break the prior candle's high.
        k = j
        while k < n:
            if high[k] > high[k - 1] and high[k] > squeeze_high * 0.98:
                entry_price = max(close[k], o[k])  # fill at the break
                stop_price = pb_low
                risk = entry_price - stop_price
                if risk <= 0:
                    i = k + 1
                    break
                target_price = entry_price + rr_target * risk
                out.append(PullbackSignal(
                    symbol=symbol, session=grp["session"].iloc[0],
                    entry_ts=grp["bar_ts_utc"].iloc[k],
                    entry_price=float(entry_price), stop_price=float(stop_price),
                    target_price=float(target_price),
                    squeeze_high=float(squeeze_high), pullback_low=float(pb_low),
                ))
                i = k + 1
                break
            k += 1
        else:
            i = j
    return out
