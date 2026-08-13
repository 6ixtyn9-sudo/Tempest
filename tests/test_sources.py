"""Tests for the yfinance 1m adapter's chunking (the ~7-day server cap)."""

from datetime import datetime, timedelta, timezone

from tempest.sources.yfinance_1m import _chunks, _clamp_start, PILOT_MAX_DAYS


def test_chunks_respect_seven_day_cap():
    end = datetime(2026, 8, 13, tzinfo=timezone.utc)
    start = end - timedelta(days=30)
    chunks = list(_chunks(start, end))
    assert len(chunks) > 1
    for cs, ce in chunks:
        assert (ce - cs) <= timedelta(days=7)
    # windows are contiguous and cover the range
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for (_, ce), (ns, _) in zip(chunks, chunks[1:]):
        assert ce == ns


def test_clamp_start_respects_pilot_max_days():
    end = datetime(2026, 8, 13, tzinfo=timezone.utc)
    start = end - timedelta(days=90)
    clamped = _clamp_start(start, end)
    assert (end - clamped) <= timedelta(days=PILOT_MAX_DAYS)
