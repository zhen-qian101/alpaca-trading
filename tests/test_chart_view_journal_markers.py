"""Account-level exits (rotation_close / reconcile_close) must be drawn on the
chart even though no strategy recompute can produce them — the 2026-08-03
GOOG->AMZN rotation left the account flat while the GOOG chart showed the
position still open. See backtest.chart_view._journal_exit_markers.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

import backtest.chart_view as cv


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / "trades.jsonl"
    monkeypatch.setattr(cv, "JOURNAL_FILE", path)

    def write(events):
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    return write


# Hourly UTC bars spanning the rotation timestamp used below.
INDEX = pd.date_range("2026-08-03 00:00", periods=24, freq="1h", tz="UTC")
ROTATION_TS = pd.Timestamp("2026-08-03 08:03:58", tz="UTC").timestamp()


def test_rotation_close_is_drawn_snapped_to_containing_bar(journal):
    journal([{"ts": ROTATION_TS, "event": "rotation_close", "ticker": "GOOG",
              "new_pick": "AMZN", "reason": "rotate GOOG(pri=3) -> AMZN(pri=4)"}])
    markers = cv._journal_exit_markers("GOOG", INDEX, INDEX[0])
    assert len(markers) == 1
    m = markers[0]
    # Snapped DOWN to the 08:00 bar, not 09:00 (and rendered in Melbourne time
    # via _chart_ts, same convention as every other marker).
    assert m["time"] == cv._chart_ts(INDEX[8])
    assert m["shape"] == "arrowDown" and m["position"] == "aboveBar"
    assert m["text"] == "ROT>AMZN"


def test_other_symbols_and_unrelated_events_are_ignored(journal):
    journal([
        {"ts": ROTATION_TS, "event": "rotation_close", "ticker": "GOOG", "new_pick": "AMZN"},
        {"ts": ROTATION_TS, "event": "order_ok", "ticker": "AMZN", "action": "buy"},
    ])
    assert cv._journal_exit_markers("AMZN", INDEX, INDEX[0]) == []


def test_voided_events_are_not_drawn(journal):
    """Journal hygiene: corrections append and void by ts — a voided
    reconcile_close (e.g. one written by a test run against the live journal)
    must never reach the chart."""
    journal([
        {"ts": ROTATION_TS, "event": "reconcile_close", "ticker": "NVDA", "qty": 5},
        {"ts": ROTATION_TS + 60, "event": "correction", "voids_ts": [ROTATION_TS],
         "reason": "not a real trading event"},
    ])
    assert cv._journal_exit_markers("NVDA", INDEX, INDEX[0]) == []


def test_events_before_window_or_before_first_bar_are_dropped(journal):
    journal([
        {"ts": ROTATION_TS, "event": "rotation_close", "ticker": "GOOG", "new_pick": "AMZN"},
        {"ts": INDEX[0].timestamp() - 3600, "event": "rotation_close",
         "ticker": "GOOG", "new_pick": "MSFT"},
    ])
    # Window starts after the rotation bar -> nothing to draw.
    assert cv._journal_exit_markers("GOOG", INDEX, INDEX[10]) == []
    # Full window -> only the in-range event (the pre-history one is dropped).
    markers = cv._journal_exit_markers("GOOG", INDEX, INDEX[0])
    assert [m["text"] for m in markers] == ["ROT>AMZN"]


def test_legacy_string_timestamps_and_junk_lines_survive(journal, tmp_path):
    path = tmp_path / "trades.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-08-03 08:03:58+00:00", "event": "reconcile_close",
                    "ticker": "GOOG", "qty": 14}) + "\nnot json at all\n")
    markers = cv._journal_exit_markers("GOOG", INDEX, INDEX[0])
    assert len(markers) == 1 and markers[0]["text"] == "RECONCILE"


def test_missing_journal_means_no_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "JOURNAL_FILE", tmp_path / "nope.jsonl")
    assert cv._journal_exit_markers("GOOG", INDEX, INDEX[0]) == []


# ------------------------------------------------- rejected closes are not exits
RECON_TS = pd.Timestamp("2026-08-03 08:03:58", tz="UTC").timestamp()


def test_reconcile_close_followed_by_a_rejection_draws_no_arrow(journal):
    """Until 2026-08-19 the engine journaled reconcile_close BEFORE trying the
    order, so a broker rejection left an arrow behind exactly like a real exit.
    ~230 such rows are already on disk (TSLA 115, AAPL 109) and the chart reads
    the journal from the beginning, so they still have to be filtered here."""
    journal([
        {"ts": RECON_TS, "event": "reconcile_close", "ticker": "TSLA", "qty": 13.0},
        {"ts": RECON_TS + 9, "event": "order_error", "ticker": "TSLA",
         "action": "close", "reason": '{"code":40310000,"message":"insufficient qty"}'},
    ])
    assert cv._journal_exit_markers("TSLA", INDEX, INDEX[0]) == []


def test_reconcile_close_followed_by_a_risk_block_draws_no_arrow(journal):
    """The 60s dedup rejects the first duplicate close of a divergence."""
    journal([
        {"ts": RECON_TS, "event": "reconcile_close", "ticker": "TSLA", "qty": 13.0},
        {"ts": RECON_TS, "event": "risk_block", "ticker": "TSLA", "action": "close",
         "reason": "duplicate TSLA close within 60s"},
    ])
    assert cv._journal_exit_markers("TSLA", INDEX, INDEX[0]) == []


def test_a_close_that_reached_the_broker_still_draws(journal):
    """The whole point of the filter is to keep the REAL exit. A failure far
    outside the window belongs to a different attempt entirely."""
    journal([
        {"ts": RECON_TS, "event": "reconcile_close", "ticker": "TSLA", "qty": 13.0},
        {"ts": RECON_TS + 2, "event": "order_ok", "ticker": "TSLA", "action": "close"},
        {"ts": RECON_TS + 3600, "event": "order_error", "ticker": "TSLA",
         "action": "close", "reason": "much later, unrelated"},
    ])
    markers = cv._journal_exit_markers("TSLA", INDEX, INDEX[0])
    assert len(markers) == 1 and markers[0]["text"] == "RECONCILE"


def test_a_buy_rejection_does_not_suppress_an_exit_arrow(journal):
    """Only a failed CLOSE can be the failure of a journaled exit."""
    journal([
        {"ts": RECON_TS, "event": "reconcile_close", "ticker": "TSLA", "qty": 13.0},
        {"ts": RECON_TS + 5, "event": "order_error", "ticker": "TSLA", "action": "buy"},
    ])
    assert len(cv._journal_exit_markers("TSLA", INDEX, INDEX[0])) == 1


def test_repeated_exits_in_one_bar_collapse_to_a_single_arrow(journal):
    """The pass runs every poll while a divergence lasts, so an hourly bar can
    collect a dozen; one arrow per bar is all the chart can show."""
    journal([{"ts": RECON_TS + 60 * i, "event": "reconcile_close",
              "ticker": "TSLA", "qty": 13.0} for i in range(8)])
    markers = cv._journal_exit_markers("TSLA", INDEX, INDEX[0])
    assert len(markers) == 1
    assert markers[0]["time"] == cv._chart_ts(INDEX[8])
