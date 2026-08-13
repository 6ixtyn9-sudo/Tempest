"""Alpaca PAPER broker wrapper: orders, positions, closes.

All order paths are paper-only (get_trading_client asserts TEMPEST_PAPER=1
and constructs with paper=True). Never touches a live account.
"""

import pandas as pd

from tempest.sources.alpaca import get_trading_client


def get_account_equity(client=None) -> float:
    client = client or get_trading_client()
    return float(client.get_account().equity)


def get_open_order_symbols(client=None) -> set[str]:
    """Symbols with a working (not filled/cancelled) order. A 5-minute
    poll must not re-submit a DAY bracket that is already resting."""
    client = client or get_trading_client()
    try:
        orders = client.get_orders()
    except Exception:
        return set()
    out = set()
    done = {"filled", "canceled", "cancelled", "expired", "rejected"}
    for o in orders or []:
        try:
            status = str(getattr(o, "status", "") or "").lower()
            if status in done:
                continue
            out.add(str(o.symbol).upper())
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def get_open_positions(client=None) -> pd.DataFrame:
    client = client or get_trading_client()
    try:
        positions = client.get_all_positions()
    except Exception:
        return pd.DataFrame()
    if not positions:
        return pd.DataFrame()
    rows = []
    for p in positions:
        try:
            rows.append({
                "symbol": str(p.symbol).upper(),
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
            })
        except (TypeError, ValueError):
            continue
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
        LimitOrderRequest, StopLossRequest, TakeProfitRequest,
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


def last_closed_fill(client, symbol: str) -> dict | None:
    """Latest filled exit on `symbol`: {price, reason, qty}.

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
            status = str(getattr(o, "status", "") or "").lower()
            if status != "filled":
                continue
            side = str(getattr(o, "side", "") or "").lower()
            if side not in ("sell", "order_side.sell"):
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
            if best_ts is not None and ts is not None and ts < best_ts:
                continue
            best_ts = ts
            qty = getattr(o, "filled_qty", None) or getattr(o, "qty", None)
            best = {"price": float(px), "reason": reason, "qty": float(qty or 0)}
        except (TypeError, ValueError, AttributeError):
            continue
    return best


def close_position(client, symbol: str) -> dict:
    try:
        return client.close_position(str(symbol).upper())
    except Exception as e:  # noqa: BLE001 - best effort
        return {"error": str(e)}
