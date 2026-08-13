"""Tests for the paper broker wrapper, risk rails, and trader."""

import pandas as pd
import pytest

from tempest import broker, risk
from tempest.risk import RiskLimits, append_journal, check_entry_ok, record_cooldown
from tempest.trader import PaperTrader, _ny_minutes_to_close
from tests.conftest import squeeze_pullback_break_frame


class FakeBroker:
    """In-memory broker: records orders, holds positions, no network."""

    def __init__(self):
        self.orders = []
        self.closed = []
        self.positions = pd.DataFrame()
        self.client_ctor_calls = 0

    def get_trading_client(self):
        self.client_ctor_calls += 1
        return object()  # opaque client

    def get_open_positions(self):
        return self.positions

    def submit_bracket(self, client, symbol, qty, side, entry, stop, target, cid):
        self.orders.append({
            "symbol": symbol, "qty": qty, "side": side, "entry": entry,
            "stop": stop, "target": target, "cid": cid,
        })

    def close_position(self, client, symbol):
        self.closed.append(symbol)
        self.positions = self.positions[
            self.positions["symbol"].astype(str).str.upper() != symbol.upper()
        ].reset_index(drop=True)


class FakeSource:
    """Returns a canned frame per symbol."""

    def __init__(self, frames: dict):
        self.frames = frames

    def fetch_1m(self, symbol, start, end):
        return self.frames.get(symbol, pd.DataFrame())


def _ending_at_crossing():
    """Textbook session truncated so the crossing candle is the LAST bar."""
    df = squeeze_pullback_break_frame()
    return df.iloc[:8].reset_index(drop=True)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    monkeypatch.setattr(risk, "HALT_FLAG_PATH", tmp_path / "halt.flag")


# ---------------------------------------------------------------------------
# Broker paper guard
# ---------------------------------------------------------------------------

def test_broker_requires_paper_env(monkeypatch):
    monkeypatch.delenv("TEMPEST_PAPER", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    from tempest.sources.alpaca import require_paper_env
    with pytest.raises(RuntimeError):
        require_paper_env()
    monkeypatch.setenv("TEMPEST_PAPER", "1")
    require_paper_env()  # no raise


def test_broker_submit_bracket_shape():
    """The bracket carries the strategy's risk shape: entry limit, 2R
    take-profit, stop at the pullback low, DAY time-in-force."""
    fake = FakeBroker()

    class _Client:
        def submit_order(self, order):
            fake.orders.append(order)
            return order

    broker.submit_bracket(
        _Client(), "YXT", 100, "buy", entry_limit=10.50,
        stop_price=10.20, take_profit_price=11.10, client_order_id="tempest-x",
    )
    order = fake.orders[0]
    assert order.symbol == "YXT"
    assert order.qty == 100
    assert order.limit_price == 10.50
    assert order.stop_loss.stop_price == 10.20
    assert order.take_profit.limit_price == 11.10
    assert "DAY" in str(order.time_in_force)


# ---------------------------------------------------------------------------
# Risk rails
# ---------------------------------------------------------------------------

def test_check_entry_ok_blocks_halt(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "HALT_FLAG_PATH", tmp_path / "halt.flag")
    (tmp_path / "halt.flag").write_text("")
    ok, reasons = check_entry_ok("YXT", 100, 10.0, RiskLimits(), pd.DataFrame())
    assert ok is False and any("halt" in r for r in reasons)


def test_check_entry_ok_blocks_daily_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    append_journal({
        "timestamp_utc": "2026-08-13T15:00:00+00:00", "symbol": "BOXL",
        "action": "exit", "qty": 100, "price": 10.0, "pnl": -300.0,
    })
    ok, reasons = check_entry_ok("YXT", 100, 10.0, RiskLimits(), pd.DataFrame())
    assert ok is False and any("daily loss" in r for r in reasons)


def test_cooldown_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    record_cooldown("YXT")
    ok, reasons = check_entry_ok("YXT", 100, 10.0, RiskLimits(), pd.DataFrame())
    assert ok is False and any("cooldown" in r for r in reasons)


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------

def test_trader_enters_fresh_signal_with_bracket(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    broker_ = FakeBroker()
    source = FakeSource({"YXT": _ending_at_crossing()})
    trader = PaperTrader(broker_, source, limits=RiskLimits(max_notional_per_position=1000.0))
    result = trader.run_once(["YXT"], dry_run=False)
    entries = [e for e in result["entries"] if e["action"] == "entered"]
    assert len(entries) == 1
    order = broker_.orders[0]
    assert order["symbol"] == "YXT"
    assert order["stop"] < order["entry"] < order["target"]
    assert order["target"] - order["entry"] == pytest.approx(2 * (order["entry"] - order["stop"]))
    # journaled
    j = risk.load_journal()
    assert (j["action"] == "entry").any()


def test_trader_watches_when_no_fresh_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    broker_ = FakeBroker()
    # Only the first 5 bars: squeeze started, pullback not resolved.
    df = squeeze_pullback_break_frame().iloc[:5].reset_index(drop=True)
    source = FakeSource({"YXT": df})
    trader = PaperTrader(broker_, source)
    result = trader.run_once(["YXT"], dry_run=False)
    assert result["entries"][0]["action"] == "watching"
    assert broker_.orders == []


def test_trader_blocks_when_already_open(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    broker_ = FakeBroker()
    broker_.positions = pd.DataFrame([{
        "symbol": "YXT", "qty": 10, "avg_entry_price": 10.0,
        "current_price": 10.2, "market_value": 102.0,
    }])
    source = FakeSource({"YXT": _ending_at_crossing()})
    trader = PaperTrader(broker_, source)
    result = trader.run_once(["YXT"], dry_run=False)
    assert result["entries"][0]["action"] == "blocked"
    assert "already open" in result["entries"][0]["reason"]
    assert broker_.orders == []


def test_trader_exits_at_horizon(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    # Entry 20 minutes ago -> past the 15-bar horizon.
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    append_journal({
        "timestamp_utc": past, "symbol": "YXT",
        "action": "entry", "side": "buy", "qty": 100, "price": 10.0,
        "status": "filled", "entry_price": 10.0,
    })
    broker_ = FakeBroker()
    broker_.positions = pd.DataFrame([{
        "symbol": "YXT", "qty": 100, "avg_entry_price": 10.0,
        "current_price": 10.3, "market_value": 1030.0,
    }])
    trader = PaperTrader(broker_, source=FakeSource({}),
                         limits=RiskLimits(horizon_bars=15))
    result = trader.run_once([], dry_run=False)
    assert result["exits"] and result["exits"][0]["action"] == "exited"
    assert broker_.closed == ["YXT"]
    j = risk.load_journal()
    assert (j["action"] == "exit").any()


def test_trader_dry_run_places_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(risk, "COOLDOWN_PATH", tmp_path / "cooldown.json")
    broker_ = FakeBroker()
    source = FakeSource({"YXT": _ending_at_crossing()})
    trader = PaperTrader(broker_, source)
    result = trader.run_once(["YXT"], dry_run=True)
    assert result["entries"][0]["action"] == "would_enter"
    assert broker_.orders == []
    assert broker_.client_ctor_calls == 0


def test_ny_minutes_to_close_sane():
    m = _ny_minutes_to_close(pd.Timestamp("2026-08-13 15:00:00+00:00").to_pydatetime())
    # 15:00 UTC = 11:00 ET -> 5h to close = 300 min
    assert m == 300
