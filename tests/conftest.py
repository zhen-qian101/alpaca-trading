"""Session-wide guards, applied before any test module imports project code.

relay.config resolves ALPACA_DATA_DIR and calls load_dotenv() at IMPORT time, and
load_dotenv() never overrides a variable that is already set. Setting these here,
first, means no test can reach the live data disk (/mnt/sandisk/alpaca: the bar
cache, the trades journal, engine state, the recap archive) or pick up the real
broker keys from .env, whatever order the files run in and whether or not a test
remembers to monkeypatch.

Before this file existed isolation was hand-rolled per test, and one test
(test_delete_bars_clears_one_symbol_and_interval_only) patched a name that does
not exist and wrote into the live bars.sqlite. The same isolation is what lets
the suite run on a GitHub Actions runner, which has no /mnt/sandisk at all.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

import pytest

_DATA_DIR = tempfile.mkdtemp(prefix="alpaca-tests-")
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)

os.environ["ALPACA_DATA_DIR"] = _DATA_DIR
os.environ["BRIEFING_ARCHIVE_DIR"] = os.path.join(_DATA_DIR, "briefings")
# Dummy broker credentials: no test needs real ones, and these win over .env.
os.environ["ALPACA_KEY"] = "PKTESTKEY"
os.environ["ALPACA_SECRET"] = "testsecret"
os.environ["PAPER_TRADING"] = "true"
os.environ.pop("ALPACA_BASE_URL", None)
# An accidental IBKR connect fails fast instead of reaching the live Gateway.
os.environ["IBKR_HOST"] = "127.0.0.1"
os.environ["IBKR_PORT"] = "1"


@pytest.fixture(autouse=True)
def _no_closed_orders_kept(monkeypatch):
    """web.server keeps the closed orders it has read between calls
    (_ClosedOrders), keyed on the broker client object. A fake that two tests
    share would answer the second test from the first one's orders, so every
    test starts with nothing kept."""
    server = sys.modules.get("web.server")
    if server is not None and hasattr(server, "_ClosedOrders"):
        monkeypatch.setattr(server, "_ORDERS", server._ClosedOrders())
