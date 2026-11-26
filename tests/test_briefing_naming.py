"""One filename stem per EDITION, and a prune that keeps what the archive needs.

Every render used to write `briefing_<date>.mp4`, so the day's three slideshows
and the flagship recap overwrote each other in out_dir — the archive kept them
apart only because it files by slot. The stem now comes from the same label slug
the slot does, so the file and its archive directory agree.
"""
from __future__ import annotations

import json

import pytest

from briefing.generate import archive_and_index, briefing_slot, edition_stem


@pytest.mark.parametrize("label,expected", [
    ("Opening hour briefing", "opening-hour-briefing_2026-07-31"),
    ("Midday briefing", "midday-briefing_2026-07-31"),
    ("Pre-close briefing", "pre-close-briefing_2026-07-31"),
    ("Post-market recap", "post-market-recap_2026-07-31"),
    ("Weekend recap", "weekend-recap_2026-07-31"),
])
def test_each_edition_gets_its_own_stem(label, expected):
    assert edition_stem(label, "2026-07-31") == expected


@pytest.mark.parametrize("label", [None, "", "   ", "!!!"])
def test_an_unlabelled_render_keeps_the_old_name(label):
    """An ad-hoc `run_briefing.sh` with no --label is not an edition."""
    assert edition_stem(label, "2026-07-31") == "briefing_2026-07-31"


def test_the_stem_and_the_archive_slot_agree():
    """Both key on the same label slug, which is the point — a file downloaded
    from the archive says which directory it came from."""
    label = "Opening hour briefing"
    slot = briefing_slot({"renderer": "hyperframes", "label": label})
    assert slot == "slideshow-opening-hour-briefing"
    assert edition_stem(label, "2026-07-31").startswith("opening-hour-briefing_")


def test_two_editions_on_one_day_no_longer_collide():
    a = edition_stem("Midday briefing", "2026-07-31")
    b = edition_stem("Pre-close briefing", "2026-07-31")
    assert a != b


# --------------------------------------------------------------------------
# the prune
# --------------------------------------------------------------------------
def _edition(out_dir, archive, stem, date, label):
    """Write a minimal render into out_dir and archive it."""
    mp4 = out_dir / f"{stem}.mp4"
    mp4.write_bytes(b"mp4")
    cover = out_dir / f"{stem}_cover.png"
    cover.write_bytes(b"png")
    clip = out_dir / "clip_MARKET.mp4"
    clip.write_bytes(b"clip")
    meta = {"date": date, "label": label, "renderer": "hyperframes",
            "briefing_file": mp4.name, "cover_file": cover.name, "clips": [],
            "generated_at": f"{date}T12:00:00+00:00"}
    archive_and_index(date, mp4, [clip], meta, out_dir, archive)
    return mp4, cover, clip


def test_prune_keeps_todays_other_editions(tmp_path):
    """The regression this naming exists for: rendering the midday edition must
    not delete the morning one that is still on the dashboard."""
    out, arch = tmp_path / "media", tmp_path / "archive"
    out.mkdir(); arch.mkdir()
    morning, m_cover, _ = _edition(out, arch, "opening-hour-briefing_2026-07-31",
                                   "2026-07-31", "Opening hour briefing")
    midday, d_cover, clip = _edition(out, arch, "midday-briefing_2026-07-31",
                                     "2026-07-31", "Midday briefing")
    assert morning.exists() and m_cover.exists()
    assert midday.exists() and d_cover.exists()
    assert clip.exists(), "clip_*.mp4 is archived from out_dir — pruning it was a bug"


def test_prune_still_drops_other_dates(tmp_path):
    out, arch = tmp_path / "media", tmp_path / "archive"
    out.mkdir(); arch.mkdir()
    stale = out / "opening-hour-briefing_2026-07-30.mp4"
    stale.write_bytes(b"old")
    legacy = out / "briefing_2026-07-29.mp4"          # pre-rename name
    legacy.write_bytes(b"older")
    _edition(out, arch, "midday-briefing_2026-07-31", "2026-07-31", "Midday briefing")
    assert not stale.exists() and not legacy.exists()


def test_history_points_at_the_per_edition_files(tmp_path):
    out, arch = tmp_path / "media", tmp_path / "archive"
    out.mkdir(); arch.mkdir()
    _edition(out, arch, "opening-hour-briefing_2026-07-31", "2026-07-31",
             "Opening hour briefing")
    hist = json.loads((out / "history.json").read_text())
    entry = hist["briefings"][0]
    assert "opening-hour-briefing_2026-07-31.mp4" in entry["briefing"]
    assert "opening-hour-briefing_2026-07-31_cover.png" in entry["cover"]
