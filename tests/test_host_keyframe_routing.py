"""The day's opening frame must belong to the presenter who is presenting.

A presenter's stylist writes its own history (陈谷雨's into
assets/host2/stylist), and day_keyframe() reads the one named by the
presenter's own `stylist_dir`. Reading a shared or default path is the bug
this file exists to prevent, and its signature is that nothing raises — the
render simply opens on another presenter's plate, in another presenter's
clothes, and only watching the video catches it.

The same shape of failure has already happened twice in this subsystem: a deck
written in his words read out in another presenter's voice, and a composer
writing the wrong pronoun into his render prompt. Both were silent. So there is
no fallback presenter either: a presenter with no look and no `keyframe` stops
the render.
"""
from __future__ import annotations

import json

import pytest

from briefing.host_briefing import ROOT, day_keyframe

HOSTS = ROOT / "briefing"


def _host(name: str) -> dict:
    return json.loads((HOSTS / name).read_text())


@pytest.fixture
def guyu():
    # host2.json is a local-only presenter asset and gitignored (see
    # .gitignore). Where it is absent (CI, a fresh clone) there
    # is no 陈谷雨 to route, and his tests have nothing to check.
    if not (HOSTS / "host2.json").exists():
        pytest.skip("briefing/host2.json is local-only and not in this checkout")
    return _host("host2.json")


def test_guyu_falls_back_to_his_own_fixed_keyframe(guyu):
    """With no stylist look on record, his fixed frame is the floor — an
    off-model host must never ship because of an outfit."""
    kf = day_keyframe("2099-01-01", guyu)
    assert kf.exists()
    assert "assets/host2/" in kf.as_posix()


def test_a_missing_fixed_keyframe_is_loud(guyu):
    """The fallback floor failing is the one case that must NOT be silent."""
    broken = {**guyu, "stylist": False, "keyframe": "briefing/nope.png"}
    with pytest.raises(SystemExit):
        day_keyframe(None, broken)


def test_the_stylist_dir_is_declared_and_real(guyu):
    """host2.json must point day_keyframe at his own history."""
    assert guyu["stylist_dir"] == "briefing/assets/host2/stylist"
    assert "host2" in guyu["keyframe"]


def test_the_stylist_module_writes_its_own_history():
    """generate.py and run_stylist.sh dispatch on the presenter; the stylist
    they dispatch to must write where that presenter's `stylist_dir` reads."""
    from briefing import host2_stylist
    assert host2_stylist.HISTORY_DIR.as_posix().endswith("assets/host2/stylist")


def test_there_is_no_fallback_presenter():
    """No look, no `keyframe`: the render stops rather than opening on some
    other presenter's frame."""
    with pytest.raises(SystemExit):
        day_keyframe(None, {"name": "X", "stylist": False})
    with pytest.raises(SystemExit):
        day_keyframe("2099-01-01", {"name": "X"})


def test_no_stylist_dir_means_no_history_lookup(tmp_path):
    """Without a `stylist_dir` the presenter's own fixed keyframe is used —
    no default history directory is consulted on its behalf."""
    kf = tmp_path / "locked.png"
    kf.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert day_keyframe("2099-01-01", {"name": "X", "keyframe": str(kf)}) == kf
