"""The 2026-08-02 weekend recap failure, pinned at every link in its chain.

What happened: all 24 Wan shots rendered (~2h of GPU), then `_rechunk_cjk_srt`
rewrote one Chinese subtitle into a 1-byte file, `_beat_clip`'s `st_size > 0`
guard passed it, ffmpeg's subtitles filter could not open a cue-less .srt and
exited 183, and generate.py's blanket wan→hyperframes fallback threw the whole
finished render away — then archived the slideshow as renderer=wan.

Four fixes, four sections. Each test fails against the pre-fix code.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from briefing import story
from briefing.generate import _rechunk_cjk_srt, _story_scene_render, srt_has_cues


ZH_CUE = "1\n00:00:00,100 --> 00:00:03,500\n亚马逊云业务增速创了十八个月新高，资本开支单季上调二百亿美元\n"


# --------------------------------------------------------------------------
# 1. the rechunker must never write a cue-less file
# --------------------------------------------------------------------------
def test_unparseable_srt_is_left_untouched_not_emptied(tmp_path):
    """The origin of the 1-byte file: every block failed to parse, and the
    in-place rewrite wrote out the empty join anyway."""
    srt = tmp_path / "z.srt"
    original = "WEBVTT weirdness\nno timestamps here\n"
    srt.write_text(original, encoding="utf-8")
    _rechunk_cjk_srt(srt)
    assert srt.read_text(encoding="utf-8") == original


def test_a_valid_long_cue_is_still_rechunked(tmp_path):
    srt = tmp_path / "z.srt"
    srt.write_text("1\n00:00:00,100 --> 00:00:13,000\n" + "特" * 75 + "\n",
                   encoding="utf-8")
    _rechunk_cjk_srt(srt, max_chars=24)
    text = srt.read_text(encoding="utf-8")
    assert text.count("-->") >= 3            # split into several cues
    assert text.count("特") == 75            # no characters lost


def test_a_mixed_file_keeps_the_blocks_it_cannot_parse(tmp_path):
    """Dropping unparseable blocks silently is how subtitle TEXT went missing;
    they must pass through verbatim instead."""
    srt = tmp_path / "z.srt"
    srt.write_text(ZH_CUE + "\nNOTE some stray block\n\n" + ZH_CUE.replace("1\n", "2\n"),
                   encoding="utf-8")
    _rechunk_cjk_srt(srt)
    text = srt.read_text(encoding="utf-8")
    assert "NOTE some stray block" in text
    assert text.count("亚马逊") == 2


# --------------------------------------------------------------------------
# 2. cues, not bytes
# --------------------------------------------------------------------------
def test_the_one_byte_file_fails_the_new_guard(tmp_path):
    srt = tmp_path / "bz_AMZN_3.srt"
    srt.write_text("\n")                      # exactly the 2026-08-02 artifact
    assert srt.stat().st_size > 0             # the OLD guard passed this…
    assert srt_has_cues(srt) is False         # …the new one does not


def test_a_real_cue_passes(tmp_path):
    srt = tmp_path / "ok.srt"
    srt.write_text(ZH_CUE, encoding="utf-8")
    assert srt_has_cues(srt) is True


@pytest.mark.parametrize("srt", [None, Path("/nonexistent/x.srt")])
def test_missing_files_are_just_false(srt):
    assert srt_has_cues(srt) is False


# --------------------------------------------------------------------------
# 3. a cue-less srt costs the subtitle, not the beat
# --------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_beat_clip_survives_the_exact_2026_08_02_srt(tmp_path, monkeypatch):
    """End to end through the real ffmpeg: the same 1-byte srt that killed the
    weekend recap must now produce a beat (silently unsubtitled)."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(story.G, "WORK", work)

    raw = tmp_path / "wanraw.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=gray:s=128x64:d=1:r=8", str(raw)], check=True)
    mp3 = tmp_path / "b.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=24000:cl=mono", "-t", "1", str(mp3)], check=True)
    srt = tmp_path / "bz.srt"
    srt.write_text("\n")                      # the killer file

    dest = tmp_path / "beat.mp4"
    story._beat_clip(raw, mp3, srt, "AMZN", "Zippy", None, 1.0,
                     {"subtitles": True}, dest)
    assert dest.is_file() and dest.stat().st_size > 0


# --------------------------------------------------------------------------
# 4. the fallback can no longer swallow a finished render
# --------------------------------------------------------------------------
class _StoryStub:
    """Stands in for briefing.story with a scripted render_scenes_wan."""
    WanCompositingError = story.WanCompositingError

    def __init__(self, wan):
        self._wan = wan
        self.hyperframes_ran = False

    def render_scenes_wan(self, dec, cfg, date, out_dir, stem):
        return self._wan(out_dir, stem)

    def render_scenes(self, dec, cfg, date, out_dir, stem):
        self.hyperframes_ran = True
        p = out_dir / f"{stem}.mp4"
        p.write_bytes(b"slideshow")
        return p, [p], [{"symbol": "MARKET"}]


CFG = {"story": {"renderer": "wan"}}


def test_a_render_failure_still_falls_back(tmp_path):
    """Wan genuinely broke before producing anything — the fallback is right,
    and the metadata must say what actually ran."""
    def wan(out_dir, stem):
        raise RuntimeError("comfyui server unavailable")
    stub = _StoryStub(wan)
    used, briefing, _, _ = _story_scene_render(stub, {}, CFG, "2026-08-02",
                                               tmp_path, "weekend-recap_2026-08-02")
    assert used == "hyperframes" and stub.hyperframes_ran
    assert briefing.name == "weekend-recap_2026-08-02.mp4"


def test_a_compositing_failure_refuses_to_downgrade(tmp_path):
    """The 2026-08-02 case: raws done, assembly broke. Falling back here is
    destruction, not resilience — it must raise instead."""
    def wan(out_dir, stem):
        raise story.WanCompositingError("raws intact — assembly failed")
    stub = _StoryStub(wan)
    with pytest.raises(story.WanCompositingError):
        _story_scene_render(stub, {}, CFG, "2026-08-02", tmp_path, "x")
    assert not stub.hyperframes_ran


def test_a_wan_success_reports_wan(tmp_path):
    def wan(out_dir, stem):
        p = out_dir / f"{stem}.mp4"
        p.write_bytes(b"wan")
        return p, [p], [{"symbol": "MARKET"}]
    used, _, _, _ = _story_scene_render(_StoryStub(wan), {}, CFG, "2026-08-02",
                                        tmp_path, "x")
    assert used == "wan"


def test_plain_hyperframes_reports_hyperframes(tmp_path):
    stub = _StoryStub(None)
    used, _, _, _ = _story_scene_render(stub, {}, {"story": {"renderer": "hyperframes"}},
                                        "2026-08-02", tmp_path, "x")
    assert used == "hyperframes" and stub.hyperframes_ran
