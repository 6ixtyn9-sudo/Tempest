"""Validation discipline for Tempest backtests. Mirrors Price's doctrine:
chronological splits, cost-adjusted returns, walk-forward, honest stats.

Costs: microcap spreads/slippage are brutal. The default model is generous
(entry + exit legs), and the backtest reports BOTH gross and net so the
cost assumption is never hidden.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class CostModel:
    spread_bps: float = 25.0     # half-spread per leg (microcaps are wide)
    slippage_bps: float = 25.0   # adverse fill per leg
    commission_bps: float = 0.0  # modern zero-commission retail

    def leg_bps(self) -> float:
        return self.spread_bps + self.slippage_bps + self.commission_bps

    def round_trip_bps(self) -> float:
        return 2.0 * self.leg_bps()

    def net_return(self, gross: float) -> float:
        return gross - self.round_trip_bps() / 10000.0


@dataclass
class TradeResult:
    symbol: str
    session: object
    entry_ts: str
    entry_price: float
    exit_price: float
    exit_reason: str          # "stop" | "target" | "horizon"
    gross_return: float
    net_return: float
    r_multiple: float
    held_bars: int
    bucket: dict              # gap band, relvol band, hour, float band

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "session": str(self.session),
            "entry_ts": self.entry_ts, "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4), "exit_reason": self.exit_reason,
            "gross_return": round(self.gross_return, 6),
            "net_return": round(self.net_return, 6),
            "r_multiple": round(self.r_multiple, 3), "held_bars": self.held_bars,
            "bucket": self.bucket,
        }


def bucket_for(gap_open: float, relvol: float, price: float, entry_hour_et: int) -> dict:
    """Discretise a trade into the analysis buckets — the 'find edges the
    course missed' surface: gap band, relvol band, price band, hour band."""
    def _band(v, edges, labels):
        for e, lab in zip(edges, labels):
            if v < e:
                return lab
        return labels[-1]
    # Edges are the UPPER bound of each label. A 3% gap is 2-5%, not 5-10%.
    return {
        "gap_band": _band(gap_open, [0.05, 0.10], ["2-5%", "5-10%", "10%+"]),
        "relvol_band": _band(relvol, [10, 20], ["5-10x", "10-20x", "20x+"]),
        "price_band": _band(price, [5, 10], ["2-5", "5-10", "10-20"]),
        "hour_et": entry_hour_et,
    }


def summarize(results: list[TradeResult]) -> dict:
    """Honest summary: gross AND net, win rate, avg R, by exit reason."""
    if not results:
        return {"n": 0}
    gross = np.array([r.gross_return for r in results])
    net = np.array([r.net_return for r in results])
    r_m = np.array([r.r_multiple for r in results])
    reasons = {}
    for r in results:
        reasons[r.exit_reason] = reasons.get(r.exit_reason, 0) + 1
    return {
        "n": len(results),
        "gross_mean": round(float(gross.mean()), 6),
        "net_mean": round(float(net.mean()), 6),
        "win_rate": round(float((net > 0).mean()), 4),
        "avg_r": round(float(r_m.mean()), 3),
        "exit_reasons": reasons,
    }


def walk_forward_folds(
    df: pd.DataFrame, n_folds: int = 4, ts_col: str = "entry_ts"
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Chronological expanding-window folds. Returns (train, valid) pairs."""
    if df is None or df.empty:
        return []
    df = df.sort_values(ts_col).reset_index(drop=True)
    n = len(df)
    if n < n_folds + 1:
        return []
    edges = np.linspace(0, n, n_folds + 2).astype(int)
    folds = []
    for i in range(1, n_folds + 1):
        train = df.iloc[: edges[i]]
        valid = df.iloc[edges[i]: edges[i + 1]]
        if len(valid) > 0 and len(train) > 0:
            folds.append((train, valid))
    return folds
