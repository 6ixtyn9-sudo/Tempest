"""Alpaca PAPER broker wrapper: orders, positions, closes.

All order paths are paper-only (get_trading_client asserts TEMPEST_PAPER=1
and constructs with paper=True). Never touches a live account.
"""

import math

import pandas as pd

from tempest.sources.alpaca import get_trading_client


def get_account_equity(client=None) -> float:
    client = client or get_trading_client()
    return float(client.get_account().equity)


def get_account_day_pnl(client=None) -> float:
    """Broker-authoritative account P&L since the prior trading-day close."""
    client = client or get_trading_client()
    account = client.get_account()
    return float(account.equity) - float(account.last_equity)


def get_clock_info(client=None) -> dict:
    """Return Alpaca's authoritative market clock.

    Errors propagate: unknown market state must stop the paper pass rather than
    fall back to a workstation clock.
    """
    client = client or get_trading_client()
    clock = client.get_clock()
    timestamp = pd.Timestamp(clock.timestamp)
    next_close = pd.Timestamp(clock.next_close)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    if next_close.tzinfo is None:
        next_close = next_close.tz_localize("UTC")
    else:
        next_close = next_close.tz_convert("UTC")
    return {
        "is_open": clock.is_open,
        "timestamp_utc": timestamp,
        "next_close_utc": next_close,
        "minutes_to_close": int((next_close - timestamp).total_seconds() // 60),
    }


def get_open_order_symbols(client=None) -> set[str]:
    """Symbols with a working (not filled/cancelled) order.

    Broker-state failures deliberately raise. Returning an empty set on an API
    outage would let a later poll submit duplicate brackets.
    """
    client = client or get_trading_client()
    orders = client.get_orders()
    out = set()
    done = {
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "replaced", "calculated",
    }
    for o in orders or []:
        try:
            raw_status = getattr(o, "status", "")
            status = str(getattr(raw_status, "value", raw_status) or "").lower()
            symbol = str(o.symbol).upper().strip()
            if not status or not symbol:
                raise ValueError("missing status or symbol")
            if status in done:
                continue
            out.add(symbol)
        except (TypeError, ValueError, AttributeError) as exc:
            raise RuntimeError(f"broker returned malformed open-order state: {exc}") from exc
    return out


def get_open_positions(client=None) -> pd.DataFrame:
    """Return current positions, raising when broker state is unavailable."""
    client = client or get_trading_client()
    positions = client.get_all_positions()
    if not positions:
        return pd.DataFrame()
    rows = []
    for p in positions:
        try:
            row = {
                "symbol": str(p.symbol).upper().strip(),
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
            }
            numeric = [
                row["qty"], row["avg_entry_price"],
                row["current_price"], row["market_value"],
            ]
            if (
                not row["symbol"]
                or not all(math.isfinite(value) for value in numeric)
                or row["qty"] == 0
                or row["avg_entry_price"] <= 0
                or row["current_price"] <= 0
            ):
                raise ValueError("missing or invalid position field")
            rows.append(row)
        except (TypeError, ValueError, AttributeError) as exc:
            raise RuntimeError(f"broker returned malformed position state: {exc}") from exc
    return pd.DataFrame(rows)


def submit_bracket(
    client,
    symbol: str,
    qty: int,
    side: str,
    entry_limit: float,
    stop_price: float,
    take_profit_price: float,
    client_order_id: str,
):
    """Submit a DAY bracket: entry limit + take-profit + stop-loss.

    The bracket enforces the strategy's risk shape in one order: entry at
    the crossing candle, 2R take-profit, stop at the pullback low. DAY TIF
    means unfilled legs expire at the close (same-day strategy).
    """
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    def _round(px):
        return round(float(px), 2 if px >= 1.0 else 4)

    # alpaca-py 0.43.x: brackets are LimitOrderRequest with order_class
    # BRACKET plus take_profit/stop_loss legs (no BracketOrderRequest type).
    return client.submit_order(LimitOrderRequest(
        symbol=str(symbol).upper(),
        qty=qty,
        side=OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        limit_price=_round(entry_limit),
        take_profit=TakeProfitRequest(limit_price=_round(take_profit_price)),
        stop_loss=StopLossRequest(stop_price=_round(stop_price)),
        client_order_id=client_order_id,
    ))


def last_closed_fill(client, symbol: str, after_utc=None) -> dict | None:
    """Latest provable filled exit on `symbol`: {price, reason, qty, filled_at}.

    Used to journal a broker-side stop/TP that this process did not close.
    Returns None if the order history cannot be read.
    """
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[str(symbol).upper()],
            limit=30,
        ))
    except Exception:
        try:
            orders = client.get_orders()
        except Exception:
            return None
    best = None
    best_ts = None
    for o in orders or []:
        try:
            if str(getattr(o, "symbol", "")).upper() != str(symbol).upper():
                continue
            raw_status = getattr(o, "status", "")
            status = str(getattr(raw_status, "value", raw_status) or "").lower()
            if status != "filled":
                continue
            raw_side = getattr(o, "side", "")
            side = str(getattr(raw_side, "value", raw_side) or "").lower()
            if side != "sell":
                continue
            px = getattr(o, "filled_avg_price", None) or getattr(o, "limit_price", None)
            if px is None:
                continue
            otype = str(getattr(o, "order_type", getattr(o, "type", "")) or "").lower()
            if "stop" in otype:
                reason = "stop_filled"
            elif "limit" in otype:
                reason = "tp_filled"
            else:
                reason = "broker_closed"
            ts = getattr(o, "filled_at", None) or getattr(o, "updated_at", None)
            if ts is None:
                continue  # without time, this cannot be tied to the current entry
            fill_ts = pd.Timestamp(ts)
            if fill_ts.tzinfo is None:
                fill_ts = fill_ts.tz_localize("UTC")
            else:
                fill_ts = fill_ts.tz_convert("UTC")
            if after_utc is not None and fill_ts <= pd.Timestamp(after_utc):
                continue
            if best_ts is not None and fill_ts < best_ts:
                continue
            best_ts = fill_ts
            qty = getattr(o, "filled_qty", None) or getattr(o, "qty", None)
            best = {
                "price": float(px), "reason": reason, "qty": float(qty or 0),
                "filled_at": fill_ts.isoformat(),
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return best


def get_order_status(client, client_order_id: str) -> dict:
    """Return the broker's authoritative state for a submitted parent order.

    ``client_order_id`` is the Tempest-generated id persisted in the journal.
    Missing/failed lookups raise so reconciliation never invents a fill.
    """
    reference = str(client_order_id).strip()
    if not reference:
        raise RuntimeError("cannot query an order without an identifier")
    try:
        order = client.get_order_by_client_id(reference)
    except Exception:
        order = client.get_order_by_id(reference)
    if order is None:
        raise RuntimeError(f"broker returned no state for order {reference}")

    def _value(value):
        return getattr(value, "value", value)

    status = str(_value(getattr(order, "status", "")) or "").lower()
    filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    raw_price = getattr(order, "filled_avg_price", None)
    filled_price = float(raw_price) if raw_price is not None else None
    if (
        not status
        or not math.isfinite(filled_qty)
        or filled_qty < 0
        or (
            filled_price is not None
            and (not math.isfinite(filled_price) or filled_price <= 0)
        )
        or (filled_qty > 0 and filled_price is None)
    ):
        raise RuntimeError(f"broker returned malformed state for order {reference}")
    return {
        "id": str(getattr(order, "id", "") or ""),
        "client_order_id": str(getattr(order, "client_order_id", "") or reference),
        "status": status,
        "filled_avg_price": filled_price,
        "filled_qty": filled_qty,
    }


def close_position(client, symbol: str):
    """Submit a close and propagate broker failures to the caller."""
    return client.close_position(str(symbol).upper())
