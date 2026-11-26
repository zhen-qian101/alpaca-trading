"""The engine trades each enabled symbol on its configured bar interval and
polls on sigengine/config.json's poll_seconds.

    .venv/bin/pytest -q tests/test_engine_poll_config.py
"""
from __future__ import annotations

import os

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


def test_run_once_uses_configured_interval(monkeypatch, tmp_path):
    seen_intervals = {}

    def _fake_fetch_and_recompute(sym, cfg, data_cfg):
        seen_intervals[sym] = cfg["interval"]
        return None

    monkeypatch.setattr(eng, "load_config", lambda: {
        "symbols": {"AAPL": {"enabled": True, "interval": "1h"}},
        "data": {},
    })
    monkeypatch.setattr(eng, "_fetch_and_recompute", _fake_fetch_and_recompute)
    monkeypatch.setattr(eng, "AlpacaClient", FakeClient)
    monkeypatch.setattr(eng, "OrderManager", FakeOM)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")

    eng.run_once(FakeSettings(), dry_run=True, quiet=True)

    assert seen_intervals == {"AAPL": "1h"}


def test_effective_poll_seconds_reads_config():
    cfg = {"symbols": {"AAPL": {"enabled": True}}, "poll_seconds": 300}
    assert eng._effective_poll_seconds(cfg) == 300
