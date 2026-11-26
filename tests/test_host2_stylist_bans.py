"""The stylist's wardrobe bans must read negation.

The system prompt hands the model host2.WARDROBE as its worked example, and that
string is itself a list of negations — "completely plain with no logo, no
graphic, no print, no embroidery, no lettering", "bare wrists, no watch, no
bracelet, no necklace and no jewellery of any kind". The model copies the
register, so a WELL-BEHAVED outfit is dense with banned words, every one of them
negated. A plain substring test rejected 6 of 6 fabricated weather days on the
first run and the stylist could never return a look at all.

What is actually banned is a garment that HAS the thing. The window width is the
whole trick and it is wrong in both directions: too wide reaches back across a
comma and reads "...with no logo, and a navy baseball cap" as negated, missing a
real cap; too narrow breaks shared negations like "no logo, graphic or
lettering". A false positive costs one rung of the retry ladder. A false
negative puts a cap on his head and hides the crew cut, which is an identity
anchor.
"""
from __future__ import annotations

import pytest

from briefing.host2 import WARDROBE
from briefing.host2_stylist import _BANNED, _affirmative_hits


def flagged(text: str) -> dict:
    out = {}
    for kind, words in _BANNED.items():
        hit = _affirmative_hits(text, words)
        if hit:
            out[kind] = hit
    return out


@pytest.mark.parametrize("text", [
    WARDROBE,
    "wearing a plain oatmeal lambswool crew-neck sweater, completely plain with "
    "no logo, no graphic and no lettering, over straight-leg jeans, bare "
    "wrists, no watch, no bracelet and no jewellery of any kind",
    "wearing a charcoal cotton hoodie with the hood down on his shoulders, no "
    "logo and no print, over faded indigo jeans",
    "a stone linen overshirt, open, with no logo, graphic or lettering",
])
def test_a_correctly_negated_outfit_passes(text):
    assert flagged(text) == {}


@pytest.mark.parametrize("text,kind", [
    ("a navy sweatshirt with no logo, and a navy baseball cap", "headwear"),
    ("a grey sweatshirt with a small embroidered logo on the chest", "graphics"),
    ("a stone overshirt over a white tee, with white leather sneakers", "footwear"),
    ("an olive jacket, no logo, and a steel watch on his left wrist", "jewellery"),
    ("a charcoal sweater, no print, worn with a wool beanie", "headwear"),
])
def test_a_real_violation_is_caught(text, kind):
    assert kind in flagged(text), f"missed the {kind} in: {text}"


def test_the_worked_example_would_not_reject_itself():
    """The regression that broke the first run end to end: the example the model
    is told to imitate must itself be a passing answer."""
    assert flagged(WARDROBE) == {}
