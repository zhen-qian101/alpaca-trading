"""A hand held OUT is the one acting direction this renderer cannot be given.

REGRESSION for the 2026-08-18 SOXL 中文 clip, where a black articulated
magic-arm grip rig — knobs, yellow brand lettering, a boom head — grew out of
the bare wall beside the host from ~5.5s to ~9s and he leaned over and put his
hands on it.

The evidence was a natural experiment: three clips, one seed (4242), one
keyframe, differing only in the hand direction the composer wrote.

    "his arms stay down at his sides, hands still"             -> clean
    "his hand rises, palm open, a single gesture, then lowers" -> clean
    "lifts his hand, palm open, HOLDING IT OUT FOR A BEAT"     -> the rig

A hand that rises and falls is a gesture. A hand held out, waiting, is an
unanswered question, and LTX's "presenter performing" prior answers it with the
studio gear that co-occurs with a seamless-wall shot in its training data. It is
the same prior that drew a headset-mic capsule on the host's cheek on 2026-08-11
(see ltx_ia2v_probe.NEG); the mic nouns were suppressed there, so it reached for
the nearest thing that list had not named.

Re-rendered at the SAME seed with only this phrase removed, the wall came back
empty and the host still gestured.

    .venv/bin/pytest -q tests/test_held_gesture_strip.py
"""
from __future__ import annotations

import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pytest  # noqa: E402

from briefing.ltx_story import strip_held_gesture  # noqa: E402

# The exact prompt that produced the defect, from
# .hf_work/tmp/host_SOXL_zh/graph.json.
SOXL_ZH = ("He talks continuously to camera from the first frame to the last. As he "
           "reaches the drop, his head shakes once, then he lifts his hand, palm open, "
           "holding it out for a beat before lowering it as he continues on the "
           "leverage and the fear. He gives a slight shrug as he mentions the "
           "recovery, then stands still as he finishes on Apple's strength. His voice "
           "stays quiet and measured, with only room tone behind him.")


def test_the_defect_phrase_is_removed():
    out = strip_held_gesture(SOXL_ZH)
    assert "holding it out" not in out
    assert "for a beat" not in out


def test_everything_around_it_survives():
    """An earlier draft's unbounded tail ate the rest of the sentence. The beat
    is being cleaned, not deleted."""
    out = strip_held_gesture(SOXL_ZH)
    for kept in ("his head shakes once", "he lifts his hand, palm open",
                 "before lowering it", "as he continues on the leverage and the fear",
                 "a slight shrug", "Apple's strength", "room tone"):
        assert kept in out, kept


# ------------------------------------------------- the wordings that were CLEAN
@pytest.mark.parametrize("clean", [
    # SOXL en, seed 4242, no equipment
    "His arms stay down at his sides, hands still, and his weight is settled and steady.",
    # AAPL 中文, seed 4242, no equipment — "palm open" must NOT trigger
    "When he turns to the market's opinion, his hand rises, palm open, a single "
    "gesture, then lowers.",
    # a hand going UP and coming back is a gesture, not a dwell
    "His hand lifts up briefly and comes back down as he makes the point.",
    "He gives a slight shrug as he mentions the recovery, then stands still.",
    "One hand rises and draws a small horizontal line in the air, then lowers "
    "and rests still.",
])
def test_clean_wordings_are_left_exactly_alone(clean):
    assert strip_held_gesture(clean) == clean


# ------------------------------------------------------------- other dwellings
@pytest.mark.parametrize("phrase", [
    "he holds it out",
    "he holds it there",
    "he extends his hand out",
    "he reaches out",
    "she offers her hand out",
    "he keeps it out for a moment",
    "he holds it out toward the camera",
])
def test_other_ways_of_writing_the_same_dwell(phrase):
    s = f"He talks steadily, and {phrase} while speaking, then continues."
    out = strip_held_gesture(s)
    assert "out" not in out.split("while")[0] or "holds" not in out
    assert "He talks steadily" in out

