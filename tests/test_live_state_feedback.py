"""The feedback the research agent gets about its own decisions.

Two additions, both aimed at the same failure: research sets bias a few times a
day, the engine fires on any 1h bar, and a buy refused by the gate is
edge-triggered and never retried. On 2026-07-29 that lost an MSFT entry that ran
+15.51% the next session, and nothing in the loop noticed — because the agent was
never shown what its own bias had blocked, and judged "is it close to firing?" by
eye rather than from the engine's gates.

`blocked_signals` closes the first half, `_near_trigger` the second. Both are
pure functions over data the engine already writes, so they are tested here.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.live_state import _near_trigger, _previous_run, blocked_signals


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _journal(tmp_path, rows):
    p = tmp_path / "trades.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


# --------------------------------------------------------------------------
# what the gate refused
# --------------------------------------------------------------------------
def test_only_refusals_since_the_cursor_are_reported(tmp_path):
    old = (NOW - timedelta(hours=30)).timestamp()
    recent = (NOW - timedelta(hours=2)).timestamp()
    j = _journal(tmp_path, [
        {"ts": old, "event": "risk_block", "ticker": "SOXL", "action": "buy", "qty": 41,
         "reason": "SOXL bias=flat, blocking new buy entry"},
        {"ts": recent, "event": "risk_block", "ticker": "MSFT", "action": "buy", "qty": 12,
         "reason": "MSFT bias=flat, blocking new buy entry"},
        {"ts": recent, "event": "order_ok", "ticker": "GOOG", "action": "buy"},
    ])
    out = blocked_signals(since=NOW - timedelta(hours=3), journal=j)
    assert out["count"] == 1
    assert out["events"][0]["symbol"] == "MSFT"
    assert "bias=flat" in out["events"][0]["reason"]


def test_both_journal_timestamp_shapes_are_understood(tmp_path):
    """trades.jsonl carries a float epoch on order rows and an ISO bar time on
    signal rows; a risk_block written either way must still be reported."""
    j = _journal(tmp_path, [
        {"ts": (NOW - timedelta(hours=1)).timestamp(), "event": "risk_block",
         "ticker": "AMZN", "action": "buy", "reason": "AMZN bias=flat"},
        {"ts": (NOW - timedelta(hours=1)).isoformat(), "event": "risk_block",
         "ticker": "TSLA", "action": "buy", "reason": "TSLA bias=flat"},
        {"ts": "2026-07-31 11:00:00", "event": "risk_block",      # naive, no tz
         "ticker": "AAPL", "action": "buy", "reason": "AAPL bias=flat"},
    ])
    got = {e["symbol"] for e in blocked_signals(since=NOW - timedelta(hours=3), journal=j)["events"]}
    assert got == {"AMZN", "TSLA", "AAPL"}


def test_rotation_blocks_are_reported_too(tmp_path):
    """Not every refusal is a flat bias — 'holding X (pri=4) >= Y' is also a
    trade the engine wanted and did not get."""
    j = _journal(tmp_path, [
        {"ts": (NOW - timedelta(minutes=30)).timestamp(), "event": "risk_block",
         "ticker": "NVDA", "action": "buy", "qty": 25,
         "reason": "holding META (pri=4) >= NVDA (pri=2); not rotating"},
    ])
    out = blocked_signals(since=NOW - timedelta(hours=1), journal=j)
    assert out["count"] == 1 and "not rotating" in out["events"][0]["reason"]


def test_garbage_lines_do_not_break_the_feed(tmp_path):
    p = tmp_path / "trades.jsonl"
    p.write_text('{"broken\n' + json.dumps(
        {"ts": (NOW - timedelta(minutes=5)).timestamp(), "event": "risk_block",
         "ticker": "MSFT", "action": "buy", "reason": "MSFT bias=flat"}) + "\n"
        + json.dumps({"ts": "not-a-time", "event": "risk_block", "ticker": "X"}) + "\n")
    assert blocked_signals(since=NOW - timedelta(hours=1), journal=p)["count"] == 1


def test_a_missing_journal_is_reported_not_raised(tmp_path):
    out = blocked_signals(since=NOW, journal=tmp_path / "nope.jsonl")
    assert out["events"] == [] and "not found" in out["note"]


def test_the_note_tells_the_agent_a_block_is_final(tmp_path):
    """The agent must know a refused signal is gone, not queued — that is the
    whole reason a contingency has to be written in ADVANCE rather than at the
    next refresh."""
    note = blocked_signals(since=NOW, journal=_journal(tmp_path, []))["note"]
    assert "not retried" in note.lower()


# --------------------------------------------------------------------------
# what is about to fire
# --------------------------------------------------------------------------
def _macd(gates_pass=True, above_zero=False, rising=True, hist=-0.9):
    return {"macd_sma": {"gates_all_pass": gates_pass, "hist_above_zero": above_zero,
                         "hist_rising": rising, "hist": hist}}


def _bb(rsi=48.0, d50=2.0, pct=0.8):
    return {"bollinger_rsi": {"rsi": rsi, "rsi_distance_to_50": d50,
                              "pct_close_to_lower": pct}}


def test_macd_arm_is_armed_when_the_gates_pass_and_the_histogram_is_rising():
    n = _near_trigger(_macd())
    assert n and "macd_sma" in n["why"][0]


def test_macd_arm_is_not_armed_when_a_gate_fails():
    """A zero-cross fires NOTHING while a gate is false, however close the
    histogram looks — the exact mistake an eyeballed chart makes."""
    assert _near_trigger(_macd(gates_pass=False)) is None


@pytest.mark.parametrize("kw", [{"above_zero": True}, {"rising": False}])
def test_macd_arm_is_not_armed_once_it_has_crossed_or_is_falling(kw):
    assert _near_trigger(_macd(**kw)) is None


def test_bollinger_arm_needs_both_legs_close():
    assert _near_trigger(_bb(d50=2.0, pct=0.8)) is not None
    assert _near_trigger(_bb(d50=12.0, pct=0.8)) is None      # RSI far from 50
    assert _near_trigger(_bb(d50=2.0, pct=6.0)) is None       # price far from the band


def test_a_quiet_symbol_is_not_flagged():
    assert _near_trigger({**_macd(gates_pass=False), **_bb(d50=20.0, pct=9.0)}) is None


def test_both_arms_armed_reports_both():
    n = _near_trigger({**_macd(), **_bb()})
    assert n and len(n["why"]) == 2


def test_missing_or_errored_arms_are_survivable():
    assert _near_trigger({}) is None
    assert _near_trigger({"macd_sma": {"error": "insufficient bars"}}) is None


# --------------------------------------------------------------------------
# the cursor
# --------------------------------------------------------------------------
def test_cursor_comes_from_the_previous_runs_timestamp(tmp_path):
    p = tmp_path / "live_state.json"
    p.write_text(json.dumps({"generated_at": "2026-07-31T09:00:00+00:00"}))
    assert _previous_run(p) == datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)


def test_a_naive_timestamp_is_read_as_utc(tmp_path):
    p = tmp_path / "live_state.json"
    p.write_text(json.dumps({"generated_at": "2026-07-31T09:00:00"}))
    assert _previous_run(p).tzinfo is timezone.utc


@pytest.mark.parametrize("body", ["", "not json", '{"generated_at": "nope"}', "{}"])
def test_an_unusable_previous_run_falls_back_rather_than_raising(tmp_path, body):
    p = tmp_path / "live_state.json"
    p.write_text(body)
    assert _previous_run(p) is None       # caller then uses the fixed lookback
