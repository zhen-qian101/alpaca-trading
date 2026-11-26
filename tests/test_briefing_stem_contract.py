"""The renderer must write the edition file the CALLER named.

On 2026-07-31 `generate.main` began naming each edition after its label
(`edition_stem`), but `story.py` still built its concat destination as
`briefing_<date>.mp4`. Every render then succeeded — slides, concat, zh cut,
cover — and `main()` fell over on the very next line, `_duration(briefing)`,
because ffprobe was pointed at a name nobody had written. Three editions were
lost that day (midday, pre-close and the flagship recap): rendered in full,
never archived, never published.

Nothing caught it because the two halves of the name were derived
independently. These tests pin the contract instead: the stem goes IN as an
argument and the returned path comes OUT of the same string.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from briefing import story
from briefing.generate import edition_stem


CFG = {"story": {"include_overview": True},
       "zh": {"enabled": False}, "max_symbols": 5}

DEC = {"date": "2026-07-31", "watchlist": ["MSFT"], "bias": {"MSFT": "long"},
       "priority": {"MSFT": 1},
       "notes": {"_": "The tape held its range.", "MSFT": "Held the 50-day."}}


@pytest.fixture
def stubbed(monkeypatch):
    """Stub only the expensive leaves — TTS, slide compositing, the desk capture
    and ffmpeg concat. The naming path under test runs for real."""
    stitched: list[Path] = []

    def fake_slide(sym, bias, brief, note, cfg, date, dest, **kw):
        dest.write_bytes(b"clip")
        return {"duration": 12.0, "narration": f"{sym} narration.", "lines": [f"{sym} line."]}

    def fake_stitch(clips, dest, cfg, scope=None):
        stitched.append(dest)
        dest.write_bytes(b"concat")

    monkeypatch.setattr(story, "render_bullet_slide", fake_slide)
    monkeypatch.setattr(story.G, "stitch", fake_stitch)
    monkeypatch.setattr(story.G, "briefing_for", lambda dec, sym: None)
    monkeypatch.setattr(story, "_prepare_desk", lambda cfg: (None, None, None))
    return stitched


@pytest.mark.parametrize("label,expected", [
    ("Opening hour briefing", "opening-hour-briefing_2026-07-31.mp4"),
    ("Midday briefing", "midday-briefing_2026-07-31.mp4"),
    ("Pre-close briefing", "pre-close-briefing_2026-07-31.mp4"),
])
def test_the_slideshow_writes_the_stem_it_was_given(tmp_path, stubbed, label, expected):
    stem = edition_stem(label, "2026-07-31")
    briefing, _, _ = story.render_bullet_slideshow(DEC, CFG, "2026-07-31", tmp_path, stem)
    assert briefing.name == expected
    assert stubbed == [briefing], "the concat destination IS the returned path"


def test_the_returned_path_actually_exists(tmp_path, stubbed):
    """The exact failure: main() ran ffprobe on the path it believed in. Whatever
    a renderer returns must be a file, or the next line crashes the run after all
    the GPU work is already spent."""
    stem = edition_stem("Midday briefing", "2026-07-31")
    briefing, _, _ = story.render_bullet_slideshow(DEC, CFG, "2026-07-31", tmp_path, stem)
    assert briefing.is_file()


def test_no_stem_keeps_the_legacy_name(tmp_path, stubbed):
    """An ad-hoc render with no --label still writes briefing_<date>.mp4, so
    older callers and the archived back-catalogue keep working."""
    briefing, _, _ = story.render_bullet_slideshow(DEC, CFG, "2026-07-31", tmp_path)
    assert briefing.name == "briefing_2026-07-31.mp4"


def test_render_scenes_forwards_the_stem_to_the_bullet_path(tmp_path, stubbed):
    """`render_scenes` is what generate.py actually calls; it delegates, and
    the delegation dropped the stem once."""
    stem = edition_stem("Pre-close briefing", "2026-07-31")
    briefing, _, _ = story.render_scenes(DEC, CFG, "2026-07-31", tmp_path, stem)
    assert briefing.name == "pre-close-briefing_2026-07-31.mp4"


@pytest.mark.parametrize("fn", ["render_bullet_slideshow", "render_scenes", "render_scenes_wan"])
def test_every_renderer_accepts_a_stem(fn):
    """A renderer that cannot be told the name will silently invent one."""
    import inspect
    assert "stem" in inspect.signature(getattr(story, fn)).parameters


def test_the_edition_name_is_never_rebuilt_from_the_date(tmp_path, stubbed):
    """The root cause in one assertion: two independent derivations of one name.
    A renderer that ignores its stem and rebuilds from `date` passes every other
    test here only while the label happens to be empty."""
    stem = edition_stem("Opening hour briefing", "2026-07-31")
    briefing, _, _ = story.render_bullet_slideshow(DEC, CFG, "2026-07-31", tmp_path, stem)
    assert "briefing_2026-07-31.mp4" != briefing.name
    assert not (tmp_path / "briefing_2026-07-31.mp4").exists()


# --------------------------------------------------------------------------
# blast radius of a late failure
# --------------------------------------------------------------------------
def test_an_unprobeable_file_does_not_destroy_a_finished_render(tmp_path, capsys):
    """`_duration` runs after every frame is rendered and every cover drawn, one
    line before archive + publish. A raising version turned a cosmetic unknown
    into three lost editions. It must degrade, not detonate."""
    from briefing.generate import _duration

    junk = tmp_path / "not-a-video.mp4"
    junk.write_bytes(b"definitely not an mp4")
    assert _duration(junk) == 0.0
    assert "cannot probe duration" in capsys.readouterr().out


def test_a_missing_file_is_survivable_too(tmp_path, capsys):
    from briefing.generate import _duration

    assert _duration(tmp_path / "gone.mp4") == 0.0


def test_a_real_probe_still_returns_the_real_number(tmp_path):
    """The degradation must not swallow the normal path."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH")
    from briefing.generate import _duration

    mp4 = tmp_path / "two-seconds.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=black:s=64x64:d=2", str(mp4)], check=True)
    assert _duration(mp4) == pytest.approx(2.0, abs=0.2)
