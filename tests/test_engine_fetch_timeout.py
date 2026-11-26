"""Regression test for the 2026-07-04 incident: a single symbol's fetch call
hung with no exception and no timeout of its own, and run_once()'s fetch phase
(plain `as_completed(futures)`, no timeout) waited on it forever — freezing the
whole poll loop for 41+ hours with zero new log lines. run_once must now bound
the whole fetch phase (sigengine.engine._FETCH_PHASE_TIMEOUT) and treat any
still-pending symbol as a failure for that cycle instead of blocking on it.

    .venv/bin/pytest -q tests/test_engine_fetch_timeout.py
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import sigengine.engine as eng  # noqa: E402


class FakeSettings:
    webhook_secret = "testsecret"


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    def get_account(self):
        return type("Acct", (), {"equity": "100000", "last_equity": "100000"})()

    def get_all_positions(self):
        return []


class FakeOM:
    def __init__(self, *a, **kw):
        pass

    def handle(self, payload):
        return {"status": "ok"}


def _fake_fetch_and_recompute(sym, cfg, data_cfg):
    if sym == "SLOW":
        # Longer than the (monkeypatched, tiny) fetch-phase timeout, so the
        # timeout path is exercised, but short enough the thread finishes
        # before the test process exits (non-daemon threads block interpreter
        # shutdown) instead of leaking forever.
        time.sleep(0.6)
        return None
    return None  # "not enough cached bars yet" — simplest valid non-hang result


def test_fetch_phase_timeout_does_not_block_other_symbols(monkeypatch, tmp_path):
    monkeypatch.setattr(eng, "_FETCH_PHASE_TIMEOUT", 0.2)
    monkeypatch.setattr(eng, "load_config", lambda: {
        "symbols": {
            "SLOW": {"enabled": True, "interval": "1h"},
            "FAST": {"enabled": True, "interval": "1h"},
        },
        "data": {},
    })
    monkeypatch.setattr(eng, "_fetch_and_recompute", _fake_fetch_and_recompute)
    monkeypatch.setattr(eng, "AlpacaClient", FakeClient)
    monkeypatch.setattr(eng, "OrderManager", FakeOM)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")

    start = time.monotonic()
    summary = eng.run_once(FakeSettings(), dry_run=True, quiet=True)
    elapsed = time.monotonic() - start

    # Must return well before SLOW's own 0.6s sleep finishes — proves run_once
    # did not wait on it, only on the 0.2s phase timeout.
    assert elapsed < 0.5, f"run_once blocked on the stuck symbol ({elapsed:.2f}s)"
    assert summary["FAST"] == []
    assert len(summary["SLOW"]) == 1
    assert summary["SLOW"][0]["status"] == "error"
    assert "timed out" in summary["SLOW"][0]["reason"]
