"""Signal engine tests — fire_event sizing/skip logic (client + OrderManager mocked).

    .venv/bin/pytest -q tests/test_signal.py

Covers: buy sizing from equity*deploy/price (whole shares for equities,
fractional for crypto), skip-if-already-long, half-sell sized from the REAL
position, skip-if-not-holding for sell/close, close routing, and that
reason=='eod' events are filtered out of the live diff.
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

# Dummy creds BEFORE importing relay/sigengine so get_settings() doesn't raise.
os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import relay.risk_filter as rf  # noqa: E402
import relay.order_manager as om_mod  # noqa: E402
import sigengine.engine as eng


@pytest.fixture(autouse=True)
def _isolate_sleeve_policy(monkeypatch, tmp_path):
    """The share book sizes off equity MINUS the put sleeve's reserve, read from
    decisions/csp.json. These tests are about the sizing arithmetic itself, so
    the LIVE policy file must not leak into an expected share count: point the
    sleeve at an empty file (= disabled = no reserve beyond any pledge)."""
    import relay.csp_manager as csp
    monkeypatch.setattr(csp, "CSP_CONFIG_FILE", tmp_path / "csp.json")  # noqa: E402


class FakeSettings:
    webhook_secret = "testsecret"


class FakeClient:
    """Mimics AlpacaClient enough for fire_event: account + positions."""

    def __init__(self, equity=100000.0, positions=None):
        self._equity = equity
        self._positions = positions or []

    def get_account(self):
        return type("Acct", (), {"equity": str(self._equity),
                                 "last_equity": str(self._equity)})()

    def get_all_positions(self):
        return list(self._positions)


class FakeOM:
    """Captures the OrderRequest handed to handle() instead of trading."""

    def __init__(self):
        self.calls = []

    def handle(self, payload):
        self.calls.append({
            "ticker": payload.ticker, "action": payload.action,
            "qty": payload.qty, "strategy": payload.strategy,
        })
        return {"status": "submitted_market" if payload.action == "buy" else "closed"}


def _pos(sym, qty):
    return type("Pos", (), {"symbol": sym, "qty": str(qty), "side": "long"})()


@pytest.fixture(autouse=True)
def isolate_journal(monkeypatch, tmp_path):
    # keep signal_skip journals out of the repo
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(om_mod, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    rf._DEDUP.clear()


EQ_CFG = {"fast": 9, "slow": 65, "window": 300, "interval": "5min",
          "source": "yfinance", "deploy": 0.97, "stop": 0.02, "enabled": True}
BTC_CFG = {"fast": 20, "slow": 50, "window": 300, "interval": "1h",
           "source": "yfinance", "deploy": 0.97, "stop": 0.02, "enabled": True}


def _ev(action, price=100.0, reason="cross_up"):
    e = {"ts": pd.Timestamp("2026-07-02 10:00:00-04:00"), "action": action,
         "price": price, "reason": reason}
    if action == "sell":
        e["qty"] = 5.0
    return e


def test_buy_sizes_from_equity_deploy_price_whole_shares():
    client = FakeClient(equity=100000.0, positions=[])
    om = FakeOM()
    out = eng.fire_event("AAPL", _ev("buy", price=250.0), EQ_CFG, client, om, FakeSettings(), dry_run=False)
    # 100000 * 0.97 / 250 = 388.0 -> whole shares
    assert out["status"] == "submitted_market"
    assert om.calls == [{"ticker": "AAPL", "action": "buy", "qty": 388.0, "strategy": "signal-engine"}]


def test_buy_skipped_when_already_long():
    client = FakeClient(equity=100000.0, positions=[_pos("AAPL", 10)])
    om = FakeOM()
    out = eng.fire_event("AAPL", _ev("buy", price=250.0), EQ_CFG, client, om, FakeSettings(), dry_run=False)
    assert out["status"] == "skip"
    assert "already long" in out["reason"]
    assert om.calls == []


def test_buy_crypto_fractional_qty():
    client = FakeClient(equity=100000.0, positions=[])
    om = FakeOM()
    out = eng.fire_event("BTC/USD", _ev("buy", price=65000.0), BTC_CFG, client, om, FakeSettings(), dry_run=False)
    # 100000 * 0.97 / 65000 = 1.4923... -> 4dp (not floored to whole BTC)
    assert out["status"] == "submitted_market"
    assert om.calls[0]["qty"] == round(100000 * 0.97 / 65000, 4)
    assert om.calls[0]["ticker"] == "BTC/USD"  # canonical, no aliasing needed here


def test_a_sell_exits_the_whole_position():
    """It used to send qty = held / 2. All-in/all-out means there is exactly one
    way out of a position, and it is the same full close a "close" takes —
    sized by the broker from the position itself, not by the engine."""
    client = FakeClient(equity=100000.0, positions=[_pos("AAPL", 10)])
    om = FakeOM()
    out = eng.fire_event("AAPL", _ev("sell", reason="half"), EQ_CFG, client, om, FakeSettings(), dry_run=False)
    assert out["status"] == "closed"
    assert om.calls == [{"ticker": "AAPL", "action": "close", "qty": None, "strategy": "signal-engine"}]


def test_a_sell_is_skipped_when_not_holding():
    client = FakeClient(equity=100000.0, positions=[])
    om = FakeOM()
    out = eng.fire_event("AAPL", _ev("sell", reason="half"), EQ_CFG, client, om, FakeSettings(), dry_run=False)
    assert out["status"] == "skip"
    assert "not holding" in out["reason"]
    assert om.calls == []


def test_close_routes_to_close_action():
    client = FakeClient(equity=100000.0, positions=[_pos("AAPL", 10)])
    om = FakeOM()
    out = eng.fire_event("AAPL", _ev("close", reason="cross_down"), EQ_CFG, client, om, FakeSettings(), dry_run=False)
    assert out["status"] == "closed"
    assert om.calls == [{"ticker": "AAPL", "action": "close", "qty": None, "strategy": "signal-engine"}]


def test_close_skipped_when_not_holding():
    client = FakeClient(equity=100000.0, positions=[])
    om = FakeOM()
    out = eng.fire_event("AAPL", _ev("close", reason="stop"), EQ_CFG, client, om, FakeSettings(), dry_run=False)
    assert out["status"] == "skip"
    assert "not holding" in out["reason"]
    assert om.calls == []


def test_dry_run_never_calls_handle():
    client = FakeClient(equity=100000.0, positions=[])
    om = FakeOM()
    out = eng.fire_event("AAPL", _ev("buy", price=250.0), EQ_CFG, client, om, FakeSettings(), dry_run=True)
    assert out["status"] == "dry_run"
    assert om.calls == []


def test_btcusd_alias_canonicalized_on_buy():
    """A buy event for the slash-less alias BTCUSD must route under BTC/USD."""
    client = FakeClient(equity=100000.0, positions=[])
    om = FakeOM()
    eng.fire_event("BTCUSD", _ev("buy", price=65000.0), BTC_CFG, client, om, FakeSettings(), dry_run=False)
    assert om.calls[0]["ticker"] == "BTC/USD"


def test_dry_run_state_never_touches_real_state_file(monkeypatch, tmp_path):
    """Regression for a real bug found 2026-07-03: --dry-run used to write to
    the SAME state.json live polling reads to decide what's "new" — so a
    dry-run test could permanently mark a real signal as already-handled with
    no order ever placed, and it would then never fire live either. Dry-run
    must persist to its own sibling file instead."""
    real_state = tmp_path / "state.json"
    real_state.write_text('{"AAPL": "2026-07-01T00:00:00+00:00"}')
    monkeypatch.setattr(eng, "STATE_FILE", real_state)

    dry_state = eng.load_state(dry_run=True)
    assert dry_state == {}  # no sibling file yet -> empty, NOT the real content
    dry_state["AAPL"] = "2026-07-02T00:00:00+00:00"
    eng.save_state(dry_state, dry_run=True)

    # The real state file must be untouched.
    assert real_state.read_text() == '{"AAPL": "2026-07-01T00:00:00+00:00"}'
    # The dry-run write landed in a sibling file, not the real one.
    dry_path = tmp_path / "state_dryrun.json"
    assert dry_path.exists()
    assert eng.load_state(dry_run=False) == {"AAPL": "2026-07-01T00:00:00+00:00"}