"""Path/env config for Tempest. Mirrors Price's conventions: DATA_DIR under
the repo root, env loaded from .env, symbol hygiene via a strict pattern."""

import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional convenience
    def load_dotenv(*args, **kwargs):
        return False

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "localdata"
WAREHOUSE_DIR = DATA_DIR / "warehouse"

DATA_DIR.mkdir(parents=True, exist_ok=True)
WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)

# Symbols: 1-5 upper letters/digits, optional hyphen (BRK-B), no slashes.
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,5}$")

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

# Timeframe constants for the 1-minute pilot.
PILOT_MAX_DAYS = 30          # yfinance 1m ceiling
DEFAULT_DAYS = 30
