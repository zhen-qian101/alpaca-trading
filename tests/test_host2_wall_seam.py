"""The empty wall behind the host must actually be empty.

A vertical discontinuity in the left region is a drawn wall corner or a panel
edge, and it is not cosmetic: the slide cards sit on that wall and LTX animates
the entire clip from this one frame, so a corner there is a line down the middle
of the finished video for its full length.

It got in through the PROMPT. The staging was reworded to fix a composition
problem — "divide the frame into three equal vertical thirds ... the LEFT TWO
THIRDS are completely empty wall" — and klein drew the division, putting a hard
corner at x=0.329-0.333 in every single draw (user-caught 2026-08-18). Wording
describing the frame's GEOMETRY comes back as CONTENT; the same family as
naming footwear pulling the camera out to a full-body shot.

Wording alone has now failed twice on this frame (a "closed-lip smile" that drew
teeth, and this), so the gate is the part that has to hold. These tests pin the
threshold against real frames rather than synthetic ones wherever they survive
in the repo, and against constructed edges otherwise.
"""
from __future__ import annotations

import numpy as np
import pytest

from briefing.host2 import WALL_SEAM_MAX, _STAGING, wall_seam

# wall_seam is deliberately numpy-only. No env on this box carries cv2 AND
# pytest at once, so a cv2 implementation would have made this whole file
# permanently skipped — a production gate the suite could never execute.
W, H = 1280, 704


def _wall(value: int = 170) -> np.ndarray:
    """A flat, softly-mottled wall — what a good keyframe's left side is."""
    rng = np.random.default_rng(7)
    img = np.full((H, W, 3), value, np.uint8)
    noise = rng.normal(0, 1.4, (H, W, 1)).astype(np.float32)
    grad = np.linspace(-6, 6, W, dtype=np.float32)[None, :, None]
    return np.clip(img + noise + grad, 0, 255).astype(np.uint8)


def test_a_flat_wall_passes():
    ratio, _ = wall_seam(_wall())
    assert ratio <= WALL_SEAM_MAX, f"a flat wall scored {ratio:.1f}"


@pytest.mark.parametrize("x", [0.33, 0.20, 0.45])
def test_a_drawn_corner_is_caught(x):
    """The observed failure: a step in brightness down a full-height column."""
    img = _wall()
    col = int(W * x)
    img[:, :col] = np.clip(img[:, :col].astype(np.int16) + 22, 0, 255).astype(np.uint8)
    ratio, at = wall_seam(img)
    assert ratio > WALL_SEAM_MAX, f"missed a corner at x={x} (scored {ratio:.1f})"
    assert abs(at - x) < 0.02, f"located the seam at {at:.3f}, expected {x}"


def test_a_dark_border_strip_is_caught():
    """The second defect the same gate found: a dark band at the frame edge."""
    img = _wall()
    img[:, :8] = 90
    ratio, at = wall_seam(img)
    assert ratio > WALL_SEAM_MAX
    assert at < 0.02


def test_a_soft_lighting_gradient_is_not_a_seam():
    """The staging asks for a brightness gradient across the wall. A gradual
    ramp must not read as an edge, or every correct frame fails the gate."""
    img = _wall()
    ramp = np.linspace(0, 40, W, dtype=np.float32)[None, :, None]
    img = np.clip(img.astype(np.float32) + ramp, 0, 255).astype(np.uint8)
    ratio, _ = wall_seam(img)
    assert ratio <= WALL_SEAM_MAX, f"a smooth gradient scored {ratio:.1f}"


def test_the_staging_does_not_describe_the_frame_as_divisible():
    """The prompt-side half of the fix. These words made klein draw geometry."""
    low = _STAGING.lower()
    for banned in ("divide the frame", "thirds", "third of the frame"):
        assert banned not in low, f"staging says {banned!r} again"
    # and it must still demand an unbroken wall
    assert "continuous" in low
    assert "no corner" in low
