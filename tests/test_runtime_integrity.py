"""Runtime integrity contracts for broker time, completed bars and fills."""


import pandas as pd
import pytest

from tempest import risk
from tempest.gale_trader import GalePaperTrader
from tempest.risk import RiskLimits, append_journal, check_entry_ok, load_journal
from tempest.trader import PaperTrader
from tests.test_gale import gale_frame
from tests.test_gale_paper import screen_evidence
from tests.test_paper_trade import FakeBroker, FakeSource, _ending_at_crossing


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    monkeypatch.setattr(risk, "HALT_FLAG_PATH", tmp_path / "halt.flag")


def _clock(broker, timestamp, minutes=300, is_open=True):
    broker.clock_info = {
        "is_open": is_open,
        "timestamp_utc": pd.Timestamp(timestamp),
        "minutes_to_close": minutes,
    }
    return broker


def test_clock_failure_blocks_tempest_and_gale():
    class BrokenClockBroker(FakeBroker):
        def get_clock_info(self):
            raise RuntimeError("clock unavailable")

    tempest = PaperTrader(
        BrokenClockBroker(), FakeSource({"YXT": _ending_at_crossing()})
    )
    with pytest.raises(RuntimeError, match="clock unavailable"):
        tempest.run_once(["YXT"], dry_run=False)

    gale = GalePaperTrader(
        BrokenClockBroker(),
        FakeSource({"GALE": gale_frame().iloc[:6]}),
        screen_evidence=screen_evidence(),
    )
    with pytest.raises(RuntimeError, match="clock unavailable"):
        gale.run_once(["GALE"], dry_run=False)


def test_market_closed_blocks_both_strategies():
    tempest_broker = _clock(
        FakeBroker(), "2026-08-03T13:38:00+00:00", is_open=False
    )
    tempest = PaperTrader(
        tempest_broker, FakeSource({"YXT": _ending_at_crossing()})
    )
    result = tempest.run_once(["YXT"], dry_run=False)
    assert result["entries"][0]["action"] == "blocked"

    gale_broker = _clock(
        FakeBroker(), "2026-08-03T13:38:00+00:00", is_open=False
    )
    gale = GalePaperTrader(
        gale_broker,
        FakeSource({"GALE": gale_frame().iloc[:6]}),
        screen_evidence=screen_evidence(),
    )
    result2 = gale.run_once(["GALE"], dry_run=False)
    assert result2["entries"][0]["action"] == "blocked"


def test_incomplete_latest_bar_is_excluded_for_both_strategies():
    tempest_broker = _clock(FakeBroker(), "2026-08-03T13:37:30+00:00")
    tempest = PaperTrader(
        tempest_broker, FakeSource({"YXT": _ending_at_crossing()})
    )
    result = tempest.run_once(["YXT"], dry_run=False)
    assert result["entries"][0]["action"] == "watching"
    assert tempest_broker.orders == []

    gale_broker = _clock(FakeBroker(), "2026-08-03T13:35:30+00:00")
    gale = GalePaperTrader(
        gale_broker,
        FakeSource({"GALE": gale_frame().iloc[:6]}),
        screen_evidence=screen_evidence(),
    )
    result2 = gale.run_once(["GALE"], dry_run=False)
    assert result2["entries"][0]["action"] == "watching"
    assert gale_broker.orders == []


def test_unrealized_losses_contribute_to_daily_loss_gate():
    positions = pd.DataFrame([{
        "symbol": "AAA", "qty": 100, "avg_entry_price": 10.0,
        "current_price": 7.0, "market_value": 700.0,
    }])
    ok, reasons = check_entry_ok(
        "BBB", 10, 10.0, RiskLimits(max_daily_loss=200), positions
    )
    assert ok is False
    assert any("daily loss cap" in reason for reason in reasons)


def _submitted(order_id="tempest-YXT-partial"):
    append_journal({
        "timestamp_utc": "2026-08-03T13:37:00+00:00",
        "strategy_id": "tempest_first_pullback",
        "symbol": "YXT", "action": "order_submitted", "side": "buy",
        "qty": 50, "price": 10.5, "entry_price": 10.5,
        "stop_price": 10.2, "target_price": 11.1,
        "order_id": order_id, "status": "accepted",
    })


def test_partial_fill_remains_pending_and_is_idempotent():
    order_id = "tempest-YXT-partial"
    _submitted(order_id)
    broker = FakeBroker()
    broker.order_states[order_id] = {
        "status": "partially_filled", "filled_qty": 20,
        "filled_avg_price": 10.45,
    }
    trader = PaperTrader(broker, FakeSource({}))

    first = trader.run_once([], dry_run=False)
    second = trader.run_once([], dry_run=False)

    assert any(e["action"] == "entry_partially_filled" for e in first["exits"])
    assert not any(e["action"] == "entry_partially_filled" for e in second["exits"])
    journal = load_journal()
    partials = journal[journal["action"] == "entry_partial"]
    assert len(partials) == 1
    assert float(partials.iloc[0]["qty"]) == 20
    assert "YXT" in risk.pending_journal_orders(journal)


def test_cancelled_remainder_preserves_partial_position():
    order_id = "tempest-YXT-partial"
    _submitted(order_id)
    broker = FakeBroker()
    broker.order_states[order_id] = {
        "status": "partially_filled", "filled_qty": 20,
        "filled_avg_price": 10.45,
    }
    trader = PaperTrader(broker, FakeSource({}))
    trader.run_once([], dry_run=False)

    broker.order_states[order_id] = {
        "status": "canceled", "filled_qty": 20,
        "filled_avg_price": 10.45,
    }
    result = trader.run_once([], dry_run=False)

    assert any(e["action"] == "entry_partial_final" for e in result["exits"])
    journal = load_journal()
    assert "YXT" not in risk.pending_journal_orders(journal)
    open_entry = risk.open_journal_entries(journal)["YXT"]
    assert float(open_entry["qty"]) == 20
    assert open_entry["status"] == "partially_filled"


def test_partial_then_full_uses_cumulative_quantity_once():
    order_id = "tempest-YXT-partial"
    _submitted(order_id)
    broker = FakeBroker()
    broker.order_states[order_id] = {
        "status": "partially_filled", "filled_qty": 20,
        "filled_avg_price": 10.45,
    }
    trader = PaperTrader(broker, FakeSource({}))
    trader.run_once([], dry_run=False)

    broker.order_states[order_id] = {
        "status": "filled", "filled_qty": 50,
        "filled_avg_price": 10.47,
    }
    trader.run_once([], dry_run=False)

    journal = load_journal()
    open_entry = risk.open_journal_entries(journal)["YXT"]
    assert float(open_entry["qty"]) == 50
    assert float(open_entry["entry_price"]) == pytest.approx(10.47)
    assert "YXT" not in risk.pending_journal_orders(journal)


def test_broker_account_loss_is_authoritative_for_daily_gate():
    limits = RiskLimits(max_daily_loss=200.0, max_open_positions=4)
    ok, reasons = check_entry_ok(
        "NEW", 1, 10.0, limits, pd.DataFrame(), account_day_pnl=-200.01,
    )

    assert not ok
    assert any("daily loss cap" in reason for reason in reasons)


def test_present_but_corrupt_state_fails_closed(tmp_path):
    risk.JOURNAL_PATH.write_text("")
    with pytest.raises(RuntimeError, match="journal is unreadable"):
        load_journal()

    risk.JOURNAL_PATH.write_text("timestamp_utc,symbol,action\n2026-08-03T13:30:00Z,YXT,entry\n")
    with pytest.raises(RuntimeError, match="missing required columns"):
        load_journal()

    risk.JOURNAL_PATH.unlink()
    risk.COOLDOWN_PATH.write_text("not-json")
    with pytest.raises(RuntimeError, match="cooldown state is unreadable"):
        risk.cooldown_remaining("YXT", cooldown_seconds=60)


def test_invalid_broker_account_pnl_blocks_poll():
    broker = FakeBroker()
    broker.account_day_pnl = float("nan")
    trader = PaperTrader(broker, FakeSource({}))

    with pytest.raises(RuntimeError, match="invalid account P&L"):
        trader.run_once(["YXT"], dry_run=False)


@pytest.mark.parametrize(
    "clock_info",
    [
        {"is_open": "false", "timestamp_utc": "2026-08-03T13:38:00Z",
         "minutes_to_close": 300},
        {"is_open": True, "timestamp_utc": None, "minutes_to_close": 300},
        {"is_open": True, "timestamp_utc": "2026-08-03T13:38:00Z",
         "minutes_to_close": float("nan")},
    ],
)
def test_malformed_broker_clock_blocks_poll(clock_info):
    broker = FakeBroker()
    broker.clock_info = clock_info
    trader = PaperTrader(broker, FakeSource({}))

    with pytest.raises(RuntimeError, match="invalid market clock"):
        trader.run_once([], dry_run=False)


def test_broker_account_loss_blocks_strategy_entry():
    broker = FakeBroker()
    broker.account_day_pnl = -250.0
    trader = PaperTrader(broker, FakeSource({"YXT": _ending_at_crossing()}))

    result = trader.run_once(["YXT"], dry_run=False)

    assert result["entries"][0]["action"] == "blocked"
    assert "daily loss cap" in result["entries"][0]["reason"]
    assert broker.orders == []


def test_historical_sell_is_not_reused_for_current_entry():
    append_journal({
        "timestamp_utc": "2026-08-03T14:00:00+00:00",
        "strategy_id": "tempest_first_pullback", "symbol": "YXT",
        "action": "entry", "status": "filled", "qty": 10,
        "price": 10.0, "entry_price": 10.0,
    })
    broker = FakeBroker()
    broker.clock_info["timestamp_utc"] = pd.Timestamp("2026-08-03T14:10:00+00:00")
    broker.fills = {"YXT": {
        "price": 9.8, "reason": "stop_filled", "qty": 10,
        "filled_at": "2026-08-03T13:55:00+00:00",
    }}
    trader = PaperTrader(broker, FakeSource({}))

    result = trader.run_once([], dry_run=False)

    assert any(event["action"] == "close_unresolved" for event in result["exits"])
    assert not (load_journal()["action"] == "stop_filled").any()


def test_horizon_exit_waits_for_partial_parent_to_finish():
    order_id = "tempest-YXT-partial"
    _submitted(order_id)
    broker = FakeBroker()
    broker.pending.add("YXT")
    broker.positions = pd.DataFrame([{
        "symbol": "YXT", "qty": 20, "avg_entry_price": 10.45,
        "current_price": 10.50, "market_value": 210.0,
    }])
    broker.order_states[order_id] = {
        "status": "partially_filled", "filled_qty": 20,
        "filled_avg_price": 10.45,
    }
    trader = PaperTrader(broker, FakeSource({}), limits=RiskLimits(horizon_bars=1))

    result = trader.run_once([], dry_run=False)

    assert any(event["action"] == "entry_pending" for event in result["exits"])
    assert broker.closed == []
