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

    def _fresh_signal(self, bars: pd.DataFrame, symbol: str):
        """Return the first-pullback signal whose entry bar is the latest
        completed bar, or None."""
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
        last_ts = today["bar_ts_utc"].iloc[-1]
        fresh = [s for s in sigs if s.entry_ts == last_ts]
        return fresh[-1] if fresh else None

    def _entry_qty(self, price: float) -> int:
        qty = int(self.limits.max_notional_per_position // price)
        return max(0, qty)

    def _try_entry(self, symbol: str, open_positions: pd.DataFrame, dry_run: bool) -> dict:
        bars = self._candidate_bars(symbol)
        sig = self._fresh_signal(bars, symbol)
        if sig is None:
            return {"symbol": symbol, "action": "watching",
                    "reason": "no fresh first-pullback signal"}
        qty = self._entry_qty(sig.entry_price)
        if qty < 1:
            return {"symbol": symbol, "action": "blocked",
                    "reason": "qty < 1 at max notional"}
        ok, reasons = check_entry_ok(
            symbol, qty, sig.entry_price, self.limits, open_positions
        )
        if not ok:
            return {"symbol": symbol, "action": "blocked", "reason": "; ".join(reasons)}
        risk = sig.entry_price - sig.stop_price
        if risk <= 0:
            return {"symbol": symbol, "action": "blocked", "reason": "non-positive risk"}
        target = sig.entry_price + 2.0 * risk
        cid = f"tempest-{symbol.upper()}-{uuid.uuid4().hex[:8]}"
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol.upper(), "action": "entry", "side": "buy",
            "qty": qty, "price": round(sig.entry_price, 4),
            "order_id": cid, "status": "dry_run" if dry_run else "submitted",
            "setup": "first_pullback", "session": str(sig.session),
            "entry_price": round(sig.entry_price, 4),
            "stop_price": round(sig.stop_price, 4),
            "target_price": round(target, 4),
        }
        if dry_run:
            append_journal({**row, "reason": "dry_run"})
            return {"symbol": symbol.upper(), "action": "would_enter",
                    "qty": qty, "price": round(sig.entry_price, 4),
                    "stop": round(sig.stop_price, 4), "target": round(target, 4)}
        try:
            self.client = self.client or self.broker.get_trading_client()
            self.broker.submit_bracket(
                self.client, symbol, qty, "buy", sig.entry_price,
                sig.stop_price, target, cid,
            )
            append_journal(row)
            return {"symbol": symbol.upper(), "action": "entered",
                    "qty": qty, "price": round(sig.entry_price, 4),
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
