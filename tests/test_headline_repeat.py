"""The cover headline must stop re-leading with yesterday's story.

The headline call had no memory of earlier editions, so a slow-burning story
led every day it was still the biggest number: "10-year Treasury yield ...
highest since 2007" fronted SIX recaps (08-18 → 09-24). These tests pin the
three pieces of the fix: the archive helper that supplies the recent front
pages, the prompt that carries them, and the deterministic same-story guard
(calibrated on the archive's REAL titles, quoted verbatim below).
"""
from __future__ import annotations

import itertools
import json

import pytest

from briefing import director, generate


# the six editions the yield story led, verbatim from the archive
YIELD = [
    "30-year yield tops 5.3%, highest since 2007",                         # 08-18
    "30-year yield hits 5.33%, a 19-year high",                            # 08-23
    "Treasury yield hits 5.04%, highest since 2007",                       # 09-15
    "Ten-year Treasury yield tops 5% for first time since 2007",           # 09-18
    "10-year Treasury yield climbs to about 5.1%, highest since 2007",     # 09-23
    "10-year Treasury yield climbs above 5.1%, highest since 2007",        # 09-24
]
REPEATS = [
    ("Fed hikes to 3.75-4.00%, Dow drops 631 points",                      # 09-16
     "Fed raises rates a quarter point to 3.75-4.00%, first hike since 2023"),  # 09-20
    ("Nasdaq climbs 2.26% to record close of 27,122.09",                   # 09-21
     "Nasdaq closes at record 27,244, up half a percent"),                 # 09-22
    ("Brent tops $101 as US-Iran strikes escalate",                        # 09-09
     "Oil tops $100 on war worries, S&P slips 0.58%"),                     # 09-10
]
# consecutive editions that are genuinely different stories — some share an
# index, a verb or a theme word, which is exactly what must NOT trip the guard
DIFFERENT = [
    ("S&P 500 hits record 7,799 on cool PPI",
     "S&P 500 slips from record as retail sales drop first time in 9 months"),
    ("Chips rally as S&P slips on hot payrolls",
     "SPY slides 0.77% on hot inflation, crude above $100"),
    ("S&P 500 closes July up 0.7% at 7,489.72",
     "July payrolls fall 23,000, S&P 500 hits record close"),
    ("Treasury yield hits 5.04%, highest since 2007",
     "Fed hikes to 3.75-4.00%, Dow drops 631 points"),
    ("Fed raises rates a quarter point to 3.75-4.00%, first hike since 2023",
     "Nasdaq climbs 2.26% to record close of 27,122.09"),
    ("Nasdaq closes at record 27,244, up half a percent",
     "10-year Treasury yield climbs to about 5.1%, highest since 2007"),
    ("Samsung drops 9% on payout miss, drags chips",
     "S&P 500 bounces 0.32% to 7,677.28 on chip-led rebound"),
    ("Dow falls 1.3% as relief rally fades",
     "S&P 500 up 0.43% to 7,674.37 as healthcare, financials lead rebound"),
    ("Nvidia revenue more than doubles, guide clears bar",
     "Nvidia's blowout lifts Nasdaq 0.83%, Dow slips 0.19%"),
]


# --------------------------------------------------------------------------
# the similarity metric, on real titles
# --------------------------------------------------------------------------
@pytest.mark.parametrize("a,b", list(itertools.combinations(YIELD, 2)))
def test_every_pair_of_the_six_yield_titles_is_one_story(a, b):
    assert director.title_similarity(a, b) >= director._REPEAT_AT


@pytest.mark.parametrize("a,b", REPEATS)
def test_the_other_real_repeats_are_caught(a, b):
    assert director.title_similarity(a, b) >= director._REPEAT_AT


@pytest.mark.parametrize("a,b", DIFFERENT)
def test_genuinely_different_days_are_not_flagged(a, b):
    assert director.title_similarity(a, b) < director._REPEAT_AT


def test_numbers_alone_never_make_a_repeat():
    """Two unrelated stories that happen to quote the same figure."""
    assert director.title_similarity("Oracle falls 3.47% on data-center notice",
                                     "Meta jumps 3.47% as Connect wraps") == 0.0


# --------------------------------------------------------------------------
# headline(): prompt context + guard
# --------------------------------------------------------------------------
DEC = {"notes": {"_": "Thursday opened with a bond-market scare, as the 10-year "
                      "Treasury yield climbed above 5.1% to its highest since 2007. "
                      "Meta was the day's clear winner."}}
CLIPS = [{"symbol": "META", "bias": "long", "headline": "Meta's Muse Rally"},
         {"symbol": "ORCL", "bias": "flat", "headline": "Force Majeure"}]
REPEAT_HEAD = {"title": YIELD[5], "keywords": ["YIELD 5.1%", "BOND SCARE"],
               "art": "a trader on a ladder of bond certificates"}
FRESH_HEAD = {"title": "Meta jumps 4.50% as Wall Street piles into Muse",
              "keywords": ["META +4.5%", "YIELD 5.1%"],
              "art": "a cartoon rocket shaped like a thumbs-up"}
RECENT = [YIELD[4], "Nasdaq closes at record 27,244, up half a percent",
          "Nasdaq climbs 2.26% to record close of 27,122.09", YIELD[3]]


class _Chat:
    """Scripted director._chat_json: pops one canned answer per call and
    records the messages of every call."""

    def __init__(self, monkeypatch, answers):
        self.answers, self.calls = list(answers), []
        monkeypatch.setattr(director, "_chat_json", self)

    def __call__(self, payload, d, timeout, tag, salvage=None):
        self.calls.append(payload["messages"])
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return dict(a)


def test_the_prompt_carries_the_recent_front_pages(monkeypatch):
    chat = _Chat(monkeypatch, [FRESH_HEAD])
    out = director.headline(DEC, CLIPS, {}, recent=RECENT)
    assert out["title"] == FRESH_HEAD["title"]
    user = chat.calls[0][-1]["content"]
    assert "RECENT FRONT PAGES" in user
    for t in RECENT:
        assert director._mask_numbers(t) in user
    # the rule lives in the system prompt; the factual rules are still there
    system = chat.calls[0][0]["content"]
    assert "DIFFERENT" in system and "EXACTLY as the digest states it" in system


def test_old_figures_never_reach_the_model(monkeypatch):
    """THE RISK OF SHOWING OLD TITLES AT ALL. The headline rules say every number
    must be copied from today's digest — and the recent front pages are appended
    to that same digest. Verbatim, they would offer last week's "27,244" as a
    number the model is allowed to use. The story words stay; the figures go."""
    chat = _Chat(monkeypatch, [FRESH_HEAD])
    old = ["Nasdaq closes at record 27,244, up half a percent",
           "Fed hikes to 3.75-4.00%, Dow drops 631 points",
           "10-year Treasury yield climbs above 5.1%, highest since 2007"]
    director.headline(DEC, CLIPS, {}, recent=old)
    block = chat.calls[0][-1]["content"].split("RECENT FRONT PAGES", 1)[1]
    for figure in ("27,244", "3.75", "4.00%", "631", "5.1%", "2007"):
        assert figure not in block
    assert "Nasdaq closes at record" in block and "highest since" in block


def test_old_figures_never_reach_the_zh_localiser(monkeypatch):
    chat = _Chat(monkeypatch, [{"title": "Meta狂飙4.5%", "keywords": ["META +4.5%"]}])
    director.headline_zh(FRESH_HEAD, {}, recent_zh=["纳指收创新高27244点，涨了半个百分点"])
    ctx = chat.calls[0][-1]["content"].split("最近几期的中文标题", 1)[1]
    assert "27244" not in ctx and "纳指收创新高#点" in ctx


def test_no_recent_titles_leaves_the_prompt_as_it_was(monkeypatch):
    chat = _Chat(monkeypatch, [FRESH_HEAD])
    director.headline(DEC, CLIPS, {})
    assert "RECENT FRONT PAGES" not in chat.calls[0][-1]["content"]
    assert len(chat.calls) == 1


def test_a_repeated_title_triggers_one_retry_naming_the_collision(monkeypatch):
    """Today's real title against the real archive: it must be retried."""
    chat = _Chat(monkeypatch, [REPEAT_HEAD, FRESH_HEAD])
    out = director.headline(DEC, CLIPS, {}, recent=RECENT)
    assert out["title"] == FRESH_HEAD["title"]
    assert len(chat.calls) == 2
    nudge = chat.calls[1][-1]["content"]
    assert YIELD[5] in nudge and YIELD[4] in nudge
    assert chat.calls[1][-2]["role"] == "assistant"


def test_a_still_similar_retry_is_kept_rather_than_dropped(monkeypatch):
    still = dict(REPEAT_HEAD, title="Ten-year yield tops 5.1%, highest since 2007")
    chat = _Chat(monkeypatch, [REPEAT_HEAD, still])
    out = director.headline(DEC, CLIPS, {}, recent=RECENT)
    assert out is not None and out["title"] in (REPEAT_HEAD["title"], still["title"])
    assert len(chat.calls) == 2           # exactly one retry, never a loop


def test_an_equally_repetitive_retry_keeps_the_first_title(monkeypatch):
    """A tie is not an improvement, so the model's first answer stands."""
    first = director._repeat_of(REPEAT_HEAD["title"], RECENT)
    same = dict(REPEAT_HEAD, title="10-year Treasury yield rises above 5.1%, highest since 2007")
    assert director._repeat_of(same["title"], RECENT)[1] == first[1]
    _Chat(monkeypatch, [REPEAT_HEAD, same])
    assert director.headline(DEC, CLIPS, {}, recent=RECENT)["title"] == REPEAT_HEAD["title"]


def test_a_less_repetitive_retry_wins(monkeypatch):
    """Still flagged, but strictly less like the recent front page than the
    first answer was: the retry is the better cover and is the one kept."""
    recent = ["Fed hikes to 3.75-4.00%, Dow drops 631 points"]
    first = dict(REPEAT_HEAD, title="Fed hikes again as Dow drops 400 points")
    retry = dict(REPEAT_HEAD, title="Fed hike odds jump as Treasury yields climb")
    assert director._repeat_of(first["title"], recent)[1] == 1.0
    assert director._REPEAT_AT <= director._repeat_of(retry["title"], recent)[1] < 1.0
    _Chat(monkeypatch, [first, retry])
    assert director.headline(DEC, CLIPS, {}, recent=recent)["title"] == retry["title"]


def test_a_failed_retry_keeps_the_first_title(monkeypatch):
    _Chat(monkeypatch, [REPEAT_HEAD, ValueError("dead"), ValueError("dead")])
    out = director.headline(DEC, CLIPS, {}, recent=RECENT)
    assert out["title"] == REPEAT_HEAD["title"]


def test_the_guard_only_looks_at_the_last_few_front_pages(monkeypatch):
    """A story that led long ago may lead again; the prompt still lists it."""
    old = ["Samsung drops 9% on payout miss, drags chips"] * director._REPEAT_WINDOW
    chat = _Chat(monkeypatch, [REPEAT_HEAD])
    out = director.headline(DEC, CLIPS, {}, recent=old + [YIELD[4]])
    assert out["title"] == REPEAT_HEAD["title"] and len(chat.calls) == 1


# --------------------------------------------------------------------------
# headline_zh(): gets the recent Chinese titles, same return contract
# --------------------------------------------------------------------------
def test_headline_zh_gets_the_recent_chinese_titles(monkeypatch):
    zh = ["10年期美债收益率飙至5.1%，2007年以来最高！", "纳指收创新高27244点，涨了半个百分点"]
    chat = _Chat(monkeypatch, [{"title": "Meta狂飙4.5%，Muse引爆华尔街",
                                "keywords": ["META +4.5%", "美债5.1%"]}])
    out = director.headline_zh(FRESH_HEAD, {}, recent_zh=zh)
    assert out == {"title": "Meta狂飙4.5%，Muse引爆华尔街",
                   "keywords": ["META +4.5%", "美债5.1%"]}
    user = chat.calls[0][-1]["content"]
    assert all(director._mask_numbers(t) in user for t in zh)
    assert FRESH_HEAD["title"] in user


def test_headline_zh_without_context_is_unchanged(monkeypatch):
    chat = _Chat(monkeypatch, [{"title": "Meta狂飙4.5%", "keywords": ["META +4.5%"]}])
    director.headline_zh(FRESH_HEAD, {})
    src = chat.calls[0][-1]["content"]
    assert json.loads(src)["title"] == FRESH_HEAD["title"]


# --------------------------------------------------------------------------
# generate.recent_titles(): the archive side
# --------------------------------------------------------------------------
def _edition(root, date, slot, title, title_zh=""):
    p = root / date / slot
    p.mkdir(parents=True)
    (p / "meta.json").write_text(json.dumps(
        {"date": date, "title": title, "title_zh": title_zh}))


def test_recent_titles_excludes_the_current_and_later_dates(tmp_path):
    for i, day in enumerate(["2026-09-20", "2026-09-21", "2026-09-22",
                             "2026-09-23", "2026-09-24"]):
        _edition(tmp_path, day, "video", f"title {day}", f"标题{i}")
    got = generate.recent_titles(tmp_path, "2026-09-23", "video")
    assert [r["date"] for r in got] == ["2026-09-22", "2026-09-21", "2026-09-20"]
    assert got[0] == {"date": "2026-09-22", "title": "title 2026-09-22",
                      "title_zh": "标题2"}


def test_recent_titles_keeps_to_its_slot_and_limit(tmp_path):
    for d in range(1, 12):
        _edition(tmp_path, f"2026-09-{d:02d}", "video", f"video {d}")
    _edition(tmp_path, "2026-09-12", "slideshow-midday-briefing", "a slideshow")
    got = generate.recent_titles(tmp_path, "2026-09-13", "video", n=7)
    assert len(got) == 7 and got[0]["title"] == "video 11"
    assert all("slideshow" not in r["title"] for r in got)


def test_recent_titles_survives_a_missing_or_corrupt_archive(tmp_path):
    assert generate.recent_titles(tmp_path / "nope", "2026-09-24", "video") == []
    (tmp_path / "2026-09-22" / "video").mkdir(parents=True)
    (tmp_path / "2026-09-22" / "video" / "meta.json").write_text("{half a file")
    (tmp_path / "2026-09-21" / "video").mkdir(parents=True)
    (tmp_path / "2026-09-21" / "video" / "meta.json").write_text("[1, 2]")
    (tmp_path / "_backups").mkdir()
    _edition(tmp_path, "2026-09-20", "video", "")          # no title at all
    _edition(tmp_path, "2026-09-19", "video", "the one good title")
    got = generate.recent_titles(tmp_path, "2026-09-24", "video")
    assert [r["title"] for r in got] == ["the one good title"]
