"""Tests for agent.decisions_schema's contingency clamping (2026-07-07).

Contingencies let the research agent pre-authorize a specific price level that
would override a flat/short bias (see relay/risk_filter.py's module docstring
for the full mechanism). This validator is the last line of defense against a
malformed or over-permissive contingency reaching the live risk gate — every
clamp here has a matching "what if the LLM got this wrong" scenario.

    .venv/bin/pytest -q tests/test_decisions_schema.py
"""
from __future__ import annotations

import datetime as dt

from agent.decisions_schema import HARD_MAX_CONTINGENCY_HOURS, validate_decision


def _iso(hours):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat()


def _base(contingencies):
    return {
        "watchlist": ["AAPL"],
        "bias": {"AAPL": "flat"},
        "contingencies": contingencies,
    }


def test_well_formed_contingency_survives_validation():
    d = validate_decision(_base({"AAPL": [{
        "condition": "close_above", "level": 210.0, "action": "long",
        "size_cap": 0.25, "valid_until": _iso(1), "reason": "breakout",
    }]}))
    assert len(d["contingencies"]["AAPL"]) == 1
    kept = d["contingencies"]["AAPL"][0]
    assert kept["condition"] == "close_above"
    assert kept["level"] == 210.0
    assert "size_cap" not in kept, (
        "a contingency PERMITS an entry; it does not resize one. This book is "
        "all-in/all-out, so the key is stripped rather than carried through.")


def test_contingency_for_symbol_not_on_watchlist_is_dropped():
    d = validate_decision(_base({"MSFT": [{
        "condition": "close_above", "level": 400.0, "action": "long",
        "valid_until": _iso(1), "reason": "not on watchlist",
    }]}))
    assert d["contingencies"] == {}


def test_already_expired_contingency_is_dropped():
    d = validate_decision(_base({"AAPL": [{
        "condition": "close_above", "level": 210.0, "action": "long",
        "valid_until": _iso(-1), "reason": "expired",
    }]}))
    assert d["contingencies"] == {}


def test_valid_until_beyond_hard_max_is_clamped_not_dropped():
    d = validate_decision(_base({"AAPL": [{
        "condition": "close_above", "level": 210.0, "action": "long",
        "valid_until": _iso(24 * 30), "reason": "way too long"
    }]}))
    kept = d["contingencies"]["AAPL"][0]
    valid_until = dt.datetime.fromisoformat(kept["valid_until"])
    max_allowed = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=HARD_MAX_CONTINGENCY_HOURS)
    assert valid_until <= max_allowed + dt.timedelta(seconds=5)


def test_missing_valid_until_is_dropped():
    d = validate_decision(_base({"AAPL": [{
        "condition": "close_above", "level": 210.0, "action": "long",
        "reason": "no expiry given",
    }]}))
    assert d["contingencies"] == {}


def test_unsupported_condition_is_dropped():
    d = validate_decision(_base({"AAPL": [{
        "condition": "rsi_below", "level": 30.0, "action": "long",
        "valid_until": _iso(1), "reason": "not a supported condition",
    }]}))
    assert d["contingencies"] == {}


def test_action_other_than_long_is_dropped():
    d = validate_decision(_base({"AAPL": [{
        "condition": "close_above", "level": 210.0, "action": "short",
        "valid_until": _iso(1), "reason": "only long overrides are supported",
    }]}))
    assert d["contingencies"] == {}


def test_size_cap_is_stripped_however_it_arrives():
    """It used to be clamped to 0..1 and then APPLIED by order_manager, which
    made a pre-authorised breakout enter at a fraction of size — a different
    power from the one a contingency is for. The model may still emit it; the
    rule is that it never reaches the order path (operator directive
    2026-08-27: every entry is all-in)."""
    for cap in (5.0, 0.35, 0.0, "nonsense"):
        d = validate_decision(_base({"AAPL": [{
            "condition": "close_above", "level": 210.0, "action": "long",
            "size_cap": cap, "valid_until": _iso(1), "reason": "oversized cap",
        }]}))
        assert "size_cap" not in d["contingencies"]["AAPL"][0]


def test_suggested_size_is_derived_from_bias_not_taken_from_the_model():
    """The field is a READOUT of what the engine will really deploy, not an
    input. It was a fiction for months: nothing in the trade path read it while
    the dashboard and the video briefing both showed it, so the one number a
    human saw for position size disagreed with reality by more than 10x."""
    from agent.decisions_schema import deploy_fraction
    d = validate_decision({
        "watchlist": ["AAPL", "MSFT"],
        "bias": {"AAPL": "long", "MSFT": "flat"},
        "suggested_size": {"AAPL": 0.08, "MSFT": 0.42},
    })
    assert d["suggested_size"]["AAPL"] == deploy_fraction("AAPL")
    assert d["suggested_size"]["MSFT"] == 0.0


def test_missing_level_is_dropped():
    d = validate_decision(_base({"AAPL": [{
        "condition": "close_above", "action": "long",
        "valid_until": _iso(1), "reason": "no level given",
    }]}))
    assert d["contingencies"] == {}


def test_non_dict_contingencies_value_becomes_empty_dict():
    d = validate_decision(_base(["not", "a", "dict"]))
    assert d["contingencies"] == {}


def test_missing_contingencies_key_defaults_to_empty_dict():
    d = validate_decision({"watchlist": ["AAPL"], "bias": {"AAPL": "flat"}})
    assert d["contingencies"] == {}
