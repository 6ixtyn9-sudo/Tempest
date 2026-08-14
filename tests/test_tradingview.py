"""Tests for the TradingView screener adapter (fixture-based, no network)."""

import json

from tempest.sources import tradingview
from tempest.sources.tradingview import _payload, build_filter, screen

_FIXTURE = {
    "totalCount": 2,
    "data": [
        {"s": "NASDAQ:BOXL", "d": ["BOXL", 7.87, 168.6, 55.8, 115.3, 564333.0, 66169726]},
        {"s": "NYSE:RSKD", "d": ["RSKD", 6.32, 19.9, 14.99, 10.09, 72659458.0, 634000]},
    ],
}


def test_build_filter_matches_pillars():
    f = build_filter()
    rights = {item["left"]: item for item in f}
    assert rights["close"]["right"] == [2.0, 20.0]
    assert rights["gap"]["right"] == 2.0
    assert rights["relative_volume_10d_calc"]["right"] == 5.0
    assert rights["float_shares_outstanding"]["right"] == 100_000_000
    assert rights["volume"]["right"] == 1_000_000


def test_payload_shape():
    p = _payload(build_filter())
    assert p["symbols"]["query"]["types"] == ["stock"]
    assert "gap" in p["columns"] and "float_shares_outstanding" in p["columns"]


def test_screen_parses_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(tradingview, "SCAN_CACHE_PATH", tmp_path / "c.json")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return _FIXTURE

    monkeypatch.setattr(
        tradingview.requests, "post",
        lambda *a, **k: _Resp(),
    )
    rows = screen(use_cache=False)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "BOXL"
    assert rows[0]["gap_pct"] == 55.8
    assert rows[0]["relvol"] == 115.3
    assert rows[1]["symbol"] == "RSKD"


def test_screen_failure_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(tradingview, "SCAN_CACHE_PATH", tmp_path / "c.json")

    def _boom(*a, **k):
        raise RuntimeError("blocked")

    monkeypatch.setattr(tradingview.requests, "post", _boom)
    assert screen(use_cache=False) == []


def test_cache_short_circuits(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"_ts": __import__("time").time(), "rows": [{"symbol": "X"}]}))
    monkeypatch.setattr(tradingview, "SCAN_CACHE_PATH", cache)
    monkeypatch.setattr(tradingview, "SCAN_CACHE_MINUTES", 15)
    monkeypatch.setattr(tradingview.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not hit network")))
    rows = screen(use_cache=True)
    assert rows == [{"symbol": "X"}]
