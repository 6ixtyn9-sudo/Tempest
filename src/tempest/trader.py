"""The paper momentum trader: screen -> first-pullback -> bracket -> manage.

One run:
  1. Refresh open positions from the (paper) broker.
  2. Exit management: horizon reached or near market close -> close.
  3. For each candidate symbol: risk-gate, fetch recent 1m bars, detect a
     first-pullback signal whose crossing candle is the LATEST COMPLETED
     bar, submit a DAY bracket (entry limit + 2R take-profit + stop at the
     pullback low). Journal everything.

The pattern must form on the latest completed bar — no chasing older
signals. Nothing here can touch a live account (broker is paper-only).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd

from tempest.features import compute_features
from tempest.risk import (
    RiskLimits, append_journal, check_entry_ok, load_journal, record_cooldown,
)
from tempest.strategy import detect_first_pullback


def _ny_minutes_to_close(now_utc: datetime) -> int:
    """Minutes from now to 16:00 ET (regular close). Negative after close."""
    try:
        from zoneinfo import ZoneInfo
        ny = now_utc.astimezone(ZoneInfo("America/New_York"))
        close = ny.replace(hour=16, minute=0, second=0, microsecond=0)
        return int((close - ny).total_seconds() // 60)
    except Exception:
        return 9999


class PaperTrader:
    def __init__(self, broker, source, limits: RiskLimits | None = None):
        self.broker = broker
        self.source = source
        self.limits = limits or RiskLimits()
        self.client = None  # lazily built (tests inject a fake)

    # -- entry ------------------------------------------------------------
    def _candidate_bars(self, symbol: str, lookback_days: int = 10) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        return self.source.fetch_1m(symbol, start, end)

    def _max_signal_age_bars(self) -> int:
        """How many bars old a first-pullback signal may be and still be
        actionable. Polling cannot land on the exact signal bar often enough:
        a session is 390 bars, so a 5-minute poll sees ~78 of them. Requiring
        age==0 made entries near-impossible.

        The window must be >= the poll interval or signals still slip between
        polls: measured on real 1m data, a 5-minute poll with a 3-bar window
        caught 33% of signals, with a 5-bar window 100%. Override with
        TEMPEST_SIGNAL_MAX_AGE_BARS."""
        import os
        try:
            return max(0, int(os.getenv("TEMPEST_SIGNAL_MAX_AGE_BARS", "5")))
        except ValueError:
            return 5

    def _max_entry_slippage(self) -> float:
        """Max fraction the price may run past the signal's entry before the
        setup counts as chased. Override with TEMPEST_MAX_ENTRY_SLIPPAGE."""
        import os
        try:
            return max(0.0, float(os.getenv("TEMPEST_MAX_ENTRY_SLIPPAGE", "0.01")))
        except ValueError:
            return 0.01

    def _fresh_signal(self, bars: pd.DataFrame, symbol: str):
        """Return the most recent actionable first-pullback signal, or None.

        Accepts a signal whose entry bar is within _max_signal_age_bars() of
        the latest completed bar. The caller re-prices at the current bar, so
        a stale signal is only taken if the setup is still intact.
        """
        if bars is None or bars.empty:
            return None
        feat = compute_features(bars)
        if feat is None or feat.empty or "session" not in feat.columns:
            return None
        current_session = feat["session"].iloc[-1]
        today = feat[feat["session"] == current_session]
        if today.empty:
            return None
        sigs = detect_first_pullback(today, symbol)
        if not sigs:
            return None
        ts = list(today["bar_ts_utc"])
        index_of = {t: i for i, t in enumerate(ts)}
        last_i = len(ts) - 1
        max_age = self._max_signal_age_bars()
        fresh = [s for s in sigs
                 if last_i - index_of.get(s.entry_ts, -10_000) <= max_age
                 and index_of.get(s.entry_ts, -10_000) >= 0]
        if not fresh:
            return None
        sig = fresh[-1]
        sig.age_bars = last_i - index_of[sig.entry_ts]
        sig.last_price = float(today["close"].iloc[-1])
        return sig

    def _entry_qty(self, price: float) -> int:
        qty = int(self.limits.max_notional_per_position // price)
        return max(0, qty)

    def _try_entry(self, symbol: str, open_positions: pd.DataFrame, dry_run: bool) -> dict:
        bars = self._candidate_bars(symbol)
        sig = self._fresh_signal(bars, symbol)
        if sig is None:
            return {"symbol": symbol, "action": "watching",
                    "reason": "no fresh first-pullback signal"}
        age = getattr(sig, "age_bars", 0)
        # Re-price a stale signal at the latest bar. The backtest fills on the
        # breakout bar; a poll that arrives N bars later must not pretend it
        # got that price. Enter at the current price or skip.
        fill = float(getattr(sig, "last_price", sig.entry_price))
        if age > 0:
            if fill <= sig.stop_price:
                return {"symbol": symbol, "action": "watching",
                        "reason": f"signal {age}b old, price {fill:.4f} below stop "
                                  f"{sig.stop_price:.4f} - setup broken"}
            slip = (fill - sig.entry_price) / max(sig.entry_price, 1e-9)
            if slip > self._max_entry_slippage():
                return {"symbol": symbol, "action": "watching",
                        "reason": f"signal {age}b old, price ran {slip:.2%} past "
                                  f"entry - chasing"}
        qty = self._entry_qty(fill)
        if qty < 1:
            return {"symbol": symbol, "action": "blocked",
                    "reason": "qty < 1 at max notional"}
        ok, reasons = check_entry_ok(
            symbol, qty, fill, self.limits, open_positions
        )
        if not ok:
            return {"symbol": symbol, "action": "blocked", "reason": "; ".join(reasons)}
        risk = fill - sig.stop_price
        if risk <= 0:
            return {"symbol": symbol, "action": "blocked", "reason": "non-positive risk"}
        target = fill + 2.0 * risk
        cid = f"tempest-{symbol.upper()}-{uuid.uuid4().hex[:8]}"
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol.upper(), "action": "entry", "side": "buy",
            "qty": qty, "price": round(fill, 4),
            "order_id": cid, "status": "dry_run" if dry_run else "submitted",
            "setup": "first_pullback", "session": str(sig.session),
            "signal_ts": str(sig.entry_ts), "signal_age_bars": age,
            "entry_price": round(fill, 4),
            "stop_price": round(sig.stop_price, 4),
            "target_price": round(target, 4),
        }
        if dry_run:
            append_journal({**row, "reason": "dry_run"})
            return {"symbol": symbol.upper(), "action": "would_enter",
                    "qty": qty, "price": round(fill, 4), "age_bars": age,
                    "stop": round(sig.stop_price, 4), "target": round(target, 4)}
        try:
            self.client = self.client or self.broker.get_trading_client()
            self.broker.submit_bracket(
                self.client, symbol, qty, "buy", fill,
                sig.stop_price, target, cid,
            )
            append_journal(row)
            return {"symbol": symbol.upper(), "action": "entered",
                    "qty": qty, "price": round(fill, 4), "age_bars": age,
                    "stop": round(sig.stop_price, 4), "target": round(target, 4)}
        except Exception as e:  # noqa: BLE001 - journal the failure
            append_journal({**row, "status": "rejected", "reason": str(e)})
            return {"symbol": symbol.upper(), "action": "rejected", "reason": str(e)}

    # -- exits ------------------------------------------------------------
    def _manage_exits(self, open_positions: pd.DataFrame, dry_run: bool) -> list[dict]:
        if open_positions.empty:
            return []
        journal = load_journal()
        out = []
        now = datetime.now(timezone.utc)
        mins_to_close = _ny_minutes_to_close(now)
        for _, pos in open_positions.iterrows():
            sym = str(pos["symbol"]).upper()
            entries = journal[
                (journal["symbol"].astype(str).str.upper() == sym)
                & (journal["action"] == "entry")
            ]
            entry_ts = None
            if not entries.empty:
                try:
                    entry_ts = pd.to_datetime(entries["timestamp_utc"].iloc[-1], utc=True)
                except Exception:
                    entry_ts = None
            held_bars = None
            if entry_ts is not None and pd.notna(entry_ts):
                held_bars = int((now - entry_ts).total_seconds() // 60)
            horizon_hit = held_bars is not None and held_bars >= self.limits.horizon_bars
            close_soon = mins_to_close <= self.limits.close_before_market_close_minutes
            if not (horizon_hit or close_soon):
                continue
            exit_price = float(pos["current_price"])
            qty = float(pos["qty"])
            avg = float(pos["avg_entry_price"])
            pnl = (exit_price - avg) * qty
            reason = "horizon" if horizon_hit else "near_close"
            if dry_run:
                out.append({"symbol": sym, "action": "would_exit", "reason": reason,
                            "pnl": round(pnl, 2)})
                continue
            try:
                self.client = self.client or self.broker.get_trading_client()
                self.broker.close_position(self.client, sym)
            except Exception as e:  # noqa: BLE001
                out.append({"symbol": sym, "action": "close_failed", "reason": str(e)})
                continue
            append_journal({
                "timestamp_utc": now.isoformat(), "symbol": sym, "action": "exit",
                "side": "sell", "qty": qty, "price": round(exit_price, 4),
                "status": "filled", "session": str(now.date()),
                "entry_price": round(avg, 4), "exit_price": round(exit_price, 4),
                "pnl": round(pnl, 2), "reason": reason,
            })
            record_cooldown(sym)
            out.append({"symbol": sym, "action": "exited", "reason": reason,
                        "pnl": round(pnl, 2)})
        return out

    # -- main loop ---------------------------------------------------------
    def run_once(self, candidates: list[str], dry_run: bool = False) -> dict:
        self.client = None
        open_positions = self.broker.get_open_positions()
        exits = self._manage_exits(open_positions, dry_run)
        entries = []
        for sym in candidates:
            entries.append(self._try_entry(sym, open_positions, dry_run))
        return {
            "open_positions": len(open_positions),
            "exits": exits,
            "entries": entries,
        }
