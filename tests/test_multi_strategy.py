"""Tests for the signal engine's multi-strategy dispatch (dual-candidate
rotation: independent signal sources for the same symbol — live, MACD+SMA and
Bollinger+RSI).

Covers:
  - backward compat: a legacy flat config (no "strategies" key) normalizes to
    exactly one implicit EMA strategy, byte-identical to the pre-multi-strategy
    behavior.
  - a "strategies" list runs each strategy and merges/tags/time-sorts events.
  - the same-poll double-buy guard: if two strategies both signal a buy for the
    same symbol in one poll, only the first (by ts) reaches the order path —
    the second is skipped, not sent to fire_event/OrderManager at all.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import numpy as np
import pandas as pd
import pytest

import backtest.multi_strategy as ms  # noqa: E402
import relay.order_manager as om_mod  # noqa: E402
import relay.risk_filter as rf  # noqa: E402
import sigengine.engine as eng  # noqa: E402


class FakeClient:
    def __init__(self, equity=100000.0, positions=None):
        self._equity = equity
        self._positions = positions or []

    def get_account(self):
        return type("Acct", (), {"equity": str(self._equity),
                                 "last_equity": str(self._equity)})()

    def get_all_positions(self):
        return list(self._positions)


class FakeOM:
    def __init__(self):
        self.calls = []

    def handle(self, payload):
        self.calls.append({"ticker": payload.ticker, "action": payload.action,
                           "qty": payload.qty})
        return {"status": "submitted_market" if payload.action == "buy" else "closed"}


class FakeSettings:
    webhook_secret = "testsecret"


@pytest.fixture(autouse=True)
def isolate_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(om_mod, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    rf._DEDUP.clear()


LEGACY_CFG = {"fast": 9, "slow": 65, "window": 300, "interval": "5min",
              "deploy": 0.97, "stop": 0.02, "enabled": True}

# The live pair, on lengths short enough for the small pullback fixture below
# (and a stop wide enough that neither trade is stopped out of it).
MULTI_CFG = {
    "interval": "5min", "deploy": 0.97, "stop": 0.5, "enabled": True,
    "strategies": [
        {"type": "macd_sma", "fast_len": 2, "slow_len": 5, "signal_len": 2, "veryslow_len": 10},
        {"type": "bollinger_rsi", "rsi_length": 2, "bb_length": 5, "bb_mult": 1.0},
    ],
}

MACD_CFG = {
    "interval": "5min", "deploy": 0.97, "stop": 0.02, "enabled": True,
    "strategies": [
        {"type": "ema", "fast": 9, "slow": 65, "window": 300},
        {"type": "macd_sma", "fast_len": 12, "slow_len": 26, "signal_len": 9, "veryslow_len": 200},
    ],
}


def _trend_df(n=400):
    """Deterministic monotonic uptrend -> an EMA cross-up should eventually
    fire on this series."""
    t = np.arange(n)
    close = 100 * (1.001 ** t)
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({"high": close * 1.001, "low": close * 0.999, "close": close}, index=idx)


def test_legacy_config_normalizes_to_single_implicit_ema_strategy():
    specs = ms.symbol_strategies(LEGACY_CFG)
    assert specs == [{"type": "ema", "fast": 9, "slow": 65, "window": 300}]


def test_multi_strategy_config_returned_as_is():
    specs = ms.symbol_strategies(MULTI_CFG)
    assert specs == MULTI_CFG["strategies"]


def _pullback_df():
    """Deterministic uptrend with one short pullback: the Bollinger+RSI arm buys
    the dip and the MACD+SMA arm buys the resumed trend."""
    close = [100.0]
    for step, bars in ((0.3, 40), (-1.5, 3), (2.0, 6), (0.3, 20)):
        for _ in range(bars):
            close.append(close[-1] + step)
    close = np.array(close)
    idx = pd.date_range("2024-01-01", periods=len(close), freq="5min", tz="UTC")
    return pd.DataFrame({"high": close + 0.1, "low": close - 0.1, "close": close}, index=idx)


def test_run_symbol_strategies_tags_and_time_sorts_events():
    df = _pullback_df()
    events = ms.run_symbol_strategies(df, MULTI_CFG, "TEST")
    assert events, "expected at least one event on the pullback fixture"
    assert all(e["strategy"] in ("macd_sma", "bollinger_rsi") for e in events)
    tss = [e["ts"] for e in events]
    assert tss == sorted(tss)
    # both strategies should have fired at least one buy
    strategies_that_bought = {e["strategy"] for e in events if e["action"] == "buy"}
    assert strategies_that_bought == {"macd_sma", "bollinger_rsi"}


def test_macd_sma_dispatch_tags_and_merges_events():
    """MACD+SMA's veryslow_len=200 + slow_len=26 lag needs a long warm-up, and
    its histogram-crosses-zero entry trigger won't refire on an already-established
    monotonic uptrend (hist is already positive throughout) — this only checks
    dispatch/tag/merge correctness."""
    df = _trend_df()
    events = ms.run_symbol_strategies(df, MACD_CFG, "TEST")
    assert events, "expected at least one event on a monotonic uptrend"
    assert all(e["strategy"] in ("ema", "macd_sma") for e in events)
    tss = [e["ts"] for e in events]
    assert tss == sorted(tss)
    strategies_that_bought = {e["strategy"] for e in events if e["action"] == "buy"}
    assert "ema" in strategies_that_bought


def test_legacy_single_strategy_matches_pre_multi_strategy_output():
    """_run_symbol_strategies on a legacy config must equal calling the EMA
    loop directly — the multi-strategy refactor must not change single-
    strategy behavior at all."""
    from backtest.ema_scaled_rolling import run_with_events
    df = _trend_df()
    via_dispatch = ms.run_symbol_strategies(df, LEGACY_CFG, "TEST")
    direct = run_with_events(df, 9, 65, 300, deploy=0.97, stop_pct=0.02)[2]
    # dispatch adds a "strategy" tag; strip it before comparing.
    stripped = [{k: v for k, v in e.items() if k != "strategy"} for e in via_dispatch]
    assert stripped == direct


def test_same_poll_double_buy_is_deduped_before_reaching_order_path():
    ts1 = pd.Timestamp("2026-07-02T10:00:00-04:00")
    ts2 = pd.Timestamp("2026-07-02T10:05:00-04:00")
    new = [
        {"ts": ts1, "action": "buy", "price": 100.0, "reason": "cross_up", "strategy": "ema"},
        {"ts": ts2, "action": "buy", "price": 101.0, "reason": "rsi_bb_long", "strategy": "bollinger_rsi"},
    ]
    client = FakeClient(equity=100000.0, positions=[])
    om = FakeOM()
    fired = eng._fire_new_events("AAPL", new, LEGACY_CFG, client, om, FakeSettings(), dry_run=False)
    assert len(fired) == 2
    assert fired[0]["status"] == "submitted_market"
    assert fired[1]["status"] == "skip"
    assert "duplicate buy in same poll" in fired[1]["reason"]
    # only ONE order actually reached the OrderManager
    assert len(om.calls) == 1
    assert om.calls[0]["action"] == "buy"


def test_second_buy_not_deduped_if_first_was_blocked():
    """If the first buy in the batch didn't actually open a position (e.g. it
    was risk-blocked), the second strategy's buy must still get a real chance
    — the guard tracks successful opens, not just "was a buy event seen"."""
    ts1 = pd.Timestamp("2026-07-02T10:00:00-04:00")
    ts2 = pd.Timestamp("2026-07-02T10:05:00-04:00")
    new = [
        {"ts": ts1, "action": "buy", "price": 100.0, "reason": "cross_up", "strategy": "ema"},
        {"ts": ts2, "action": "buy", "price": 101.0, "reason": "rsi_bb_long", "strategy": "bollinger_rsi"},
    ]

    class BlockingOM(FakeOM):
        def handle(self, payload):
            if not self.calls:
                self.calls.append({"ticker": payload.ticker, "action": payload.action})
                raise rf.RiskError("blocked: flat bias")
            return super().handle(payload)

    client = FakeClient(equity=100000.0, positions=[])
    om = BlockingOM()
    fired = eng._fire_new_events("AAPL", new, LEGACY_CFG, client, om, FakeSettings(), dry_run=False)
    assert fired[0]["status"] == "blocked"
    assert fired[1]["status"] == "submitted_market"
