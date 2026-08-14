"""TradingView screener adapter — the live detection plane for Tempest.

Uses the same JSON backend the tradingview.com/screener UI calls
(scanner.tradingview.com/america/scan). It lets us run the five-pillar
screen server-side (price $2-20, gap > 2%, relvol > 5x, float < 20M,
volume > 1M) and get the qualifying tickers in one request — the rare
mover universe a fixed symbol basket cannot capture.

Caveats (documented, not hidden):
  - This is the UI's semi-unofficial backend; the payload may change and
    heavy automated use can be throttled. Politeness: ONE scan per run,
    cached for SCAN_CACHE_MINUTES, no loops.
  - relative_volume_10d_calc is the 10-day RVOL (TradingView standard),
    an approximation of the 50-day RVOL in the source methodology.
  - Free tier data is delayed; the screen is still the right universe
    finder for end-of-day evidence accumulation.
"""

import json
import time
from datetime import datetime, timezone

import requests

from tempest.config import DATA_DIR

SCAN_URL = "https://scanner.tradingview.com/america/scan"
SCAN_CACHE_PATH = DATA_DIR / "screen_cache.json"
SCAN_CACHE_MINUTES = 15

LAST_ERROR: str | None = None

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/screener/",
}

# Column order in the response's "d" arrays (must match _COLUMNS).
_COLUMNS = [
    "name", "close", "change", "gap", "relative_volume_10d_calc",
    "float_shares_outstanding", "volume",
]


def build_filter(
    price_min: float = 2.0,
    price_max: float = 20.0,
    gap_min_pct: float = 2.0,
    relvol_min: float = 5.0,
    float_max: float = 20_000_000,
    volume_min: float = 1_000_000,
) -> list[dict]:
    """The five-pillar screen as a TradingView filter payload."""
    return [
        {"left": "type", "operation": "equal", "right": "stock"},
        {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]},
        {"left": "close", "operation": "in_range", "right": [price_min, price_max]},
        {"left": "gap", "operation": "greater", "right": gap_min_pct},
        {"left": "relative_volume_10d_calc", "operation": "greater", "right": relvol_min},
        {"left": "float_shares_outstanding", "operation": "less", "right": float_max},
        {"left": "volume", "operation": "greater", "right": volume_min},
    ]


def _payload(filters: list[dict]) -> dict:
    return {
        "symbols": {"tickers": [], "query": {"types": ["stock"]}},
        "columns": _COLUMNS,
        "filter": filters,
    }


def _read_cache() -> dict | None:
    if not SCAN_CACHE_PATH.exists():
        return None
    try:
        cached = json.loads(SCAN_CACHE_PATH.read_text())
        age_min = (time.time() - cached.get("_ts", 0)) / 60.0
        if age_min <= SCAN_CACHE_MINUTES and "rows" in cached:
            return cached
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_cache(rows: list[dict]) -> None:
    try:
        SCAN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCAN_CACHE_PATH.write_text(json.dumps(
            {"_ts": time.time(), "rows": rows}, indent=2
        ))
    except OSError:  # pragma: no cover - cache write must never break the screen
        pass


def screen(filters: list[dict] | None = None, use_cache: bool = True) -> list[dict]:
    """Run the TradingView scan. Returns rows:
    [{symbol, close, change, gap_pct, relvol, float_shares, volume}].
    Empty list on any failure (never raises)."""
    global LAST_ERROR
    LAST_ERROR = None
    if use_cache:
        cached = _read_cache()
        if cached is not None:
            return cached["rows"]
    filters = filters if filters is not None else build_filter()
    try:
        resp = requests.post(
            SCAN_URL, headers=_HEADERS, json=_payload(filters), timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        LAST_ERROR = f"{type(e).__name__}: {e}"
        return []
    rows = []
    for item in data.get("data", []):
        sym_full = str(item.get("s", ""))
        d = item.get("d", [])
        if len(d) < len(_COLUMNS):
            continue
        ticker = sym_full.split(":")[-1] if ":" in sym_full else sym_full
        rows.append({
            "symbol": ticker.upper(),
            "close": d[1],
            "change_pct": d[2],
            "gap_pct": d[3],
            "relvol": d[4],
            "float_shares": d[5],
            "volume": d[6],
            "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
        })
    if use_cache:
        _write_cache(rows)
    return rows
