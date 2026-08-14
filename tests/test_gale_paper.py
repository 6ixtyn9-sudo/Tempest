"""Gale uses the shared paper broker lifecycle and account-level risk."""

from datetime import datetime, timezone

import pandas as pd
import pytest

from tempest import risk
from tempest.gale_trader import GalePaperTrader
from tempest.risk import RiskLimits
from tests.test_gale import gale_frame
from tests.test_paper_trade import FakeBroker, FakeSource


def now_936():
    return datetime(2026, 8, 3, 13, 36, tzinfo=timezone.utc)


def screen_evidence(symbol="GALE"):
    return pd.DataFrame([{
        "captured_at_utc": pd.Timestamp("2026-08-03T13:30:30+00:00"),
        "session_date": "2026-08-03",
        "symbol": symbol,
        "tradeable": True,
    }])


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    monkeypatch.setattr(risk, "HALT_FLAG_PATH", tmp_path / "halt.flag")


def test_gale_submits_paper_bracket_with_strategy_identity():
    broker = FakeBroker()
    source = FakeSource({"GALE": gale_frame().iloc[:6]})
    trader = GalePaperTrader(
        broker, source, screen_evidence=screen_evidence(), now_fn=now_936,
    )

    result = trader.run_once(["GALE"], dry_run=False)

    assert result["entries"][0]["action"] == "submitted"
    assert broker.orders[0]["cid"].startswith("gale-GALE-")
    journal = risk.load_journal()
    row = journal.iloc[-1]
    assert row["strategy_id"] == "gale_orb5"
    assert row["setup"] == "orb5"
    assert row["action"] == "order_submitted"


def test_gale_requires_point_in_time_screen_evidence():
    broker = FakeBroker()
    trader = GalePaperTrader(
        broker,
        FakeSource({"GALE": gale_frame().iloc[:6]}),
        screen_evidence=screen_evidence("OTHER"),
        now_fn=now_936,
    )

    result = trader.run_once(["GALE"], dry_run=False)

    assert result["entries"][0]["action"] == "watching"
    assert broker.orders == []


def test_resting_tempest_order_consumes_shared_account_slot():
    broker = FakeBroker()
    broker.pending.add("TEMP")
    trader = GalePaperTrader(
        broker,
        FakeSource({"GALE": gale_frame().iloc[:6]}),
        screen_evidence=screen_evidence(),
        limits=RiskLimits(max_open_positions=1),
        now_fn=now_936,
    )

    result = trader.run_once(["GALE"], dry_run=False)

    assert result["entries"][0]["action"] == "blocked"
    assert "max open positions" in result["entries"][0]["reason"]
    assert broker.orders == []


def test_pnl_attribution_keeps_tempest_and_gale_separate():
    from scripts.attribute_pnl import attribute_by_strategy

    rows = pd.DataFrame([
        {"timestamp_utc": "2026-08-03T14:00:00+00:00", "strategy_id": "tempest_first_pullback",
         "symbol": "AAA", "action": "entry", "qty": 10, "price": 10.0},
        {"timestamp_utc": "2026-08-03T14:10:00+00:00", "strategy_id": "tempest_first_pullback",
         "symbol": "AAA", "action": "broker_closed", "qty": 10, "price": 10.5},
        {"timestamp_utc": "2026-08-03T14:00:00+00:00", "strategy_id": "gale_orb5",
         "symbol": "BBB", "action": "entry", "qty": 5, "price": 20.0},
        {"timestamp_utc": "2026-08-03T14:10:00+00:00", "strategy_id": "gale_orb5",
         "symbol": "BBB", "action": "stop_filled", "qty": 5, "price": 19.0},
    ])

    result = attribute_by_strategy(rows)

    assert result["tempest_first_pullback"]["AAA"]["realized_pnl"] == 5.0
    assert result["gale_orb5"]["BBB"]["realized_pnl"] == -5.0


def test_same_symbol_resting_order_blocks_gale():
    broker = FakeBroker()
    broker.pending.add("GALE")
    trader = GalePaperTrader(
        broker,
        FakeSource({"GALE": gale_frame().iloc[:6]}),
        screen_evidence=screen_evidence(),
        now_fn=now_936,
    )

    result = trader.run_once(["GALE"], dry_run=False)

    assert result["entries"][0]["action"] == "blocked"
    assert "working/unresolved" in result["entries"][0]["reason"]
