#!/usr/bin/env python3
"""Is this still the presenter? — an objective identity score against a locked plate.

Until now identity was judged by eye off contact sheets. That worked for
"ref2va lost the face" but does not survive five near-neighbour configs, which is
exactly what the reference-stacking probes produce. So: a number.

YuNet detects the face, SFace embeds it, and we take the cosine similarity to
the presenter's locked plate. Both ship inside opencv (4.11, already installed) — no new dependency,
just two small ONNX files in briefing/assets/models/.

The stylist (briefing.host2_stylist) gates every plate and keyframe it draws
on this: embed() each candidate and _cos() it against the locked plate.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "briefing" / "assets" / "models"


def _nets(size: tuple[int, int]):
    import cv2
    det = cv2.FaceDetectorYN.create(
        str(MODELS / "yunet.onnx"), "", size, score_threshold=0.6,
        nms_threshold=0.3, top_k=5000)
    rec = cv2.FaceRecognizerSF.create(str(MODELS / "sface.onnx"), "")
    return det, rec


def _largest_face(det, bgr):
    """The biggest detected face, or None. Biggest = the subject, not a poster."""
    h, w = bgr.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(bgr)
    if faces is None or len(faces) == 0:
        return None
    return faces[np.argmax(faces[:, 3])]      # column 3 = box height


def embed(bgr, det=None, rec=None):
    """(embedding, face_box) for one BGR frame, or (None, None) if no face."""
    if det is None or rec is None:
        det, rec = _nets((bgr.shape[1], bgr.shape[0]))
    face = _largest_face(det, bgr)
    if face is None:
        return None, None
    aligned = rec.alignCrop(bgr, face)
    return rec.feature(aligned).flatten(), face


def _cos(a, b) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))

