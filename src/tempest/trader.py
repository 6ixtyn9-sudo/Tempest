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

import math
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd

from tempest.features import compute_features
from tempest.risk import (
    RiskLimits,
    append_journal,
    check_entry_ok,
    load_journal,
    open_journal_entries,
    pending_exit_orders,
    pending_journal_orders,
    record_cooldown,
)
from tempest.strategy import detect_first_pullback
from tempest.validation import CostModel


def _authoritative_clock(broker) -> dict:
    """Read and validate broker market time; never infer an open market."""
    if not hasattr(broker, "get_clock_info"):
        raise RuntimeError("broker does not expose authoritative market clock")
    try:
        info = broker.get_clock_info()
        required = {"is_open", "timestamp_utc", "minutes_to_close"}
        if not isinstance(info, dict) or not required.issubset(info):
            raise ValueError("missing required fields")
        if type(info["is_open"]) is not bool:  # do not coerce strings truthy
            raise ValueError("is_open is not boolean")
        timestamp = pd.Timestamp(info["timestamp_utc"])
        if pd.isna(timestamp):
            raise ValueError("timestamp is missing")
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        raw_minutes = info["minutes_to_close"]
        if isinstance(raw_minutes, bool):
            raise ValueError("minutes_to_close is boolean")
        minutes_float = float(raw_minutes)
        if (
            not math.isfinite(minutes_float)
            or minutes_float < 0
            or not minutes_float.is_integer()
        ):
            raise ValueError("minutes_to_close is invalid")
        minutes = int(minutes_float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"broker returned invalid market clock: {exc}") from exc
    return {
        **info,
        "is_open": info["is_open"],
        "timestamp_utc": timestamp,
        "minutes_to_close": minutes,
    }


class PaperTrader:
    strategy_id = "tempest_first_pullback"
    setup_name = "first_pullback"
    order_prefix = "tempest"
    no_signal_reason = "no fresh first-pullback signal"

    def __init__(
        self, broker, source, limits: RiskLimits | None = None, now_fn=None,
    ):
        self.broker = broker
        self.source = source
        self.limits = limits or RiskLimits()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.client = None  # lazily built (tests inject a fake)
        self.clock_info = None
        self.account_day_pnl = None

    def _now(self) -> datetime:
        """Process timestamp for evidence only, never market-open authority."""
        now = self.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _clock(self) -> dict:
        if self.clock_info is None:
            self.clock_info = _authoritative_clock(self.broker)
        return self.clock_info

    def _clock_now(self) -> pd.Timestamp:
        return pd.Timestamp(self._clock()["timestamp_utc"])

    def _completed_bars(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Drop any minute whose closing boundary is still in the future."""
        if bars is None or bars.empty:
            return bars
        out = bars.copy()
        timestamps = pd.to_datetime(out["bar_ts_utc"], utc=True, errors="coerce")
        complete = timestamps + pd.Timedelta(minutes=1) <= self._clock_now()
        return out.loc[complete & timestamps.notna()].reset_index(drop=True)

    # -- entry ------------------------------------------------------------
    def _candidate_bars(self, symbol: str, lookback_days: int = 10) -> pd.DataFrame:
        end = self._clock_now().to_pydatetime()
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

    def _max_bar_age_minutes(self) -> float:
        """Maximum wall-clock lag allowed for the latest completed bar."""
        import os
        try:
            return max(1.0, float(os.getenv("TEMPEST_MAX_BAR_AGE_MINUTES", "10")))
        except ValueError:
            return 10.0

    def _fresh_signal(self, bars: pd.DataFrame, symbol: str):
        """Return the most recent actionable current-session signal.

        Bar-count freshness alone is insufficient: a five-bar-old signal from
        yesterday is still five bars old. Require today's ET session and a
        genuinely recent latest completed bar as well.
        """
        if bars is None or bars.empty:
            return None
        completed = self._completed_bars(bars)
        feat = compute_features(completed)
        if feat is None or feat.empty or "session" not in feat.columns:
            return None
        now = self._clock_now()
        now_et = now.tz_convert("America/New_York")
        current_session = feat["session"].iloc[-1]
        if current_session != now_et.date():
            return None
        latest_ts = pd.Timestamp(feat["bar_ts_utc"].iloc[-1])
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.tz_localize("UTC")
        else:
            latest_ts = latest_ts.tz_convert("UTC")
        lag_minutes = (pd.Timestamp(now) - latest_ts).total_seconds() / 60.0
        if lag_minutes < 0 or lag_minutes > self._max_bar_age_minutes():
            return None
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

    def _min_risk_bps(self) -> float:
        """Minimum stop distance in bps of entry: the break-even rail.

        Derivation, not a preference. The strategy exits at a 2R target, so
        a winning trade grosses 2 * risk. It is only worth taking if that
        clears the modelled round trip:

            2 * risk_bps - round_trip_bps > 0
            risk_bps > round_trip_bps / 2          (= 50 bps by default)

        Below this line a trade cannot make money even when it WINS, which
        is a correctness failure rather than a bad setup. Measured example
        (live 1m bars, 2026-08-14): RRGB entry 9.725 / stop 9.715 = 10.3 bps
        of risk; a 2R "win" grosses 20.6 bps against 100 bps of cost.

        This is deliberately the mathematical floor and NOT a profitability
        filter -- a setup at 55 bps clears it by a hair. Raising it is a
        research decision: set TEMPEST_MIN_RISK_BPS (e.g. 150) to demand
        real margin once you have win-rate evidence to justify the level.
        """
        import os
        default = 0.5 * CostModel().round_trip_bps()
        try:
            return max(0.0, float(os.getenv("TEMPEST_MIN_RISK_BPS", str(default))))
        except ValueError:
            return default

    def _min_risk_per_share(self, price: float) -> float:
        return price * self._min_risk_bps() / 10000.0

    def _entry_qty(self, price: float, stop_price: float) -> int:
        risk_per_share = price - stop_price
        if price <= 0 or risk_per_share <= 0:
            return 0
        notional_qty = int(self.limits.max_notional_per_position // price)
        risk_qty = int(self.limits.max_risk_per_position // risk_per_share)
        return max(0, min(notional_qty, risk_qty))

    def _try_entry(self, symbol: str, open_positions: pd.DataFrame, dry_run: bool) -> dict:
        bars = self._candidate_bars(symbol)
        sig = self._fresh_signal(bars, symbol)
        if sig is None:
            return {"symbol": symbol, "action": "watching",
                    "reason": self.no_signal_reason}
        age = getattr(sig, "age_bars", 0)
        # Re-price a stale signal at the latest bar. The backtest fills on the
        # breakout bar; a poll that arrives N bars later must not pretend it
        # got that price. Enter at the current price or skip.
        fill = float(getattr(sig, "last_price", sig.entry_price))
        if fill <= sig.stop_price:
            return {"symbol": symbol, "action": "watching",
                    "reason": f"signal {age}b old, price {fill:.4f} below stop "
                              f"{sig.stop_price:.4f} - setup broken"}
        slip = (fill - sig.entry_price) / max(sig.entry_price, 1e-9)
        if slip > self._max_entry_slippage():
            return {"symbol": symbol, "action": "watching",
                    "reason": f"signal {age}b old, price ran {slip:.2%} past "
                              f"entry - chasing"}
        risk = fill - sig.stop_price
        if risk <= 0:
            return {"symbol": symbol, "action": "blocked", "reason": "non-positive risk"}
        # Economic rail: a stop inside the round-trip cost cannot win even
        # when the 2R target is hit. Blocked, not "watching" -- the setup is
        # structurally untradeable rather than merely not-yet-triggered.
        min_risk = self._min_risk_per_share(fill)
        if risk < min_risk:
            return {
                "symbol": symbol, "action": "blocked",
                "reason": (
                    f"risk {risk:.4f}/sh ({10000 * risk / fill:.0f}bps) below "
                    f"cost floor {min_risk:.4f} ({self._min_risk_bps():.0f}bps) "
                    f"- stop is inside the spread"
                ),
            }
        qty = self._entry_qty(fill, sig.stop_price)
        if qty < 1:
            return {"symbol": symbol, "action": "blocked",
                    "reason": "qty < 1 at notional/risk limits"}
        ok, reasons = check_entry_ok(
            symbol, qty, fill, self.limits, open_positions,
            account_day_pnl=self.account_day_pnl,
        )
        if not ok:
            return {"symbol": symbol, "action": "blocked", "reason": "; ".join(reasons)}
        target = fill + 2.0 * risk
        cid = f"{self.order_prefix}-{symbol.upper()}-{uuid.uuid4().hex[:8]}"
        row = {
            "timestamp_utc": self._now().isoformat(),
            "strategy_id": self.strategy_id,
            "symbol": symbol.upper(), "action": "order_submitted", "side": "buy",
            "qty": qty, "price": round(fill, 4),
            "order_id": cid, "status": "dry_run" if dry_run else "submitted",
            "setup": self.setup_name, "session": str(sig.session),
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
            order = self.broker.submit_bracket(
                self.client, symbol, qty, "buy", fill,
                sig.stop_price, target, cid,
            )
            broker_status = str(
                getattr(getattr(order, "status", None), "value", None)
                or getattr(order, "status", "submitted")
            ).lower()
            append_journal({**row, "status": broker_status})
            return {"symbol": symbol.upper(), "action": "submitted",
                    "qty": qty, "price": round(fill, 4), "age_bars": age,
                    "stop": round(sig.stop_price, 4), "target": round(target, 4)}
        except Exception as e:  # noqa: BLE001 - journal the failure
            append_journal({
                **row, "action": "entry_rejected", "status": "rejected", "reason": str(e),
            })
            return {"symbol": symbol.upper(), "action": "rejected", "reason": str(e)}

    # -- broker lifecycle reconciliation ----------------------------------
    def _reconcile_entry_orders(
        self, broker_pending: set[str], dry_run: bool,
    ) -> list[dict]:
        """Reconcile cumulative parent-order fills without losing remainders."""
        submitted = pending_journal_orders()
        if not submitted:
            return []
        if dry_run:
            return [
                {"symbol": symbol, "action": "would_check_order_status"}
                for symbol in submitted
            ]
        if not hasattr(self.broker, "get_order_status"):
            raise RuntimeError("broker does not expose parent-order status")

        out = []
        terminal_actions = {
            "canceled": "entry_cancelled",
            "cancelled": "entry_cancelled",
            "expired": "entry_expired",
            "done_for_day": "entry_expired",
            "rejected": "entry_rejected",
        }
        active = {
            "new", "accepted", "pending_new", "partially_filled",
            "pending_cancel", "pending_replace", "pending_review",
            "accepted_for_bidding", "stopped", "suspended", "held",
        }
        self.client = self.client or self.broker.get_trading_client()
        client = self.client
        for sym, order_row in submitted.items():
            state = self.broker.get_order_status(
                client, str(order_row.get("order_id") or "")
            )
            if not isinstance(state, dict):
                raise RuntimeError(f"broker returned malformed order state for {sym}")
            status = str(state.get("status") or "").lower()
            broker_qty = float(state.get("filled_qty") or 0)
            broker_price = state.get("filled_avg_price")
            if broker_price is not None:
                broker_price = float(broker_price)
            if (
                not status
                or not math.isfinite(broker_qty)
                or broker_qty < 0
                or (
                    broker_price is not None
                    and (not math.isfinite(broker_price) or broker_price <= 0)
                )
                or (broker_qty > 0 and broker_price is None)
            ):
                raise RuntimeError(f"broker returned malformed order state for {sym}")
            # Parent-order state is cumulative and authoritative. A position
            # snapshot cannot prove how much this specific parent filled.
            filled_qty = broker_qty
            filled_price = float(
                broker_price
                or order_row.get("entry_price")
                or order_row.get("price")
                or 0
            )
            base = {
                **dict(order_row),
                "timestamp_utc": self._now().isoformat(),
                "qty": filled_qty,
                "price": round(filled_price, 4),
                "entry_price": round(filled_price, 4),
            }

            if status == "filled":
                if filled_qty <= 0 or filled_price <= 0:
                    raise RuntimeError(f"filled parent order for {sym} lacks fill details")
                append_journal({**base, "action": "entry", "status": "filled"})
                out.append({
                    "symbol": sym, "action": "entry_filled",
                    "qty": filled_qty, "price": round(filled_price, 4),
                })
                continue

            if status in active:
                if filled_qty > 0:
                    same_partial = (
                        str(order_row.get("action") or "") == "entry_partial"
                        and float(order_row.get("qty") or 0) == filled_qty
                        and abs(float(order_row.get("entry_price") or 0) - filled_price) < 1e-9
                    )
                    if not same_partial:
                        append_journal({
                            **base, "action": "entry_partial",
                            "status": "partially_filled",
                        })
                        out.append({
                            "symbol": sym, "action": "entry_partially_filled",
                            "qty": filled_qty, "price": round(filled_price, 4),
                        })
                continue

            if status in terminal_actions:
                action = terminal_actions[status]
                if filled_qty > 0:
                    append_journal({
                        **base, "action": "entry", "status": "partially_filled",
                    })
                    out.append({
                        "symbol": sym, "action": "entry_partial_final",
                        "qty": filled_qty, "price": round(filled_price, 4),
                    })
                append_journal({
                    **base, "action": action, "status": status,
                    "reason": f"broker parent order {status}",
                })
                record_cooldown(sym)
                out.append({"symbol": sym, "action": action})
                continue

            raise RuntimeError(
                f"ambiguous broker order state for {sym}: status={status!r}, "
                f"filled_qty={filled_qty}, broker_pending={sym in broker_pending}"
            )
        return out

    def _reconcile_exit_orders(
        self, open_positions: pd.DataFrame, dry_run: bool,
    ) -> list[dict]:
        """Resolve cancelled/rejected close requests without claiming fills."""
        submitted = pending_exit_orders()
        if not submitted or dry_run:
            return []
        live = set()
        if open_positions is not None and not open_positions.empty:
            live = set(open_positions["symbol"].astype(str).str.upper())
        out = []
        terminal_actions = {
            "canceled": "exit_cancelled",
            "cancelled": "exit_cancelled",
            "expired": "exit_expired",
            "done_for_day": "exit_expired",
            "rejected": "exit_rejected",
        }
        for sym, row in submitted.items():
            if not hasattr(self.broker, "get_order_status"):
                raise RuntimeError(f"cannot reconcile submitted exit for {sym}: status API missing")
            state = self.broker.get_order_status(
                self.client or self.broker.get_trading_client(),
                str(row.get("order_id") or ""),
            )
            status = str(state.get("status") or "").lower()
            if status not in terminal_actions:
                continue
            action = terminal_actions[status]
            append_journal({
                **dict(row), "timestamp_utc": self._now().isoformat(),
                "action": action, "status": status,
                "reason": f"broker close order {status}",
            })
            out.append({"symbol": sym, "action": action, "still_open": sym in live})
        return out

    # -- broker-side closes -----------------------------------------------
    def _reconcile_broker_closes(self, open_positions: pd.DataFrame, dry_run: bool) -> list[dict]:
        """Journal stops/TPs the broker filled while we were not looking.

        Without this, a name that hits the pullback-low stop never appears
        in the journal, daily-loss is understated, and attribute_pnl is empty.
        """
        journaled = open_journal_entries()
        if not journaled:
            return []
        live = set()
        if open_positions is not None and not open_positions.empty:
            live = set(open_positions["symbol"].astype(str).str.upper())
        out = []
        for sym, entry in journaled.items():
            if sym in live:
                continue
            fill = None
            entry_time = pd.to_datetime(
                entry.get("timestamp_utc"), utc=True, errors="coerce"
            )
            if (
                not dry_run
                and pd.notna(entry_time)
                and hasattr(self.broker, "last_closed_fill")
            ):
                try:
                    self.client = self.client or self.broker.get_trading_client()
                    fill = self.broker.last_closed_fill(
                        self.client, sym, after_utc=entry_time,
                    )
                except Exception:
                    fill = None
            try:
                qty = float(entry.get("qty") or 0)
                avg = float(entry.get("entry_price") or entry.get("price") or 0)
                stop = float(entry.get("stop_price") or 0)
            except (TypeError, ValueError):
                continue
            fill_time = (
                pd.to_datetime(fill.get("filled_at"), utc=True, errors="coerce")
                if fill else pd.NaT
            )
            if fill and pd.notna(fill_time):
                reason = str(fill.get("reason") or "broker_closed")
                px = float(fill.get("price") or stop or avg)
                if fill.get("qty"):
                    qty = float(fill["qty"]) or qty
            else:
                # Do not invent a stop fill. Keep the confirmed journal entry
                # unresolved and block re-entry until broker history is readable.
                out.append({
                    "symbol": sym, "action": "close_unresolved",
                    "reason": "position absent but no authoritative closing fill",
                })
                continue
            action = reason if reason in ("stop_filled", "tp_filled") else "broker_closed"
            pnl = (px - avg) * qty
            if dry_run:
                out.append({"symbol": sym, "action": "would_reconcile",
                            "reason": reason, "pnl": round(pnl, 2)})
                continue
            append_journal({
                "timestamp_utc": fill_time.isoformat(),
                "strategy_id": entry.get("strategy_id") or "legacy_unknown",
                "symbol": sym, "action": action, "side": "sell",
                "qty": qty, "price": round(px, 4),
                "status": "filled", "session": str(fill_time.date()),
                "entry_price": round(avg, 4), "exit_price": round(px, 4),
                "stop_price": entry.get("stop_price"),
                "target_price": entry.get("target_price"),
                "pnl": round(pnl, 2), "reason": reason,
            })
            record_cooldown(sym)
            out.append({"symbol": sym, "action": action, "reason": reason,
                        "pnl": round(pnl, 2)})
        return out

    # -- exits ------------------------------------------------------------
    def _manage_exits(
        self, open_positions: pd.DataFrame, dry_run: bool,
        pending_entries: set[str] | None = None,
    ) -> list[dict]:
        if open_positions.empty:
            return []
        journal = load_journal()
        unresolved_exits = set(pending_exit_orders())
        out = []
        now = self._clock_now().to_pydatetime()
        mins_to_close = self._clock()["minutes_to_close"]
        for _, pos in open_positions.iterrows():
            sym = str(pos["symbol"]).upper()
            if sym in (pending_entries or set()):
                out.append({"symbol": sym, "action": "entry_pending"})
                continue
            if sym in unresolved_exits:
                out.append({"symbol": sym, "action": "exit_pending"})
                continue
            entries = journal[
                (journal["symbol"].astype(str).str.upper() == sym)
                & (journal["action"] == "entry")
            ]
            entry_ts = None
            entry_strategy = "legacy_unknown"
            entry_setup = None
            if not entries.empty:
                latest_entry = entries.iloc[-1]
                entry_strategy = str(latest_entry.get("strategy_id") or "legacy_unknown")
                entry_setup = latest_entry.get("setup")
                try:
                    entry_ts = pd.to_datetime(latest_entry["timestamp_utc"], utc=True)
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
                order = self.broker.close_position(self.client, sym)
            except Exception as e:  # noqa: BLE001
                out.append({"symbol": sym, "action": "close_failed", "reason": str(e)})
                continue
            order_id = str(
                getattr(order, "client_order_id", None)
                or getattr(order, "id", None)
                or f"close-{sym}-{uuid.uuid4().hex[:8]}"
            )
            order_status = str(
                getattr(getattr(order, "status", None), "value", None)
                or getattr(order, "status", "submitted")
            ).lower()
            append_journal({
                "timestamp_utc": now.isoformat(), "strategy_id": entry_strategy,
                "symbol": sym, "action": "exit_submitted", "side": "sell", "qty": qty,
                "setup": entry_setup,
                "price": round(exit_price, 4), "order_id": order_id,
                "status": order_status, "session": str(now.date()),
                "entry_price": round(avg, 4), "reason": reason,
            })
            out.append({"symbol": sym, "action": "exit_submitted", "reason": reason})
        return out

    def _entry_window_open(self) -> tuple[bool, str]:
        clock = self._clock()
        if not clock["is_open"]:
            return False, "broker reports market closed"
        now_et = self._clock_now().tz_convert("America/New_York")
        minute = now_et.hour * 60 + now_et.minute
        if minute < 9 * 60 + 30:
            return False, "before 09:30 ET"
        if clock["minutes_to_close"] <= self.limits.close_before_market_close_minutes:
            return False, f"inside final {self.limits.close_before_market_close_minutes}m"
        return True, ""

    # -- main loop ---------------------------------------------------------
    def run_once(self, candidates: list[str], dry_run: bool = False) -> dict:
        self.client = None
        self.account_day_pnl = None
        open_positions = self.broker.get_open_positions()
        broker_open_count = len(open_positions) if open_positions is not None else 0
        if not hasattr(self.broker, "get_open_order_symbols"):
            raise RuntimeError("broker does not expose open-order state")
        broker_pending = set(self.broker.get_open_order_symbols() or [])

        order_events = self._reconcile_entry_orders(broker_pending, dry_run)
        exit_order_events = self._reconcile_exit_orders(open_positions, dry_run)
        reconciled = self._reconcile_broker_closes(open_positions, dry_run)
        self.clock_info = _authoritative_clock(self.broker)
        pending = set(broker_pending)
        pending.update(pending_journal_orders())
        exits = self._manage_exits(
            open_positions, dry_run, pending_entries=pending,
        )

        pending = set(broker_pending)
        pending.update(pending_journal_orders())
        pending.update(open_journal_entries())
        risk_positions = open_positions.copy() if open_positions is not None else pd.DataFrame()
        live_symbols = set()
        if not risk_positions.empty:
            live_symbols = set(risk_positions["symbol"].astype(str).str.upper())
        resting_only = sorted(pending - live_symbols)
        if resting_only:
            placeholders = pd.DataFrame([{
                "symbol": symbol, "qty": 0, "avg_entry_price": 0,
                "current_price": 0, "market_value": 0,
            } for symbol in resting_only])
            risk_positions = (
                placeholders if risk_positions.empty
                else pd.concat([risk_positions, placeholders], ignore_index=True)
            )
        entries = []
        entry_window_open, window_reason = self._entry_window_open()
        if candidates and entry_window_open:
            if not hasattr(self.broker, "get_account_day_pnl"):
                raise RuntimeError("broker does not expose authoritative account P&L")
            try:
                self.account_day_pnl = float(self.broker.get_account_day_pnl())
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("broker returned invalid account P&L") from exc
            if not math.isfinite(self.account_day_pnl):
                raise RuntimeError("broker returned invalid account P&L")
        for sym in candidates:
            up = str(sym).upper()
            if up in pending:
                entries.append({"symbol": up, "action": "blocked",
                                "reason": "working/unresolved order already exists"})
                continue
            if not entry_window_open:
                entries.append({"symbol": up, "action": "blocked",
                                "reason": f"entry window closed: {window_reason}"})
                continue
            result = self._try_entry(sym, risk_positions, dry_run)
            entries.append(result)
            if result.get("action") in ("submitted", "would_enter"):
                # Count accepted/resting entries toward the same-pass cap.
                pending.add(up)
                extra = pd.DataFrame([{
                    "symbol": up, "qty": result.get("qty", 0),
                    "avg_entry_price": result.get("price", 0),
                    "current_price": result.get("price", 0),
                    "market_value": 0.0,
                }])
                risk_positions = (
                    extra if risk_positions.empty
                    else pd.concat([risk_positions, extra], ignore_index=True)
                )
        return {
            "open_positions": broker_open_count,
            "exposure_slots": len(risk_positions),
            "exits": (
                list(order_events) + list(exit_order_events)
                + list(reconciled) + list(exits)
            ),
            "entries": entries,
        }
