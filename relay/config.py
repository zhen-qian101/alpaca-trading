"""Centralized config loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env at import time so importing config anywhere picks up secrets.
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DECISIONS_FILE = ROOT / "decisions" / "decisions.json"

# Generated data lives on a big disk (default: the 1.8T Sandisk mounted at
# /mnt/sandisk) so the bar cache / journal / logs / backtest CSVs don't grow the
# project dir on the NVMe. Override with ALPACA_DATA_DIR. Code + venv +
# decisions.json stay in the repo.
DATA_DIR = Path(os.getenv("ALPACA_DATA_DIR", "/mnt/sandisk/alpaca"))
BARS_DB_FILE = DATA_DIR / "bars" / "bars.sqlite"
JOURNAL_FILE = DATA_DIR / "journal" / "trades.jsonl"
LOGS_DIR = DATA_DIR / "logs"
BACKTEST_DIR = DATA_DIR / "backtest"
SIGENGINE_STATE_FILE = DATA_DIR / "sigengine" / "state.json"


def ensure_data_dirs() -> None:
    """Create the data subdirs on the big disk if missing (idempotent)."""
    for p in (BARS_DB_FILE.parent, JOURNAL_FILE.parent, LOGS_DIR,
              BACKTEST_DIR, SIGENGINE_STATE_FILE.parent):
        p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    alpaca_key: str
    alpaca_secret: str
    webhook_secret: str
    paper: bool
    base_url: str | None  # None = let alpaca-py pick by paper flag

    @classmethod
    def from_env(cls) -> "Settings":
        key = os.getenv("ALPACA_KEY", "")
        secret = os.getenv("ALPACA_SECRET", "")
        if not key or not secret:
            raise RuntimeError(
                "ALPACA_KEY / ALPACA_SECRET not set. Copy .env.example to .env and fill them in."
            )
        raw_url = os.getenv("ALPACA_BASE_URL", "").strip().rstrip("/")
        # alpaca-py appends "/v2/..." itself, so strip a trailing /v2 the user may have added.
        if raw_url.endswith("/v2"):
            raw_url = raw_url[:-3]
        return cls(
            alpaca_key=key,
            alpaca_secret=secret,
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            paper=os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes"),
            base_url=raw_url or None,
        )


def get_settings() -> Settings:
    return Settings.from_env()