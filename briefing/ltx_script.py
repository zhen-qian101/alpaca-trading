#!/usr/bin/env python3
"""Presenter-text helpers shared by the recap's deck writer and composer.

  * The writer's and composer's prompts are TEMPLATES written for no presenter
    in particular: the name and every pronoun are $-slots ($who, $subj, $poss,
    ...) that persona() fills from the presenter file;
  * regender() fixes the pronouns in the text that is NOT a template and gets
    interpolated into one: the account ground truth, the presenter file's own
    prose and the writer's beats;
  * as_presenter() does both, and is what every prompt goes through;
  * _ZH is the 中文 writing brief and CPS_ZH its measured character rate.

The production writer is host_briefing._deck_system; the composer is
ltx_official.compose.
"""
from __future__ import annotations

import re


def persona(host: dict) -> dict:
    """The template slots for whichever presenter is speaking: the presenter
    file's `name`, `name_en`, `handle` and `pronouns` block.

    The pronouns are explicit fields rather than one blanket substitution
    because "her" is possessive in most of these prompts and objective in the
    rest, and getting one wrong produces prose that reads as careless in a
    prompt whose whole job is to be read carefully.

    Defaults to she/her when the presenter file has no `pronouns` block.
    """
    p = host.get("pronouns") or {}
    subj = p.get("subject", "she")
    obj = p.get("object", "her")
    poss = p.get("possessive", "her")
    poss_alone = p.get("possessive_standalone", "hers").upper()
    name = host.get("name", "")
    name_en = host.get("name_en", "")
    who = f"{name} ({name_en})" if name_en else name
    return {
        "who": who,
        "name": host.get("name", who),
        "handle": host.get("handle", name),
        "subj": subj, "Subj": subj.capitalize(), "SUBJ": subj.upper(),
        "obj": obj,
        "poss": poss, "Poss": poss.capitalize(), "POSS": poss.upper(),
        "poss_alone": poss_alone.lower(), "POSS_ALONE": poss_alone,
        "refl": {"she": "herself", "he": "himself"}.get(subj, "themselves"),
        "noun": {"she": "woman", "he": "man"}.get(subj, "person"),
    }


_SLOT = re.compile(r"\$([A-Za-z_]+)\b")


def fill(template: str, host: dict) -> str:
    """Fill a prompt template's $-slots from persona(host).

    A `$` that does not name a slot is left exactly as it is: the same pass sees
    dollar amounts and whatever the writer put in a beat. A misspelt slot would
    therefore reach the model verbatim, which is what
    tests/test_presenter_templates.py is there to catch.
    """
    P = persona(host)
    return _SLOT.sub(lambda m: P.get(m.group(1), m.group(0)), template)


def regender(text: str, host: dict) -> str:
    """Fix the pronouns in text that is not a template, for a presenter who is
    not she/her.

    Three kinds of text reach the prompts without being written as templates:
    the account ground truth (host_briefing._account_truth, written she/her —
    its exact bytes are part of the deck cache key, so it stays that way), the
    presenter file's own prose, and the writer's beats, which the composer
    passes on. Returns `text` UNCHANGED for a she/her presenter.

    The one genuine hazard is that "her" is both possessive (her face -> his
    face) and objective (a bad day for her -> for him). An objective "her" ends
    its clause instead of introducing a noun, so that case is handled first and
    possessive is the default.
    """
    P = persona(host)
    if P["subj"] == "she":
        return text
    subs = [
        (r"\bHERS\b", P["POSS_ALONE"]),
        (r"\bhers\b", P["poss_alone"]),
        (r"\bherself\b", P["refl"]),
        (r"\bSHE\b", P["SUBJ"]),
        (r"\bShe\b", P["Subj"]),
        (r"\bshe\b", P["subj"]),
        # OBJECTIVE "her" first: a pronoun that closes its clause instead of
        # introducing a noun ("a bad day for her.") is the object form.
        (r"\bher\b(?=\s*[.,;:!?)]|\s*$)", P["obj"]),
        (r"\bHER\b", P["POSS"]),
        (r"\bHer\b", P["Poss"]),
        (r"\bher\b", P["poss"]),
        (r"\bwoman\b", P["noun"]),
    ]
    for pat, rep in subs:
        text = re.sub(pat, rep, text)
    return text


def as_presenter(text: str, host: dict) -> str:
    """A prompt as it is sent for this presenter: its template slots filled,
    then any she/her in the free text interpolated into it re-gendered."""
    return regender(fill(text, host), host)


_ZH = """
WRITE IN 中文 (Simplified Chinese), not English.

PARAPHRASE, DO NOT TRANSLATE. The notes you are given are in English. Read them,
understand what actually happened, then close them and say it in Mandarin the way
THIS PRESENTER would say it to a friend. Never carry an English sentence across word by word
— the result is 翻译腔: grammatically fine, and instantly recognisable as not
written by a person.

Signs you are translating rather than speaking:
  * long attributive clauses stacked before the noun
  * 书面语 vocabulary: 表现不佳、反应积极、呈现、显示、由于、因此、然而
  * passive constructions: 被买入、被认为
  * 「的」three times in one sentence
  * subjects spelled out where Mandarin would simply drop them

Say it out loud in your head instead:
  translated:  就业数据表现不佳，但市场对此反应积极。
  spoken:      就业数据挺难看的，结果市场反而涨了。
  translated:  英伟达上涨百分之二点二七，领涨芯片板块。
  spoken:      英伟达涨了两个多点，芯片全都跟着起来了。

Use real spoken vocabulary and the small words that make speech sound like speech
— 其实、结果、说真的、居然、还挺、我当时 — without overdoing them. Short
sentences. Numbers said aloud the way a person says them (「涨了百分之二点二七」,
never「+2.27%」). The simplest possible words, no financial jargon at all.

THE LINE IS READ ALOUD BY A MANDARIN TTS ENGINE. Write only what a Mandarin
mouth says smoothly, exactly as this presenter would SAY it:
  * NO Arabic digits, ever: 「三百」not「300」,「百分之一点四」not「1.4%」,
    「八千点」not「8000点」. A product number is spoken too:「Maia三百芯片」.
  * NO ticker symbols: name the company the way Chinese finance people say it
    — 谷歌 not GOOG, 微软 not MSFT, 英伟达 not NVDA. A Latin brand word people
    genuinely SAY is fine (AI, Meta); an institution nobody says aloud
    (FactSet) becomes 研究机构 or 数据商.
  * NO quoted English phrases — paraphrase them in Chinese.
  * Chinese full-width punctuation only（，。！？）. Punctuation is the
    BREATHING: a comma wherever the presenter takes a small pause, a period
    where a thought lands. Short sentences give the voice room to sound alive.

The `mood`, `expression` and `gestures` fields stay in ENGLISH: they are
instructions to the video model, not spoken words.
"""


# MEASURED, not guessed — a Qwen3-TTS presenter clone reading real recap lines:
# English 2.76 words/s across one 17.04 s take, Mandarin 5.43 chars/s across three
# takes (5.38 / 5.43 / 5.47, which is tight enough to trust). A word budget and a
# character budget are not interchangeable, so each language gets its own.
CPS_ZH = 5.4
