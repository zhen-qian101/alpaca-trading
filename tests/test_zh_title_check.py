"""The Chinese cover title is fact-checked by a second model before it ships.

Why this exists, measured 2026-09-25 on all 46 archived editions (3 samples each,
two blind judges per title, every verdict read against the WHOLE recap file):
the translated title carried a factual error both judges agreed on in 12.4% of
cases — New Mexico as 墨西哥州, "chips and clouds rip" as 芯片云崩 (shipped on
2026-08-12), a shareholder-payout miss as 财报暴雷. A glm-5.3-flash check against
the recap plus ONE rewrite with its critique brought that to 2.9%.

    .venv/bin/pytest -q tests/test_zh_title_check.py
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")

import pytest  # noqa: E402

from briefing import director  # noqa: E402

HEAD = {"title": "Chips and clouds rip as mega-cap platforms get sold",
        "keywords": ["CHIPS RIP", "MEGACAPS SOLD"]}
SOURCE = "[_] Chips, clouds and quantum names ripped: SOXL +6.89%, NVDA +3.03%. Mega-cap platforms were sold."
WRONG = {"title": "芯片云股崩了，巨头平台遭甩卖", "keywords": ["芯片云崩", "巨头遭抛售"]}
RIGHT = {"title": "芯片云股狂飙，巨头平台却被抛售", "keywords": ["芯片云股狂飙", "巨头遭抛售"]}
CFG = {"director": {"model": "deepseek-v4.1-flash:cloud"},
       "zh": {"title_check": {"enabled": True, "model": "glm-5.3-flash:cloud"}}}


def _ok():
    return {"ok": True, "errors": []}


def _bad(msg="方向相反：来源说芯片云股大涨（ripped），标题写成崩了"):
    return {"ok": False, "errors": [msg]}


class _Chat:
    """Scripted director._chat_json: pops one canned answer per call and
    records (tag, model, messages) of every call."""

    def __init__(self, monkeypatch, answers):
        self.answers, self.calls = list(answers), []
        monkeypatch.setattr(director, "_chat_json", self)

    def __call__(self, payload, d, timeout, tag, salvage=None):
        self.calls.append({"tag": tag, "model": payload["model"],
                           "messages": payload["messages"], "salvage": salvage})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return dict(a)

    def tags(self):
        return [c["tag"] for c in self.calls]


# --------------------------------------------------------------------------
# the verdict: glm-5.3-flash reasons in prose and ENDS with the JSON
# --------------------------------------------------------------------------
def test_the_verdict_is_salvaged_from_a_reasoning_reply():
    """Measured: glm-5.3-flash ignores `format` and `think:false` and writes its
    reasoning as the content, often with a stray </think>, then the verdict."""
    raw = ('Let me check the Chinese title against the source.\n"崩了" means crashed, but the '
           'source says ripped. Final answer: ok false.</think>{"ok": false, "errors": '
           '["方向相反：来源说大涨"]}')
    assert director._verdict_salvage(raw) == {"ok": False, "errors": ["方向相反：来源说大涨"]}


def test_the_LAST_verdict_wins_when_the_reasoning_drafts_one():
    raw = 'Draft: {"ok": false, "errors": ["x"]} ... on reflection that was wrong. {"ok": true, "errors": []}'
    assert director._verdict_salvage(raw)["ok"] is True


def test_a_reply_with_no_verdict_salvages_to_nothing():
    assert director._verdict_salvage("I think the title is fine.") is None
    assert director._verdict_salvage("") is None


# --------------------------------------------------------------------------
# the source: the WHOLE recap, not only its notes
# --------------------------------------------------------------------------
def test_the_source_carries_the_briefing_as_well_as_the_notes():
    """KOSPI's -4.37% on 2026-08-06 lived only in briefing._.catalysts; a checker
    shown the notes alone called the (correct) title unsupported."""
    dec = {"notes": {"_": "Memory stocks detonated."},
           "briefing": {"_": {"catalysts": [{"detail": "KOSPI fell 4.37% into a sidecar halt"}]}}}
    src = director.recap_source(dec)
    assert "Memory stocks detonated" in src and "KOSPI fell 4.37%" in src


def test_the_source_survives_a_recap_without_a_briefing_and_is_capped():
    assert director.recap_source({"notes": {"_": "x" * 50}}) == "[_] " + "x" * 50
    assert director.recap_source(None) == ""
    assert len(director.recap_source({"notes": {"_": "y" * 99999}}, limit=1000)) == 1000


# --------------------------------------------------------------------------
# headline_zh(): check -> ONE rewrite -> fewer problems ships
# --------------------------------------------------------------------------
def test_without_a_source_nothing_changes(monkeypatch):
    """Backfills and callers that do not pass the recap get exactly the old
    behaviour: one localiser call, the old return shape."""
    chat = _Chat(monkeypatch, [WRONG])
    assert director.headline_zh(HEAD, CFG) == WRONG
    assert chat.tags() == ["headline_zh"]


def test_a_clean_title_is_checked_once_and_ships(monkeypatch):
    chat = _Chat(monkeypatch, [RIGHT, _ok()])
    out = director.headline_zh(HEAD, CFG, source=SOURCE)
    assert (out["title"], out["keywords"]) == (RIGHT["title"], RIGHT["keywords"])
    assert out["check"]["ok"] is True and out["check"]["rewritten"] is False
    assert chat.tags() == ["headline_zh", "zh_check"]


def test_the_checker_is_a_different_model_reading_the_recap(monkeypatch):
    """A SECOND model is the point: the localiser's own model flagged a third of
    all titles and was half as precise (measured)."""
    chat = _Chat(monkeypatch, [RIGHT, _ok()])
    director.headline_zh(HEAD, CFG, source=SOURCE)
    check = chat.calls[1]
    assert check["model"] == "glm-5.3-flash:cloud" != chat.calls[0]["model"]
    user = check["messages"][-1]["content"]
    assert SOURCE in user and HEAD["title"] in user and RIGHT["title"] in user
    assert check["salvage"] is director._verdict_salvage


def test_a_flagged_title_is_rewritten_once_with_the_critique(monkeypatch):
    """The 2026-08-12 case: direction flipped. The rewrite sees the checker's
    exact complaint and the version it is fixing."""
    chat = _Chat(monkeypatch, [WRONG, _bad(), RIGHT, _ok()])
    out = director.headline_zh(HEAD, CFG, source=SOURCE)
    assert out["title"] == RIGHT["title"]
    assert out["check"]["rewritten"] is True and out["check"]["ok"] is True
    assert out["check"]["first_errors"] == _bad()["errors"]
    rewrite = chat.calls[2]["messages"]
    assert json.loads(rewrite[-2]["content"])["title"] == WRONG["title"]
    assert _bad()["errors"][0] in rewrite[-1]["content"]
    assert chat.tags() == ["headline_zh", "zh_check", "headline_zh", "zh_check"]


def test_a_rewrite_that_is_no_better_does_not_replace_the_first(monkeypatch):
    """Fewer problems wins; a tie keeps the original. Exactly one rewrite, never
    a loop — this runs inside the nightly render."""
    chat = _Chat(monkeypatch, [WRONG, _bad(), dict(WRONG, title="芯片云股双双跳水，巨头遭抛售"),
                               _bad("还是方向相反")])
    out = director.headline_zh(HEAD, CFG, source=SOURCE)
    assert out["title"] == WRONG["title"]
    assert out["check"]["ok"] is False and out["check"]["rewritten"] is False
    assert out["check"]["rewrite_errors"] == ["还是方向相反"]
    assert len(chat.calls) == 4


def test_a_checker_outage_keeps_the_title_unchecked(monkeypatch, capsys):
    """Cosmetic step: a cloud outage must never cost the cover or trigger a
    rewrite on no evidence."""
    chat = _Chat(monkeypatch, [RIGHT, ValueError("zh_check: missing keys: ok, errors")])
    out = director.headline_zh(HEAD, CFG, source=SOURCE)
    assert out["title"] == RIGHT["title"]
    assert out["check"]["checked"] is False and out["check"]["rewritten"] is False
    assert len(chat.calls) == 2
    assert "unavailable" in capsys.readouterr().out


def test_a_failed_rewrite_keeps_the_first_title(monkeypatch):
    _Chat(monkeypatch, [WRONG, _bad(), ValueError("dead"), ValueError("dead")])
    out = director.headline_zh(HEAD, CFG, source=SOURCE)
    assert out["title"] == WRONG["title"] and out["check"]["ok"] is False


def test_an_over_length_title_is_rewritten_even_when_the_facts_pass(monkeypatch):
    """The 30-character rule is arithmetic, so it needs no model — 7% of
    translated titles broke it."""
    long_ = {"title": "布伦特原油破92美元，美债收益率飙至4.79%，美联储加息概率跳涨至66%", "keywords": ["布油$92"]}
    short = {"title": "布伦特破$92，收益率4.79%，加息概率66%", "keywords": ["布油$92"]}
    assert len(long_["title"]) > 30 >= len(short["title"])
    chat = _Chat(monkeypatch, [long_, _ok(), short, _ok()])
    out = director.headline_zh(HEAD, CFG, source=SOURCE)
    assert out["title"] == short["title"] and out["check"]["rewritten"] is True
    assert "超过 30 个字" in chat.calls[2]["messages"][-1]["content"]


def test_a_flag_with_no_explanation_still_counts(monkeypatch):
    """{"ok": false, "errors": []} is still a failed check; the rewrite is told
    so rather than being handed an empty critique."""
    chat = _Chat(monkeypatch, [WRONG, {"ok": False, "errors": []}, RIGHT, _ok()])
    out = director.headline_zh(HEAD, CFG, source=SOURCE)
    assert out["title"] == RIGHT["title"]
    assert "核查员判定有事实错误" in chat.calls[2]["messages"][-1]["content"]


def test_the_check_can_be_switched_off(monkeypatch):
    cfg = {**CFG, "zh": {"title_check": {"enabled": False}}}
    chat = _Chat(monkeypatch, [WRONG])
    out = director.headline_zh(HEAD, cfg, source=SOURCE)
    assert out["title"] == WRONG["title"] and out["check"]["checked"] is False
    assert chat.tags() == ["headline_zh"]


@pytest.mark.parametrize("verdict", [{"ok": "true", "errors": []}, {"ok": 1, "errors": []}])
def test_only_a_literal_true_passes(monkeypatch, verdict):
    """A checker that answers "true" as a string has not produced a verdict the
    code can trust: it counts as a failed check, so the title goes through the
    rewrite and a second check, and only a literal pass there ships it as ok."""
    chat = _Chat(monkeypatch, [RIGHT, verdict, RIGHT, _ok()])
    out = director.headline_zh(HEAD, CFG, source=SOURCE)
    assert out["check"]["first_errors"] == ["核查员判定有事实错误但没有说明"]
    assert out["check"]["ok"] is True and len(chat.calls) == 4
