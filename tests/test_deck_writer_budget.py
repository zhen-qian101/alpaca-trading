"""write_deck: surviving a reasoning pass that eats the answer's token budget.

THE FAILURE THIS PINS. On 2026-08-19 the post-market recap produced no video at
all. The render reached the deck writer, all four attempts returned empty
content, `write_deck` raised SystemExit, and `set -euo pipefail` took the whole
run down with it — four minutes in, six hours before anyone noticed.

The cause was not the schema and not the prompt. ollama charges `thinking`
against the SAME num_predict budget as the answer, so a long reasoning pass
reaches the ceiling and the JSON is never emitted: done_reason="length" with
content "". Measured against the real 2026-08-19 deck the following day:

    think=medium/low, num_predict=16384  ->  2 of 4 EMPTY   (the shipped bug)
    think=medium/low, num_predict=32000  ->  6 of 6 OK
    think=False,      num_predict=16384  ->  3 of 3 OK, ~10x faster

The important measurement is the one that kills the obvious fix: the think
LEVEL does not bound reasoning on the cloud path. A think="low" trial spent
76,377 chars of thinking, MORE than a think="medium" one at 64,727. So the old
ladder's medium->medium->low->low was degrading a knob with no effect, which is
exactly why it could fail four times in a row rather than converging.

These tests pin the shape of the fix rather than the numbers: the ladder must
eventually stop asking the model to think, every rung must carry real headroom,
and a budget overrun must SAY it is a budget overrun.
"""
from __future__ import annotations

import io
import json

import pytest

from briefing import host_briefing


SLIDES = [{"id": "OVERVIEW", "note": "the tape"}, {"id": "TSLA", "note": "+4.23%"}]
# keyframe_desc is required: there is no fallback description of anyone's plate.
HOST = {"name": "Rain Chen",
        "keyframe_desc": "He is standing against a plain studio wall."}
CFG = {"director": {"model": "deepseek-v4.1-flash:cloud"}}


def _deck(n: int) -> dict:
    """A well-formed answer for `n` slides — every field the validator wants."""
    return {"slides": [
        {"slide": s["id"], "line": "words", "mood": "steady",
         "expression": "level", "gestures": "still", "body": "planted"}
        for s in SLIDES[:n]]}


#: The exact ollama response the 2026-08-19 render got, four times over: the
#: reasoning ran to the ceiling and nothing was left for the answer.
BUDGET_OVERRUN = {"message": {"content": "", "thinking": "x" * 53991},
                  "done_reason": "length", "eval_count": 16384}


class _Ollama:
    """Scripted /api/chat: pops one canned reply per call, records the requests.

    A reply that is already a full response envelope (it has "done_reason") is
    passed through untouched, so a test can hand back the real overrun shape.
    """

    def __init__(self, monkeypatch, replies):
        self.replies, self.requests = list(replies), []
        monkeypatch.setattr(host_briefing.urllib.request, "urlopen", self)

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        body = r if "done_reason" in r else {"message": {"content": json.dumps(r)},
                                             "done_reason": "stop"}
        return io.BytesIO(json.dumps(body).encode())


def _write(monkeypatch, replies, **kw):
    ol = _Ollama(monkeypatch, replies)
    return ol, host_briefing.write_deck(SLIDES, HOST, CFG, **kw)


# ---------------------------------------------------------------- the fix ---
def test_the_ladder_stops_asking_the_model_to_think(monkeypatch):
    """THE regression. Four consecutive budget overruns used to be terminal;
    they must now be impossible, because the ladder turns thinking OFF before it
    runs out of attempts. If every rung still reasoned, this deck would die."""
    ol = _Ollama(monkeypatch, [BUDGET_OVERRUN, _deck(2)])
    got = host_briefing.write_deck(SLIDES, HOST, CFG)

    assert [g["slide"] for g in got] == ["OVERVIEW", "TSLA"]
    assert ol.requests[1]["think"] is False, (
        "the rung after the reasoned one must stop reasoning — degrading "
        "medium->low is what failed on 2026-08-19, because the level does not "
        "bound the reasoning")


def test_only_one_attempt_is_ever_spent_on_reasoning(monkeypatch):
    """Measured in production 2026-08-20: on the real deck BOTH reasoned rungs
    lost the same race, in both languages (108,849 and 101,897 chars of
    thinking). A second reasoned attempt is not a retry, it is a wasted minute —
    so at most one rung may reason."""
    ol = _Ollama(monkeypatch, [BUDGET_OVERRUN] * 3 + [_deck(2)])
    host_briefing.write_deck(SLIDES, HOST, CFG)

    reasoned = [r for r in ol.requests if r["think"] is not False]
    assert len(reasoned) == 1, f"{len(reasoned)} rungs reason; only one may"


def test_no_rung_ever_reasons_without_headroom(monkeypatch):
    """Every attempt must carry a budget bigger than the 16384 that lost the
    race with a 53k-char thinking pass. Pinned as a floor, not an equality, so
    raising the budget later is not a test failure."""
    ol = _Ollama(monkeypatch, [BUDGET_OVERRUN] * 3 + [_deck(2)])
    host_briefing.write_deck(SLIDES, HOST, CFG)

    assert len(ol.requests) == 4
    for i, r in enumerate(ol.requests):
        assert r["options"]["num_predict"] > 16384, f"rung {i+1} has no headroom"


def test_thinking_is_off_for_at_least_the_last_two_rungs(monkeypatch):
    """The deterministic path must be reachable more than once — one flaky
    network call on the third rung must not put us back on the reasoning path
    for the final attempt."""
    ol = _Ollama(monkeypatch, [BUDGET_OVERRUN] * 3 + [_deck(2)])
    host_briefing.write_deck(SLIDES, HOST, CFG)

    assert [r["think"] for r in ol.requests][-2:] == [False, False]


def test_the_reasoned_deck_is_still_tried_first(monkeypatch):
    """think=False is the fallback, NOT the new default: it is ~10x faster but
    the reasoned deck is the better-written one, and this pipeline is judged on
    the writing. A single call must still be a thinking call."""
    ol = _Ollama(monkeypatch, [_deck(2)])
    host_briefing.write_deck(SLIDES, HOST, CFG)

    assert len(ol.requests) == 1
    assert ol.requests[0]["think"] == "medium"


# -------------------------------------------------------- the diagnostics ---
def test_a_budget_overrun_says_so(monkeypatch, capsys):
    """The 2026-08-19 log blamed the schema four times over — json.loads('')
    reports 'Expecting value: line 1 column 1 (char 0)', which sends the reader
    to the wrong file. The message must name the token ceiling instead."""
    _Ollama(monkeypatch, [BUDGET_OVERRUN, _deck(2)])
    host_briefing.write_deck(SLIDES, HOST, CFG)

    msg = capsys.readouterr().out
    assert "Expecting value" not in msg
    assert "thinking" in msg and "budget" in msg
    assert "53991" in msg, "the size of the reasoning pass is the evidence"


def test_an_empty_answer_that_did_not_hit_the_ceiling_is_named_too(monkeypatch,
                                                                   capsys):
    """The zh deck on the 2026-08-20 re-run returned empty content with
    done_reason="stop" — not the token ceiling, just nothing at all. The old
    parse error hid the difference; the message must distinguish them, because
    "spend fewer tokens thinking" is not the fix for this one."""
    _Ollama(monkeypatch, [{"message": {"content": "", "thinking": "x" * 40},
                           "done_reason": "stop", "eval_count": 900}, _deck(2)])
    host_briefing.write_deck(SLIDES, HOST, CFG)

    msg = capsys.readouterr().out
    assert "Expecting value" not in msg
    assert "returned nothing" in msg and "budget" not in msg


def test_a_genuinely_broken_answer_still_reports_normally(monkeypatch, capsys):
    """The overrun branch must not swallow ordinary malformed output: an empty
    answer that did NOT hit the ceiling is a different bug and keeps its own
    error."""
    _Ollama(monkeypatch, [{"message": {"content": "not json"},
                           "done_reason": "stop"}, _deck(2)])
    host_briefing.write_deck(SLIDES, HOST, CFG)

    out = capsys.readouterr().out
    assert "budget" not in out and "attempt 1" in out


# ------------------------------------------------------ unchanged contract ---
def test_a_short_deck_is_rejected_not_padded(monkeypatch):
    """Pre-existing guard, re-pinned because the retry ladder moved underneath
    it: a deck with the wrong slide count must be retried, never accepted."""
    ol = _Ollama(monkeypatch, [_deck(1), _deck(2)])
    got = host_briefing.write_deck(SLIDES, HOST, CFG)

    assert len(got) == 2 and len(ol.requests) == 2


def test_all_four_rungs_failing_is_still_fatal(monkeypatch):
    """The render must not limp on with no narration. Four failures remain a
    hard stop — the fix reduces how often that happens, it does not paper over
    it with a fabricated deck."""
    _Ollama(monkeypatch, [BUDGET_OVERRUN] * 4)
    with pytest.raises(SystemExit):
        host_briefing.write_deck(SLIDES, HOST, CFG)
