"""Fail-closed paper lifecycle and time/risk safety regressions."""

import pandas as pd
import pytest

from tempest import broker, risk
from tempest.risk import RiskLimits, append_journal
from tempest.trader import PaperTrader
from tests.test_paper_trade import FakeBroker, FakeSource, _ending_at_crossing, _frame_now


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    monkeypatch.setattr(risk, "HALT_FLAG_PATH", tmp_path / "halt.flag")


def _submitted(order_id="tempest-YXT-abc"):
    append_journal({
        "timestamp_utc": "2026-08-03T13:37:00+00:00",
        "symbol": "YXT", "action": "order_submitted", "side": "buy",
        "qty": 50, "price": 10.5, "entry_price": 10.5,
        "stop_price": 10.2, "target_price": 11.1,
        "order_id": order_id, "status": "accepted",
    })


def test_broker_position_and_order_state_errors_raise():
    class BrokenClient:
        def get_all_positions(self):
            raise RuntimeError("positions unavailable")

        def get_orders(self):
            raise RuntimeError("orders unavailable")

        def close_position(self, symbol):
            raise RuntimeError("close unavailable")

    client = BrokenClient()
    with pytest.raises(RuntimeError, match="positions unavailable"):
        broker.get_open_positions(client)
    with pytest.raises(RuntimeError, match="orders unavailable"):
        broker.get_open_order_symbols(client)
    with pytest.raises(RuntimeError, match="close unavailable"):
        broker.close_position(client, "YXT")


def test_resting_entry_is_not_invented_as_stop_fill():
    _submitted()
    fake = FakeBroker()
    fake.pending.add("YXT")
    fake.order_states["tempest-YXT-abc"] = {
        "status": "accepted", "filled_qty": 0, "filled_avg_price": None,
    }
    trader = PaperTrader(fake, FakeSource({}), now_fn=_frame_now)

    result = trader.run_once([], dry_run=False)

    assert not any(e.get("action") == "stop_filled" for e in result["exits"])
    journal = risk.load_journal()
    assert list(journal["action"]) == ["order_submitted"]


def test_position_promotes_submitted_order_to_confirmed_fill():
    _submitted()
    fake = FakeBroker()
    fake.positions = pd.DataFrame([{
        "symbol": "YXT", "qty": 40, "avg_entry_price": 10.48,
        "current_price": 10.52, "market_value": 420.8,
    }])
    fake.pending.add("YXT")
    fake.order_states["tempest-YXT-abc"] = {
        "status": "filled", "filled_qty": 40, "filled_avg_price": 10.48,
    }
    trader = PaperTrader(fake, FakeSource({}), now_fn=_frame_now)

    result = trader.run_once([], dry_run=False)

    assert any(e.get("action") == "entry_filled" for e in result["exits"])
    journal = risk.load_journal()
    filled = journal[journal["action"] == "entry"].iloc[-1]
    assert filled["status"] == "filled"
    assert float(filled["qty"]) == 40
    assert float(filled["entry_price"]) == pytest.approx(10.48)


def test_cancelled_parent_order_is_not_scored_as_trade():
    order_id = "tempest-YXT-cancel"
    _submitted(order_id)
    fake = FakeBroker()
    fake.order_states[order_id] = {
        "status": "canceled", "filled_qty": 0, "filled_avg_price": None,
    }
    trader = PaperTrader(fake, FakeSource({}), now_fn=_frame_now)

    result = trader.run_once([], dry_run=False)

    assert any(e.get("action") == "entry_cancelled" for e in result["exits"])
    journal = risk.load_journal()
    assert (journal["action"] == "entry_cancelled").any()
    assert not (journal["action"] == "entry").any()


def test_entry_sizing_respects_stop_risk_and_notional():
    trader = PaperTrader(FakeBroker(), FakeSource({}), limits=RiskLimits(
        max_notional_per_position=1000.0,
        max_risk_per_position=50.0,
    ), now_fn=_frame_now)

    assert trader._entry_qty(10.0, 9.0) == 50
    assert trader._entry_qty(10.0, 9.9) == 100
    assert trader._entry_qty(10.0, 10.0) == 0


def test_near_close_blocks_new_entry():
    def now():
        return pd.Timestamp("2026-08-03 19:35:00+00:00").to_pydatetime()

    broker_ = FakeBroker()
    broker_.clock_info.update({
        "timestamp_utc": pd.Timestamp("2026-08-03T19:35:00+00:00"),
        "minutes_to_close": 25,
    })
    trader = PaperTrader(
        broker_, FakeSource({"YXT": _ending_at_crossing()}), now_fn=now,
    )

    result = trader.run_once(["YXT"], dry_run=False)

    assert result["entries"][0]["action"] == "blocked"
    assert "final 30m" in result["entries"][0]["reason"]


def test_prior_session_signal_is_not_actionable_today():
    def next_day():
        return pd.Timestamp("2026-08-04 13:38:00+00:00").to_pydatetime()

    broker_ = FakeBroker()
    broker_.clock_info["timestamp_utc"] = pd.Timestamp("2026-08-04T13:38:00+00:00")
    trader = PaperTrader(
        broker_, FakeSource({"YXT": _ending_at_crossing()}), now_fn=next_day,
    )

    result = trader.run_once(["YXT"], dry_run=False)

    assert result["entries"][0]["action"] == "watching"
    assert "no fresh" in result["entries"][0]["reason"]


def test_broker_closed_fill_counts_in_realized_pnl():
    from scripts.attribute_pnl import attribute

    append_journal({
        "timestamp_utc": "2026-08-03T14:00:00+00:00", "symbol": "YXT",
        "action": "entry", "status": "filled", "qty": 10, "price": 10.0,
    })
    append_journal({
        "timestamp_utc": "2026-08-03T14:15:00+00:00", "symbol": "YXT",
        "action": "broker_closed", "status": "filled", "qty": 10, "price": 10.5,
        "pnl": 5.0,
    })

    result = attribute(risk.load_journal())

    assert result["YXT"]["realized_pnl"] == 5.0
    assert result["YXT"]["closed_trades"] == 1


def test_wall_clock_stale_bar_is_not_actionable():
    def late():
        return pd.Timestamp("2026-08-03 14:30:00+00:00").to_pydatetime()

    broker_ = FakeBroker()
    broker_.clock_info["timestamp_utc"] = pd.Timestamp("2026-08-03T14:30:00+00:00")
    trader = PaperTrader(
        broker_, FakeSource({"YXT": _ending_at_crossing()}), now_fn=late,
    )

    result = trader.run_once(["YXT"], dry_run=False)

    assert result["entries"][0]["action"] == "watching"
    assert "no fresh" in result["entries"][0]["reason"]
