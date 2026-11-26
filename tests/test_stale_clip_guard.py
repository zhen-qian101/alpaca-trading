"""A failed LTX probe must never ship the PREVIOUS day's clip as today's.

`outdir` is keyed by symbol, not by date — briefing/.hf_work/tmp/host_SOXL holds
every SOXL clip ever rendered, each overwriting the last. So yesterday's
ltx_ia2v.mp4 is still on disk when today's probe runs, and the old guard tested
only `mp4.exists()`. When the probe died without writing, that existence check
passed, the mouth gate scored YESTERDAY's video (whose mouth moves perfectly
well, so it passed), and cache_write stamped today's key onto yesterday's
footage. The upscale was skipped too — its key hashes the source bytes, which
had not changed — so not one line of log marked it.

What shipped was today's audio over yesterday's picture, in yesterday's outfit.
It went out twice in the 2026-09-18 recap: SOXL in the English cut and DESK in
the 中文 one, both public before anyone noticed.

The signature is the one this subsystem keeps producing: nothing raises, nothing
logs, and only watching the video catches it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from briefing import host_briefing as HB


STALE = b"\x00PREVIOUS DAY'S CLIP\x00" * 64


@pytest.fixture
def clipdir(tmp_path, monkeypatch):
    """An outdir already holding yesterday's render, exactly as production does."""
    out = tmp_path / "host_SOXL"
    out.mkdir()
    (out / "ltx_ia2v.mp4").write_bytes(STALE)
    # yesterday's cache entry, under yesterday's key
    (out / "ltx_ia2v.meta.json").write_text(json.dumps({"key": "yesterdays-key"}))

    wav = tmp_path / "today.wav"
    wav.write_bytes(b"todays-narration-bytes")

    keyframe = tmp_path / "locked.png"
    keyframe.write_bytes(b"keyframe-bytes")

    # soundfile lives in the alpaca-media env, not the test venv; render_host_clip
    # only wants a duration out of it (same sys.modules stubbing as test_bar_grid)
    stub = type("_sf", (), {"read": staticmethod(lambda p: ([0] * 24000, 24000))})
    monkeypatch.setitem(sys.modules, "soundfile", stub)

    # pin everything that would otherwise reach the network or the GPU
    monkeypatch.setattr(HB, "audio_mode", lambda: "pinned")
    monkeypatch.setattr(HB, "_free_gpu", lambda *a, **k: None)
    monkeypatch.setattr(HB, "guide_frames", lambda w: [0, 160])
    monkeypatch.setattr(HB, "compose", lambda *a, **k: "a composed prompt")
    return out, wav, keyframe


def _run(clipdir, returncode: int, writes: bool, monkeypatch):
    out, wav, keyframe = clipdir

    def fake_run(cmd, **kw):
        if writes:
            (out / "ltx_ia2v.mp4").write_bytes(b"TODAY'S FRESH CLIP")
        return subprocess.CompletedProcess(cmd, returncode, "stdout", "probe exploded")

    monkeypatch.setattr(HB.subprocess, "run", fake_run)
    return HB.render_host_clip({"line": "hello", "mood": "calm"}, wav, out, {},
                               keyframe=keyframe, lang="en", host={"name": "谷雨"})


def test_nonzero_exit_refuses_to_ship_the_stale_clip(clipdir, monkeypatch):
    """The exact 2026-09-18 failure: probe fails, yesterday's mp4 is still there."""
    with pytest.raises(SystemExit) as e:
        _run(clipdir, returncode=1, writes=False, monkeypatch=monkeypatch)
    assert "render failed" in str(e.value)
    assert "exit 1" in str(e.value), "the exit code belongs in the message"


def test_stale_bytes_are_gone_rather_than_reused(clipdir, monkeypatch):
    """Clearing the target is what makes existence mean 'this run wrote it'."""
    out, _, _ = clipdir
    with pytest.raises(SystemExit):
        _run(clipdir, returncode=1, writes=False, monkeypatch=monkeypatch)
    assert not (out / "ltx_ia2v.mp4").exists(), (
        "yesterday's clip survived a failed render and would be shipped again"
    )


def test_todays_key_is_never_stamped_onto_yesterdays_footage(clipdir, monkeypatch):
    """The stamping is the damage: it makes the stale clip look freshly cached
    to every later run, so the error persists past the failure that caused it."""
    out, _, _ = clipdir
    with pytest.raises(SystemExit):
        _run(clipdir, returncode=1, writes=False, monkeypatch=monkeypatch)
    meta = json.loads((out / "ltx_ia2v.meta.json").read_text())
    assert meta["key"] == "yesterdays-key", "a failed render rewrote the cache key"


def test_exit_zero_with_no_output_is_still_a_failure(clipdir, monkeypatch):
    """Existence and exit status are checked together, not either/or."""
    with pytest.raises(SystemExit):
        _run(clipdir, returncode=0, writes=False, monkeypatch=monkeypatch)


def test_a_real_render_still_succeeds(clipdir, monkeypatch):
    """The guard must not break the happy path."""
    out, _, _ = clipdir
    monkeypatch.setattr(HB, "_mouth_alive", lambda *a, **k: (True, 0.1, 0.05, ""))
    monkeypatch.setattr(HB, "upscale_clip", lambda src, cfg: src)
    got = _run(clipdir, returncode=0, writes=True, monkeypatch=monkeypatch)
    assert got.read_bytes() == b"TODAY'S FRESH CLIP"
    assert json.loads((out / "ltx_ia2v.meta.json").read_text())["key"] != "yesterdays-key"


# --- BRIEFING_LOOK_LOCK: the repair lever -----------------------------------

def _approved_look(tmp_path, monkeypatch):
    """A host whose look for the date is drawn AND signed off."""
    kf = tmp_path / "assets" / "keyframes" / "chambray_stage16x9.png"
    kf.parent.mkdir(parents=True)
    kf.write_bytes(b"chambray")
    locked = tmp_path / "assets" / "keyframes" / "host2_stage16x9.png"
    locked.write_bytes(b"locked-warm-tee")
    hist = tmp_path / "assets" / "stylist"
    hist.mkdir(parents=True)
    (hist / "2026-09-18.json").write_text(json.dumps({
        "keyframe": "assets/keyframes/chambray_stage16x9.png",
        "approved": True,
    }))
    monkeypatch.setattr(HB, "ROOT", tmp_path)
    return {"stylist": True, "stylist_dir": "assets/stylist",
            "keyframe": "assets/keyframes/host2_stage16x9.png"}


def test_approved_look_wins_by_default(tmp_path, monkeypatch):
    """Regression guard: the lever must not change normal behaviour."""
    host = _approved_look(tmp_path, monkeypatch)
    monkeypatch.delenv("BRIEFING_LOOK_LOCK", raising=False)
    assert HB.day_keyframe("2026-09-18", host).name == "chambray_stage16x9.png"


def test_look_lock_holds_the_locked_keyframe(tmp_path, monkeypatch):
    """A late approval must not redress the host mid-repair: re-rendering one
    broken clip has to match the twelve siblings already on disk."""
    host = _approved_look(tmp_path, monkeypatch)
    monkeypatch.setenv("BRIEFING_LOOK_LOCK", "1")
    assert HB.day_keyframe("2026-09-18", host).name == "host2_stage16x9.png"
