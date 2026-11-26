"""The anatomy gate's arithmetic — the half that does not need a vision model.

`director.anatomy_ok` is what stands between a limb defect and a public upload,
and its verdict comes from OUR point counting, not from any model opinion: the
model localizes hands and feet in each band, and this module maps, dedupes and
counts them. That arithmetic is where the gate can fail silently — a wrong
coordinate mapping does not raise, it just counts one foot twice and refuses a
clean frame, or counts two as one and passes a defective one. So it is tested
here with the vision call stubbed out.

What is deliberately NOT tested: whether the model finds the limbs. That is a
measurement, not a unit test, and the measurement is recorded at _BLOCK_ON.
"""
from __future__ import annotations

import json

import pytest

from briefing import director


@pytest.fixture
def frame(tmp_path):
    from PIL import Image

    p = tmp_path / "frame.png"
    Image.new("RGB", (512, 512), "white").save(p)
    return p


def _cfg(**review):
    r = {"model": "stub", "bands": [2], "bbox_pad": 0.0, "band_overlap": 0.0,
         "global_tol": 60}
    r.update(review)
    return {"story": {"review": r}}


def _stub(monkeypatch, detections, bbox=(0, 0, 1000, 1000)):
    """Answer the bbox call once, then hand out `detections` per band, in order."""
    queue = list(detections)

    def fake(model, prompt, png, rv):
        if prompt is director._BBOX_PROMPT:
            if bbox is None:
                raise ValueError("no bbox")
            return {"bbox": list(bbox)}
        return queue.pop(0) if queue else {"hands": [], "feet": []}

    monkeypatch.setattr(director, "_vqc_chat", fake)


# --------------------------------------------------------------------------
# coordinate mapping
# --------------------------------------------------------------------------
def test_band_points_map_into_frame_coordinates():
    """A point in the BOTTOM half of the lower band is near the bottom of the
    frame, not near the bottom of the band."""
    pts = director._to_frame_pts([{"x": 500, "y": 1000, "desc": "left foot"}],
                                 0.0, 0.5, 1.0, 0.5)
    assert pts == [{"x": 500.0, "y": 1000.0, "desc": "left foot"}]


def test_one_limb_seen_in_two_overlapping_bands_maps_to_one_place():
    """The whole reason the mapping exists: without it the same foot reported by
    two bands looks like two feet, and the frame is refused for being clean."""
    upper = director._to_frame_pts([{"y": 833, "x": 300}], 0.0, 0.0, 1.0, 0.6)
    lower = director._to_frame_pts([{"y": 167, "x": 300}], 0.0, 0.4, 1.0, 0.6)
    assert abs(upper[0]["y"] - lower[0]["y"]) < 2      # both ≈ y=500 in the frame
    assert len(director._dedupe_pts(upper + lower, 60)) == 1


@pytest.mark.parametrize("bad", [
    "not a dict", 42, None,
    {"x": 1200, "y": 500},      # outside 0-1000
    {"x": -5, "y": 500},
    {"x": "left", "y": 500},    # non-numeric
])
def test_unusable_points_are_dropped_not_counted(bad):
    assert director._to_frame_pts([bad], 0.0, 0.0, 1.0, 1.0) == []


def test_dedupe_keeps_limbs_that_are_genuinely_apart():
    pts = [{"x": 100, "y": 100}, {"x": 120, "y": 110},   # same limb, jitter
           {"x": 800, "y": 100}]                          # the other limb
    assert len(director._dedupe_pts(pts, 60)) == 2


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------
def test_two_hands_two_feet_passes(frame, monkeypatch):
    _stub(monkeypatch, [
        {"hands": [{"x": 200, "y": 400}, {"x": 800, "y": 400}], "feet": []},
        {"hands": [], "feet": [{"x": 200, "y": 600}, {"x": 800, "y": 600}]},
    ])
    assert director.anatomy_ok(frame, _cfg()) is True


def test_three_feet_fails(frame, monkeypatch):
    _stub(monkeypatch, [
        {"hands": [], "feet": []},
        {"hands": [], "feet": [{"x": 150, "y": 600}, {"x": 500, "y": 600},
                               {"x": 900, "y": 600}]},
    ])
    assert director.anatomy_ok(frame, _cfg()) is False


def test_three_hands_is_reported_but_does_not_block(frame, monkeypatch):
    """Measured, not cautious: the hand channel came out INVERTED on real frames
    — it missed a genuine third hand and fired on a clean one. Blocking on it
    would refuse good uploads while still passing what it was added to catch."""
    _stub(monkeypatch, [
        {"hands": [{"x": 150, "y": 400}, {"x": 500, "y": 400}, {"x": 900, "y": 400}],
         "feet": []},
        {"hands": [], "feet": [{"x": 200, "y": 600}, {"x": 800, "y": 600}]},
    ])
    assert director.anatomy_ok(frame, _cfg()) is True


def test_block_on_is_configurable(frame, monkeypatch):
    _stub(monkeypatch, [
        {"hands": [{"x": 150, "y": 400}, {"x": 500, "y": 400}, {"x": 900, "y": 400}],
         "feet": []},
        {"hands": [], "feet": []},
    ])
    assert director.anatomy_ok(frame, _cfg(block_on=["hands", "feet"])) is False


def test_the_same_two_feet_seen_by_both_bands_still_passes(frame, monkeypatch):
    """The double-counting trap, end to end: overlapping bands both see the same
    pair of feet. Four raw points, two limbs, one clean frame."""
    _stub(monkeypatch, [
        {"hands": [], "feet": [{"x": 300, "y": 833}, {"x": 700, "y": 833}]},
        {"hands": [], "feet": [{"x": 300, "y": 167}, {"x": 700, "y": 167}]},
    ])
    assert director.anatomy_ok(frame, _cfg(band_overlap=0.1)) is True


def test_an_unusable_bbox_falls_back_to_the_whole_frame(frame, monkeypatch):
    _stub(monkeypatch, [
        {"hands": [{"x": 200, "y": 400}, {"x": 800, "y": 400}], "feet": []},
        {"hands": [], "feet": [{"x": 200, "y": 600}, {"x": 800, "y": 600}]},
    ], bbox=(0, 0, 0, 0))                      # degenerate — must not shrink to nothing
    assert director.anatomy_ok(frame, _cfg()) is True


def test_a_broken_check_returns_none_never_false(frame, monkeypatch):
    """None means "the check failed", and callers treat it as a pass. Returning
    False here would block every upload the moment ollama went down."""
    def boom(*a, **k):
        raise OSError("ollama is down")

    monkeypatch.setattr(director, "_vqc_chat", boom)
    assert director.anatomy_ok(frame, _cfg()) is None


def test_bad_json_from_the_model_returns_none(frame, monkeypatch):
    def boom(*a, **k):
        raise json.JSONDecodeError("nope", "", 0)

    monkeypatch.setattr(director, "_vqc_chat", boom)
    assert director.anatomy_ok(frame, _cfg()) is None


def test_bands_too_small_to_crop_return_none(tmp_path, monkeypatch):
    from PIL import Image

    p = tmp_path / "tiny.png"
    Image.new("RGB", (80, 80), "white").save(p)
    _stub(monkeypatch, [])
    assert director.anatomy_ok(p, _cfg(bands=[3])) is None


def test_missing_image_returns_none(tmp_path, monkeypatch):
    _stub(monkeypatch, [])
    assert director.anatomy_ok(tmp_path / "nope.png", _cfg()) is None


# --------------------------------------------------------------------------
# policy locks
# --------------------------------------------------------------------------
def test_feet_block_and_hands_do_not_by_default():
    assert director._BLOCK_ON == ("feet",)


def test_default_bands_overlap_rather_than_tile_exactly():
    """Two passes at different band counts, so a limb that straddles one split is
    whole in the other. Deduping is what makes the extra pass free."""
    assert director._DETECT_BANDS == (2, 3)
    assert 0 < director._BAND_OVERLAP < 0.2
