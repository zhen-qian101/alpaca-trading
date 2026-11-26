"""The writer's and composer's prompts are written for no presenter in particular.

Who is speaking and every pronoun are $-slots that ltx_script.persona() fills
from the presenter file. Two things can go wrong with that, and both reach the
model silently:

  * a slot is misspelt, so the model is sent a literal "$sbuj";
  * a pronoun is typed into a template instead of a slot, so a he/him presenter
    is described as "she" in his own prompt.

regender() still runs over every prompt, for the text that is not a template
(the account ground truth, the presenter file's prose, the writer's beats), and
it would quietly paper over the second mistake. So these tests read the
templates with regender() switched off.
"""
from __future__ import annotations

import json
import re

import pytest

from briefing import host_briefing, ltx_official, ltx_script

HE = {"name": "陈测试", "name_en": "Test Chen", "handle": "测试",
      "keyframe_desc": "He stands against a plain studio wall.",
      "pronouns": {"subject": "he", "object": "him", "possessive": "his",
                   "possessive_standalone": "his"}}
THEY = {"name": "Sam", "keyframe_desc": "They stand against a plain wall.",
        "pronouns": {"subject": "they", "object": "them", "possessive": "their",
                     "possessive_standalone": "theirs"}}
SHE = {"name": "Ana", "keyframe_desc": "She stands against a plain wall."}

BEAT = {"line": "x", "expression": "Eyes widen.", "gestures": "A hand turns over.",
        "body": "Weight shifts."}

_SLOT_LEFT = re.compile(r"\$[A-Za-z_]")
_FEMININE = re.compile(r"\b(she|her|hers|herself|woman)\b", re.I)


def _templates(host: dict, monkeypatch) -> dict[str, str]:
    """Every presenter template, filled for `host`, with regender() disabled."""
    monkeypatch.setattr(ltx_script, "regender", lambda text, host: text)
    sent = []

    def offline(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        raise OSError("offline")

    monkeypatch.setattr(ltx_official.urllib.request, "urlopen", offline)
    fallback = ltx_official.compose(dict(BEAT), {"director": {}}, host=host)
    msgs = sent[0]["messages"]
    return {
        "deck_system[en]": host_briefing._deck_system(host, "en", 30.0),
        "deck_system[zh]": host_briefing._deck_system(host, "zh", 30.0),
        "performance_rules": ltx_official._rg(host, ltx_official.PERFORMANCE_RULES),
        "compose.system": msgs[0]["content"],
        "compose.brief": msgs[1]["content"],
        "compose.fallback": fallback,
    }


@pytest.mark.parametrize("host", [HE, THEY, SHE], ids=["he", "they", "she"])
def test_every_slot_is_filled(host, monkeypatch):
    for name, text in _templates(host, monkeypatch).items():
        assert not _SLOT_LEFT.search(text), (name, _SLOT_LEFT.search(text))


@pytest.mark.parametrize("host", [HE, THEY], ids=["he", "they"])
def test_no_pronoun_is_typed_into_a_template(host, monkeypatch):
    for name, text in _templates(host, monkeypatch).items():
        assert not _FEMININE.search(text), (name, _FEMININE.search(text))


def test_the_deck_writer_names_the_presenter(monkeypatch):
    text = _templates(HE, monkeypatch)["deck_system[en]"]
    assert text.startswith("You write what 陈测试 (Test Chen) says")
    assert "belongs to 陈测试, and" in text
    assert "how he carries\nhimself on each one" in text
    assert "a bad day for him," in text          # the objective case, not "his"


def test_fill_leaves_other_dollar_signs_alone():
    assert (ltx_script.fill("$subj has $100,000.00 in $NVDA; $poss_alone and $POSS", HE)
            == "he has $100,000.00 in $NVDA; his and HIS")


def test_regender_is_a_no_op_for_a_she_presenter():
    text = "She told her friend it was hers, a bad day for her."
    assert ltx_script.regender(text, SHE) == text


def test_regender_picks_the_objective_case_at_the_end_of_a_clause():
    assert (ltx_script.regender("Her face. A bad day for her. She did it herself.", HE)
            == "His face. A bad day for him. He did it himself.")


def test_the_truth_block_is_keyed_as_written_and_regendered_when_sent():
    """_account_truth's exact bytes are part of the deck cache key (generate.py),
    so the block is written she/her and re-gendered on the way into the prompt.
    A presenter-specific truth would re-key every deck and every clip."""
    truth = host_briefing._account_truth(None)
    assert "what she holds" in truth
    deck = host_briefing._deck_system(HE, "en", 30.0, truth=truth)
    assert "what he holds" in deck and "what she holds" not in deck
