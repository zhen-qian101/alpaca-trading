"""The look sign-off gate, and the dashboard route that writes it.

WHY THIS EXISTS. The stylist runs at 09:35 ET and the render at 20:00 ET, so a
human has ~10 hours to look at the day's outfit and keyframe. Until 2026-08-20
nothing used that window: whatever the stylist drew went straight into the
video, and the first review was the finished render — where the only remaining
lever is a re-render. That is how 2026-08-19 shipped with a keyframe whose
smile had drifted back toward a look the user had already rejected.

The gate is deliberately shaped so it can never cost a video. An unapproved
look takes the SAME path as one that failed its identity gates: the host's
locked keyframe stands, the render proceeds on time, and the presenter is
merely wearing yesterday's shirt. A gate that could block tonight's render
would be a worse failure than any outfit, so the tests below pin the
fail-safe direction as hard as they pin the gate itself.
"""
from __future__ import annotations

import json

import pytest

from briefing.host_briefing import day_keyframe
from web import server as web_server


HOST = {
    "name": "Test Presenter",
    "stylist": True,
    "stylist_dir": "briefing/assets/host2/stylist",
    "keyframe": "briefing/assets/host2/keyframes/host2_stage16x9.png",
}


@pytest.fixture
def looks(tmp_path, monkeypatch):
    """A private stylist dir + assets tree, so nothing here reads or writes the
    real looks. Both modules are re-rooted, because the gate lives in
    host_briefing and the write lives in web.server and they must agree."""
    root = tmp_path
    sdir = root / "briefing" / "assets" / "host2" / "stylist"
    kdir = root / "briefing" / "assets" / "host2" / "keyframes"
    sdir.mkdir(parents=True)
    kdir.mkdir(parents=True)
    (kdir / "day.png").write_bytes(b"\x89PNG\r\n\x1a\n today")
    (kdir / "host2_stage16x9.png").write_bytes(b"\x89PNG\r\n\x1a\n locked")

    monkeypatch.setattr("briefing.host_briefing.ROOT", root)
    monkeypatch.setattr(web_server, "ROOT", root)
    monkeypatch.setattr(web_server, "BRIEFING_CONFIG_FILE",
                        root / "briefing" / "config.json")
    (root / "briefing" / "config.json").write_text(json.dumps(
        {"host": {"host_json": "host2.json", "stylist": True}}))
    (root / "briefing" / "host2.json").write_text(json.dumps(HOST))

    def write(date, **extra):
        rec = {
            "weather": {"tmax_c": 31.4, "tmin_c": 20.7, "sky": "overcast",
                        "precip_mm": 0.0, "season": "summer"},
            "style": {"outfit": "a plain olive henley", "slug": "olive-henley",
                      "rationale": "warm and overcast"},
            "plate_identity": 0.953,
            "keyframe_identity": 0.856,
            "plate": "briefing/assets/host2/keyframes/day.png",
            "keyframe": "briefing/assets/host2/keyframes/day.png",
            **extra,
        }
        (sdir / f"{date}.json").write_text(json.dumps(rec))
        return rec

    write.dir = sdir
    return write


# ----------------------------------------------------------------- the gate
def test_an_unapproved_look_does_not_reach_the_render(looks):
    """THE regression. The look exists, its keyframe exists, and it is still
    not what renders — because nobody signed it off."""
    looks("2026-09-01")
    assert day_keyframe("2026-09-01", HOST).name == "host2_stage16x9.png"


def test_an_approved_look_does_reach_the_render(looks):
    looks("2026-09-01", approved=True)
    assert day_keyframe("2026-09-01", HOST).name == "day.png"


def test_an_unapproved_look_never_blocks_the_render(looks, capsys):
    """The fail-safe direction, stated as its own test because it is the whole
    reason this gate is allowed to exist: no exception, no SystemExit, a real
    usable keyframe, and a line in the log saying what happened."""
    looks("2026-09-01")
    kf = day_keyframe("2026-09-01", HOST)
    assert kf.exists()
    assert "not signed off" in capsys.readouterr().out


def test_approval_is_true_not_merely_present(looks):
    """`"approved": false` must read as refused, not as 'the key is there'."""
    looks("2026-09-01", approved=False)
    assert day_keyframe("2026-09-01", HOST).name == "host2_stage16x9.png"


def test_a_look_whose_keyframe_vanished_still_falls_back(looks):
    """Approval is not a promise the file exists — the disk is still checked."""
    looks("2026-09-01", approved=True,
          keyframe="briefing/assets/host2/keyframes/gone.png")
    assert day_keyframe("2026-09-01", HOST).name == "host2_stage16x9.png"


# --------------------------------------------------------------- the reader
def test_build_look_reports_what_will_actually_render(looks):
    looks("2026-09-01")
    assert web_server.build_look("2026-09-01")["renders_with"] == "locked"
    looks("2026-09-01", approved=True)
    assert web_server.build_look("2026-09-01")["renders_with"] == "look"


def test_unjudged_and_refused_are_distinguishable(looks):
    """The UI shows three states, so the API must carry three: absent (nobody
    has looked), False (looked and refused), True."""
    looks("2026-09-01")
    assert web_server.build_look("2026-09-01").get("approved") is None
    looks("2026-09-01", approved=False)
    assert web_server.build_look("2026-09-01")["approved"] is False


def test_a_missing_look_is_not_an_error(looks):
    """Before the stylist has run, the page must say 'no look' rather than
    show a failure — this is the normal state every morning."""
    out = web_server.build_look("2026-09-02")
    assert out["exists"] is False and "error" not in out
    assert out["renders_with"] == "locked"


def test_the_look_follows_the_presenting_host(looks, tmp_path, monkeypatch):
    """Per-presenter, resolved from config — the same trap
    test_host_keyframe_routing.py exists for. Point config at a different host
    and the dashboard must read THAT host's stylist dir."""
    other = tmp_path / "briefing" / "assets" / "hostX" / "stylist"
    other.mkdir(parents=True)
    (tmp_path / "briefing" / "hostX.json").write_text(json.dumps(
        {"name": "Other", "stylist_dir": "briefing/assets/hostX/stylist"}))
    (tmp_path / "briefing" / "config.json").write_text(json.dumps(
        {"host": {"host_json": "hostX.json", "stylist": True}}))
    looks("2026-09-01", approved=True)          # written into host2's dir

    out = web_server.build_look("2026-09-01")
    assert out["host"] == "Other"
    assert out["exists"] is False, "read the wrong presenter's look"


# ---------------------------------------------------------------- the write
def test_approval_round_trips(looks):
    looks("2026-09-01")
    out = web_server.set_look_approval("2026-09-01", True)
    assert out["approved"] is True and out["renders_with"] == "look"
    assert day_keyframe("2026-09-01", HOST).name == "day.png"


def test_withdrawing_approval_puts_the_locked_frame_back(looks):
    looks("2026-09-01", approved=True)
    web_server.set_look_approval("2026-09-01", False)
    assert day_keyframe("2026-09-01", HOST).name == "host2_stage16x9.png"
    rec = json.loads((looks.dir / "2026-09-01.json").read_text())
    assert "approved" not in rec and "approved_at" not in rec


def test_the_write_touches_only_the_approval_keys(looks):
    """The request body is never persisted as file content. Whatever else the
    record holds — the outfit, the identity scores, the image paths — must come
    back byte-for-byte, or a hostile POST could redress the presenter."""
    before = looks("2026-09-01")
    web_server.set_look_approval("2026-09-01", True)
    after = json.loads((looks.dir / "2026-09-01.json").read_text())
    for k in ("weather", "style", "plate_identity", "keyframe_identity",
              "plate", "keyframe"):
        assert after[k] == before[k], f"{k} was modified by an approval"
    assert set(after) - set(before) == {"approved", "approved_at", "approved_by"}


def test_a_date_that_is_not_a_date_never_becomes_a_path(looks):
    """The traversal case. These must be refused on shape, before anything
    reaches the filesystem."""
    for bad in ("../../.env", "2026-09-01/../../x", "..", "", "2026-9-1",
                "2026-09-01.json"):
        assert web_server._look_path(bad) is None, bad
        assert "error" in web_server.set_look_approval(bad, True), bad


def test_approving_a_look_that_does_not_exist_is_refused(looks):
    out = web_server.set_look_approval("2026-09-09", True)
    assert "error" in out
    assert not (looks.dir / "2026-09-09.json").exists(), "created a look file"


def test_asset_route_refuses_a_path_outside_the_asset_tree(looks, tmp_path):
    """The look record names the image to serve. It is written by our own
    stylist, but it is still data — and data that names a file to serve is a
    traversal question."""
    secret = tmp_path / "secret.env"
    secret.write_text("DASHBOARD_TOKEN=hunter2")
    looks("2026-09-01", approved=True, keyframe="../../../secret.env")
    assert web_server._look_asset("2026-09-01", "keyframe") is None


def test_asset_route_refuses_an_unknown_kind(looks):
    looks("2026-09-01", approved=True)
    assert web_server._look_asset("2026-09-01", "../../etc/passwd") is None
    assert web_server._look_asset("2026-09-01", "plate") is not None
