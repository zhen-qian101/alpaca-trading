"""Tests for agent.live_state — the precomputed trigger state the research agent
reads because it has no Bash and cannot compute indicators itself.

The whole point of this module is that research and the engine judge triggers with
the SAME math. So the tests that matter are parity tests: if live_state's gate
logic ever drifts from backtest.macd_sma._signals / backtest.bollinger_rsi._signals,
research goes back to ranking on numbers the engine does not trade — the exact bug
this was built to kill.

    .venv/bin/pytest -q tests/test_live_state.py
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import agent.live_state as ls  # noqa: E402
import relay.config as _relay_config  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_journal(monkeypatch, tmp_path):
    """Point the journal at an empty tmp file for EVERY test in this module.

    build_state() calls blocked_signals(), which falls through to
    relay.config.JOURNAL_FILE — the real, live trade journal on /mnt/sandisk.
    Five tests here monkeypatched CONFIG_FILE and read_bars but not that, so
    their input was whatever the account had actually done in the last 24h
    (blocked_signals' lookback). They passed or failed depending on the wall
    clock: at the time this was found, the newest risk_block was 22.7h old, so
    in ~1.3h the same test would have seen a different result with no code
    change. A test that reads live production state is not a test.
    """
    monkeypatch.setattr(_relay_config, "JOURNAL_FILE", tmp_path / "trades.jsonl")

from backtest.bollinger_rsi import _signals as bb_signals  # noqa: E402
from backtest.macd_sma import _signals as macd_signals  # noqa: E402
from backtest.macd_sma import compute_indicators as macd_indicators  # noqa: E402

MACD_P = {"type": "macd_sma", "fast_len": 12, "slow_len": 35, "signal_len": 9,
          "veryslow_len": 200}
BB_P = {"type": "bollinger_rsi", "rsi_length": 6, "bb_length": 200, "bb_mult": 2.5}


def _series(n=900, seed=7):
    """A wandering series with enough structure to produce real crosses."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 1.0, n).cumsum()
    wave = 25 * np.sin(np.linspace(0, 14, n))
    return 300 + wave + steps


# ------------------------------------------------------------------ parity
def test_macd_gates_match_the_strategys_own_buy_condition():
    """backtest.macd_sma buys on:
        hist_co & (macd>0) & (fast_ma>slow_ma) & (close_lag>veryslow_ma)
    Wherever the strategy actually fires, live_state must report every gate as
    passing at that bar. If this fails, research is ranking on a trigger the
    engine will not honour."""
    close = _series()
    buy, _ = macd_signals(close, MACD_P["fast_len"], MACD_P["slow_len"],
                          MACD_P["signal_len"], MACD_P["veryslow_len"])
    fired = np.flatnonzero(buy)
    assert len(fired) > 0, "fixture produced no buys — test would be vacuous"

    for i in fired:
        st = ls._macd_sma_state(close[:i + 1], MACD_P)
        assert st["gates_all_pass"] is True, f"bar {i}: strategy fired but gates say no"
        assert st["hist_above_zero"] is True  # the cross just completed


def test_gates_all_pass_is_false_when_the_strategy_would_not_fire():
    """The inverse guard: a histogram sitting on zero must NOT read as ready
    while a gate is false — that is the nuance an eyeballed chart misses."""
    close = _series()
    ind = macd_indicators(close, MACD_P["fast_len"], MACD_P["slow_len"],
                          MACD_P["signal_len"], MACD_P["veryslow_len"])
    checked = 0
    for i in range(600, len(close)):
        st = ls._macd_sma_state(close[:i + 1], MACD_P)
        if st["gates_all_pass"] is False:
            # At least one of the three documented gates must genuinely be false.
            assert (not ind["macd"][i] > 0
                    or not ind["fast_ma"][i] > ind["slow_ma"][i]
                    or not close[i - MACD_P["slow_len"]] > ind["veryslow_ma"][i])
            checked += 1
    assert checked > 0, "no gated bars in fixture — test would be vacuous"


def test_lagged_close_gate_uses_the_lagged_close_not_the_current_one():
    """The strategy compares close[i - slow_len] against today's veryslow MA.
    Using today's close instead is the obvious mistake, and would silently make
    the gate wrong — pin it."""
    close = _series()
    i = len(close) - 1
    st = ls._macd_sma_state(close, MACD_P)
    assert st["lagged_close"] == pytest.approx(close[i - MACD_P["slow_len"]])
    assert st["lagged_close"] != pytest.approx(close[i])  # fixture makes these differ


def test_bollinger_rsi_state_matches_the_strategys_indicators():
    close = _series()
    st = ls._bollinger_rsi_state(close, BB_P)
    from backtest.bollinger_rsi import compute_indicators as bb_ind
    ind = bb_ind(close, BB_P["rsi_length"], BB_P["bb_length"], BB_P["bb_mult"])
    assert st["rsi"] == pytest.approx(ind["rsi"][-1])
    assert st["bb_lower"] == pytest.approx(ind["lower"][-1])
    assert st["rsi_above_50"] is bool(ind["rsi"][-1] > 50)


def _series_with_crash(n=900, seed=3):
    """BB(200, 2.5) is a very wide band — a random walk rarely pierces it, which
    would make the buy-parity test below vacuous. Drive price through the lower
    band with a sharp crash and snap-back, the exact shape this mean-reversion
    arm is built to catch."""
    rng = np.random.default_rng(seed)
    close = 300 + rng.normal(0, 0.6, n).cumsum()
    for start in (400, 640):                 # crash, then recover through the band
        close[start:start + 12] -= np.linspace(0, 45, 12)
        close[start + 12:start + 30] += np.linspace(0, 55, 18)
        close[start + 30:] += 10.0
    return close


def test_bollinger_buy_bars_have_both_conditions_reported():
    """A bb_rsi buy needs RSI>50 AND close above the lower band on the SAME bar —
    both together. live_state must report both as true wherever it fires."""
    close = _series_with_crash()
    buy, _ = bb_signals(close, BB_P["rsi_length"], BB_P["bb_length"], BB_P["bb_mult"])
    fired = np.flatnonzero(buy)
    assert len(fired) > 0, "fixture produced no bb_rsi buys — test would be vacuous"
    for i in fired[:10]:
        st = ls._bollinger_rsi_state(close[:i + 1], BB_P)
        assert st["rsi_above_50"] is True
        assert st["close_above_lower"] is True


# ------------------------------------------- params come from config, not defaults
def test_params_are_read_from_the_engine_config_not_textbook_defaults(monkeypatch, tmp_path):
    """The original bug: the agent assumed MACD 12/26/9, RSI 14, BB 20/2.0 while
    the engine trades 12/35/9, RSI 6, BB 200/2.5. live_state must echo the REAL
    params so the drift can never silently return."""
    cfg = {"symbols": {"META": {"enabled": True, "interval": "1h",
                                "strategies": [MACD_P, BB_P]}}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(ls, "CONFIG_FILE", p)
    close = _series()
    df = pd.DataFrame({"close": close},
                      index=pd.date_range("2026-01-01", periods=len(close), freq="1h", tz="UTC"))
    monkeypatch.setattr(ls, "read_bars", lambda s, i: df)

    st = ls.build_state(positions=None)["symbols"]["META"]
    assert st["macd_sma"]["params"]["slow_len"] == 35          # not 26
    assert st["bollinger_rsi"]["params"]["rsi_length"] == 6    # not 14
    assert st["bollinger_rsi"]["params"]["bb_length"] == 200   # not 20
    assert st["bollinger_rsi"]["params"]["bb_mult"] == 2.5     # not 2.0
    assert "NOT the EMA-based textbook MACD" in st["macd_sma"]["params"]["note"]


def test_real_config_is_what_ships(monkeypatch):
    """Guards against the config drifting away from what these tests pin."""
    cfg = json.loads(ls.CONFIG_FILE.read_text())
    strat = {s["type"]: s for s in cfg["symbols"]["META"]["strategies"]}
    assert strat["macd_sma"]["slow_len"] == 35
    assert strat["bollinger_rsi"]["rsi_length"] == 6
    assert strat["bollinger_rsi"]["bb_length"] == 200


# ----------------------------------------------------------- honesty on failure
def test_insufficient_bars_reports_an_error_not_a_number(monkeypatch, tmp_path):
    """A number computed from too little history is a lie — NaN indicators would
    otherwise be serialized as null and read as 'zero distance'."""
    cfg = {"symbols": {"META": {"enabled": True, "interval": "1h",
                                "strategies": [MACD_P, BB_P]}}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(ls, "CONFIG_FILE", p)
    close = _series(n=50)
    df = pd.DataFrame({"close": close},
                      index=pd.date_range("2026-01-01", periods=50, freq="1h", tz="UTC"))
    monkeypatch.setattr(ls, "read_bars", lambda s, i: df)

    st = ls.build_state(positions=None)["symbols"]["META"]
    assert "insufficient bars" in st["macd_sma"]["error"]
    assert "insufficient bars" in st["bollinger_rsi"]["error"]


def test_no_cached_bars_reports_an_error(monkeypatch, tmp_path):
    cfg = {"symbols": {"META": {"enabled": True, "interval": "1h", "strategies": [MACD_P]}}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(ls, "CONFIG_FILE", p)
    monkeypatch.setattr(ls, "read_bars", lambda s, i: pd.DataFrame({"close": []}))
    st = ls.build_state(positions=None)["symbols"]["META"]
    assert st["error"] == "no cached bars"


def test_position_fetch_failure_says_unknown_not_flat(monkeypatch):
    """An empty list means flat; a failure must NOT look like one. The agent
    weighs 'the currently-held name' — telling it 'flat' when we simply could
    not ask would invent a fact."""
    import relay.alpaca_client as ac
    monkeypatch.setattr(ac, "AlpacaClient",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")))
    got = ls._live_positions()
    assert got["held"] is None
    assert "UNKNOWN" in got["note"]
    assert "network down" in got["error"]


def test_disabled_symbols_are_skipped(monkeypatch, tmp_path):
    cfg = {"symbols": {"META": {"enabled": True, "interval": "1h", "strategies": []},
                       "ORCL": {"enabled": False, "interval": "1h", "strategies": []}}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(ls, "CONFIG_FILE", p)
    monkeypatch.setattr(ls, "read_bars", lambda s, i: pd.DataFrame(
        {"close": [1.0, 2.0]}, index=pd.date_range("2026-01-01", periods=2, freq="1h", tz="UTC")))
    st = ls.build_state(positions=None)
    assert "META" in st["symbols"] and "ORCL" not in st["symbols"]


def test_output_is_json_serializable(monkeypatch, tmp_path):
    """numpy floats and NaN both break json.dumps / produce invalid JSON."""
    cfg = {"symbols": {"META": {"enabled": True, "interval": "1h",
                                "strategies": [MACD_P, BB_P]}}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(ls, "CONFIG_FILE", p)
    close = _series()
    df = pd.DataFrame({"close": close},
                      index=pd.date_range("2026-01-01", periods=len(close), freq="1h", tz="UTC"))
    monkeypatch.setattr(ls, "read_bars", lambda s, i: df)
    text = json.dumps(ls.build_state(positions={"held": [], "count": 0}))
    assert "NaN" not in text  # json.dumps emits bare NaN, which is invalid JSON
    json.loads(text)
