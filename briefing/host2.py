#!/usr/bin/env python3
"""THE PRESENTER: 陈谷雨 · Rain Chen, the show's presenter since 2026-08-12.

His bible and the keyframe machinery his daily stylist draws with. The locked
plates live in briefing/assets/host2/ (local-only, not in this repository);
what production reads from here is:

  * the identity anchors the stylist's prompts are built from: HAIR, WARDROBE,
    FRAMING, STYLES, _STAGING and KEYFRAME_EXPRESSION;
  * the keyframe gates: the face-scale band (MIN/MAX_FACE_SCALE, MIN_CX),
    wall_seam / WALL_SEAM_MAX, face_detail and pick_keyframe;
  * _cfg (the hero-portrait klein settings) and _draw_supersampled.

His personality is carried by how he moves and dresses, never by the face: he
is easygoing, gentle and peaceful.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import comfy

ROOT = Path(__file__).resolve().parent.parent

# ==========================================================================
# THE BIBLE — the identity anchors. Change these and he becomes someone else.
# ==========================================================================
# A CREW CUT, on the user's third pass. The trajectory is one-directional and
# worth reading as a whole: shaggy fringe (the first plates, read oldest)
# -> short and tidy with a swept fringe -> clipped short all over. Each
# step got younger and cleaner, and this one removes the fringe as a variable
# entirely, which is the detail klein kept re-lengthening on its own.
HAIR = (
    "a clean crew cut: black hair clipped short and even all over, tapered "
    "close at the sides and back, short and flat on top, squared neatly at the "
    "temples, a crisp clean hairline at the forehead and at the nape, both ears "
    "completely uncovered, no fringe and no hair touching his forehead at all, "
    "neat matte texture with no gel shine, freshly barbered"
)

# Stripped to white tee + jeans on the user's third pass. The wardrobe has now
# lost every accessory it started with — the oxford collar, the sweatshirt, and
# finally the watch — and each removal was the right call for the same reason:
# fewer specified items means fewer things for klein to re-roll, and this brief
# is a base plate rather than a styled look.
#
# Kept in lockstep with host2.json's ref_desc: that string is what production
# consumes, and the two must not drift apart.
#
# NO CHEST EMBROIDERY, and that is a decision rather than an omission. The
# obvious move was to give the tee 「谷雨」 embroidered tone-on-tone on the
# chest. Measured over 18 draws it does not survive: klein
# renders the pair as a different invented glyph on every seed and, on the
# best-scoring plate of round 3, as a legible GOLD 「美」 — a wrong character,
# clearly readable, on a plate meant to be reused every day. The recipe's whole
# premise is that an identity anchor is a fixed reproducible landmark; a mark
# that re-rolls each seed is not an anchor, it is drift wearing one's coat. So
# the sweater is specified plain, and the wrist watch carries the load instead.
WARDROBE = (
    "wearing a plain white cotton crew-neck t-shirt with short sleeves ending "
    "mid-bicep, a relaxed easy fit with soft natural drape, completely plain "
    "with no logo, no graphic, no print, no embroidery, no lettering and no "
    "pattern of any kind, tucked loosely into mid-blue straight-leg jeans, "
    "plain, no rips and no patches, no belt visible, bare forearms and bare "
    "wrists, no watch, no bracelet, no necklace and no jewellery of any kind"
)

# Upper body down to the upper thigh, as the user asked; the crop matters
# because an unspecified lower half is a lower half that drifts. "No props, no
# furniture" is load-bearing.
FRAMING = (
    "standing three-quarter-length portrait cropped at mid-thigh, subject centred "
    "with generous headroom, full head visible and not cropped, arms relaxed at "
    "his sides, eye-level camera, 85mm portrait lens, soft key light from camera "
    "left with gentle fill and a soft rim light on his hair, plain seamless warm "
    "light-grey studio backdrop, no props, no furniture"
)

STYLES: dict[str, str] = {
    "photoreal": (
        "A photorealistic portrait photograph. Natural skin texture with visible "
        "pores and fine flyaway hairs, true-to-life colour, soft natural daylight, "
        "candid documentary realism, shallow depth of field, sharp focus on the eyes"
    ),
    "semireal": (
        "A soft semi-realistic digital illustration in modern Chinese webtoon style. "
        "Clean lineless rendering, smooth airbrushed shading, gently stylised "
        "features with slightly larger eyes, warm muted palette, human proportions, "
        "polished character-art finish"
    ),
}


def _cfg() -> dict:
    """Hero-portrait klein settings: 10 steps instead of the shipped 4 (which
    are tuned for throwaway prop icons), cfg left at 1.0 because klein is
    guidance-distilled."""
    cfg = json.loads((ROOT / "briefing" / "config.json").read_text())
    cfg.pop("_comment", None)
    for k in list(cfg):
        if isinstance(cfg[k], dict):
            cfg[k].pop("_comment", None)
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("comfyui", {}).setdefault("klein", {})["steps"] = 10
    cfg["comfyui"]["klein"]["gen_timeout_s"] = 420
    return cfg


# ==========================================================================
# THE RENDER KEYFRAME — 16:9, the frame LTX actually starts from.
# ==========================================================================
# _STAGING is not styling — it is CHANNEL IDENTITY: the left two thirds stay
# empty because the full-canvas slide layout puts the cards and pills there.
# If he stood anywhere else the overlays would land on his face.
# Positioning him is genuinely hard to say, and BOTH obvious phrasings fail in
# opposite directions:
#
#   "he stands in the RIGHT third of the frame" — held at 1280x704 (cx
#   0.70-0.80) and collapsed on the wider supersample canvas, where all ten
#   draws slid to cx 0.62-0.70, his shoulder under the slide cards.
#
#   "divide the frame into three equal vertical thirds ... the LEFT TWO THIRDS
#   are empty" — fixed the position (cx 0.76-0.81) and then klein DREW THE
#   DIVISION: a hard vertical wall corner at x=0.329-0.333, dead on the
#   one-third line, in every single draw (user-caught). Seam ratio 128-173
#   against a flat wall's 1.7-3.1.
#
# The lesson is the one this file keeps relearning: a word describing the
# COMPOSITION gets rendered as CONTENT. "Thirds" is geometry, and klein draws
# geometry. So the position is now stated as where his BODY is relative to the
# right EDGE — a fact about the subject, not a construction line — and the wall
# gets its own sentence insisting it is one unbroken surface. WALL_SEAM_MAX
# below is the gate that catches it when the wording is not enough, which on
# past form is the part to trust.
_STAGING = (
    "Wide 16:9 studio photograph. He stands far over to the RIGHT of the "
    "picture, his body close to the right edge of the frame, and everything to "
    "his left is empty wall. That wall is ONE single flat continuous surface of "
    "warm light-grey paint with a soft, natural plaster texture: unbroken and "
    "uninterrupted, with no corner, no edge, no seam, no join, no panel, no "
    "doorway, no skirting and no abrupt change of colour or brightness anywhere "
    "across it. It is lit by one soft key light from the upper left so it "
    "carries a gentle, gradual brightness gradient, and his figure casts a "
    "faint soft shadow. His whole figure is visible from mid-thigh up, both "
    "hands relaxed at his sides. No furniture, no props, no window.")

# SUPERSAMPLE. The keyframe is drawn at 1920x1056 and area-downscaled to the
# 1280x704 LTX consumes, because the face is the one thing this frame cannot
# spend more pixels on any other way: the framing is fixed at mid-thigh, so the
# face is ~0.185 of frame height either way, which is 224px on the 1216-tall
# portrait plate but only ~130px on a 704-tall keyframe. Drawing at 2 MP and
# resampling down is the only lever that does not change the framing.
#
# Measured on the shipped MSFT beat, contrast-invariant face detail (high-freq
# energy as a fraction of total) averaged over the whole rendered clip:
#     1280 direct   0.049   (and DECAYING: 0.068 at frame 0 -> 0.036 at the end)
#     1920 -> 1280  0.089   (holding: 0.136 -> 0.096)
# so it is ~1.8x more facial detail for the entire clip, not just frame 0. The
# mouth got better too, not worse: travel 0.2824 / window 0.1692 against the
# 1280 draw's 0.1621 / 0.0964.
#
# Two things that do NOT work, both measured, so nobody re-runs them:
#   * megapixels=2.0 on the references. Its own docstring advertises exactly
#     this ("a face-only plate at 2.0 MP buys real facial detail") and it is
#     right for stage 1, where the reference IS a face crop. Here the references
#     are half-body plates and it moves nothing: rel 0.052-0.071 against the
#     0.057-0.071 baseline.
#   * an intermediate 1600x880. Below the baseline (lap_n median 139.7 vs
#     148.4). The gain is not smooth in resolution — klein only starts resolving
#     finer facial structure near 2 MP.
# And the reason NOT to go further: at 2 MP klein also resolves individual
# TEETH, which downscale into hard segmented blocks that read as braces. That is
# survivable while the mouth is only slightly parted and would not be if the
# expression opened wider. See KEYFRAME_EXPRESSION.
SUPERSAMPLE = (1920, 1056)


# The empty wall must BE empty. A vertical discontinuity there is a drawn wall
# corner or panel edge, and it is not a cosmetic problem: the slide cards sit on
# that wall, LTX animates the whole clip from this frame, so a corner behind the
# cards is a line down the middle of the video for its full length.
#
# Calibrated on the frames that exist: flat walls measure 1.7-3.1, the "divide
# the frame into thirds" batch 128-173, and a frame with a dark strip down its
# left border 22.6. 8.0 sits in the empty space between the two populations.
# (A keyframe padded by extending the wall with a per-row median, whose join is
# a seam by construction, measures 894 — a fair check that the detector
# measures what it claims to.)
WALL_SEAM_MAX = 8.0
_SEAM_LEFT_FRAC = 0.50      # scan only wall: MIN_CX 0.58 keeps a body clear of this


def wall_seam(img) -> tuple[float, float]:
    """(peak-to-median ratio, x) of the strongest vertical edge in the empty
    left region.

    NUMPY ONLY, deliberately. Everything else in this file may lean on cv2
    because it runs in the comfy env, but no env on this box has cv2 AND pytest
    at once — so a cv2 implementation here is a production gate that the test
    suite can never execute. The maths is a luma conversion, a vertical box
    blur (so plaster mottling averages out while a full-height edge does not)
    and a column-wise gradient; none of it needs OpenCV for one still.
    """
    import numpy as np
    a = np.asarray(img, dtype=np.float64)
    h, w = a.shape[:2]
    g = a @ (0.114, 0.587, 0.299) if a.ndim == 3 else a       # BGR luma
    band = g[int(h * 0.15):int(h * 0.85), :int(w * _SEAM_LEFT_FRAC)]
    if band.shape[1] < 4 or band.shape[0] < 9:
        return 0.0, 0.0
    k = 9                                                     # vertical box blur
    c = np.cumsum(np.vstack([np.zeros((1, band.shape[1])), band]), axis=0)
    band = (c[k:] - c[:-k]) / k
    dx = np.abs(np.diff(band, axis=1)).mean(axis=0)
    med = float(np.median(dx)) or 1e-6
    i = int(np.argmax(dx))
    return float(dx[i]) / med, i / w


def face_detail(img) -> float:
    """Contrast-invariant facial detail: high-frequency energy as a FRACTION of
    the face crop's total energy, on the face resampled to a fixed 200px.

    The fraction matters. Plain Laplacian variance rewards contrast as well as
    detail, and measured on these keyframes it mis-ranked them outright — it put
    the shipped frame 9% BELOW the one it replaced when normalising for contrast
    makes them 0.060 and 0.059, the same picture. Any sharpness decision taken on
    the un-normalised number is a decision about lighting.
    """
    import cv2
    import numpy as np
    from .h3_lipsync_score import _detector
    det = _detector((img.shape[1], img.shape[0]))
    _, faces = det.detect(img)
    if faces is None or not len(faces):
        return 0.0
    fc = faces[np.argmax(faces[:, 3])]
    x, y, w, h = [int(v) for v in fc[:4]]
    crop = img[max(0, y):y + h, max(0, x):x + w]
    if crop.size == 0:
        return 0.0
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gn = cv2.resize(g, (200, 200), interpolation=cv2.INTER_AREA).astype("float64")
    v = gn.var()
    return float(cv2.Laplacian(gn, cv2.CV_64F).var() / v) if v > 1e-6 else 0.0


# How far below the best identity a candidate may sit and still be considered.
IDENTITY_SLACK = 0.06


def pick_keyframe(rows: list[dict]) -> dict | None:
    """Choose among candidates that already PASSED every gate.

    Identity is a gate, not a score. Every passing candidate is already over
    the identity gate, and in practice they clear it by a mile — a supersampled batch
    runs 0.81-0.91 against a 0.68 bar — so ranking on identity alone spends the
    whole choice on a margin that no longer buys anything, and it lands on a soft
    frame roughly as often as not. Measured: three stylist looks picked on
    identity gained 1.56x, 1.29x and NOTHING in face detail from supersampling,
    where a hand-pick balancing both gave 1.9x.

    So: keep every candidate within IDENTITY_SLACK of the best identity, then
    take the sharpest of those. Sharpness is the thing with no ceiling here — it
    sets the detail ceiling for every frame of the clip — while identity above
    the gate has already done its job.
    """
    if not rows:
        return None
    best_id = max(r["identity"] for r in rows)
    near = [r for r in rows if r["identity"] >= best_id - IDENTITY_SLACK]
    return max(near, key=lambda r: (r.get("detail", 0.0), r["identity"]))


def _draw_supersampled(cfg, refs, prompt, dest, seed, width=1280, height=704):
    """One keyframe candidate: draw at SUPERSAMPLE, area-downscale to (width,
    height). host2_stylist.draw_keyframe draws every day's keyframe through it."""
    import cv2
    sw, sh = SUPERSAMPLE
    big = dest.with_name(dest.stem + "_ss.png")
    if not comfy.klein_storyboard_multi(cfg, refs, prompt, big,
                                        width=sw, height=sh, seed=seed):
        return False
    img = cv2.imread(str(big))
    big.unlink(missing_ok=True)
    if img is None:
        return False
    cv2.imwrite(str(dest), cv2.resize(img, (width, height),
                                      interpolation=cv2.INTER_AREA))
    return True


# The face-scale band: 0.170-0.205, bounded BOTH ways because too tight
# propagates a hip crop into every render and too wide shrinks the face below the
# size identity survives at. The locked plate measures 0.184, comfortably inside
# it, so the band stays as set rather than being re-derived from one plate.
MAX_FACE_SCALE = 0.205
MIN_FACE_SCALE = 0.170
MIN_CX = 0.58          # face centre must sit in the right third

# THE MOUTH. Every clip of the day opens on this one frame, so whatever the
# mouth is doing here is what the viewer sees at every cut. The first keyframe
# asked for a "warm, natural, closed-lip smile" and klein drew a broad grin with
# the full upper teeth row anyway (user-caught 2026-08-18: "every clip starts
# with the same smile, which looks not very natural during clip to clip
# transition"), and the reason is that BOTH reference plates are themselves
# open-mouthed — klein follows the references over the words.
#
# So the wording below is necessary but not sufficient: it moves the
# DISTRIBUTION, and the winner still has to be chosen by looking at the mouth at
# zoom. Do not trust a pixel statistic here. The video QC's `gape` is a motion
# signal and is meaningless on a still — measured, it ranked a closed-lip
# candidate (0.0911) ABOVE the teeth-baring production plate (0.1016), and a
# teeth-brightness statistic ranked a closed-lip plate highest of all, because
# at iod~46px the mouth ROI is mostly lit skin.
#
# A parted mouth is also the better frame 0 on its own merits: LTX continues
# from it into speech, so a mouth caught mid-word needs no reset, where a held
# grin reads as the take starting over at every cut.
KEYFRAME_EXPRESSION = (
    "His lips are gently parted as if he has just begun to speak, a small "
    "relaxed opening between softly relaxed lips, his expression warm and "
    "friendly but calm. He is not grinning and not showing a broad smile; his "
    "teeth are barely visible if at all."
)
