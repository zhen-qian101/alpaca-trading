#!/usr/bin/env python3
"""Score a rendered clip: does the mouth follow the audio, and how does it move?

host_briefing's lip-sync gate calls score(mp4, wav) on every rendered clip.

WHY THIS WAS REWRITTEN. The first version found the mouth by motion-argmax and
sized the box as a fixed fraction of the FRAME. Both halves were wrong in ways
that quietly produced confident numbers:

  * circular — the box was located by the very motion it then measured, so on a
    barely-moving clip it could settle on a chin and report a low gape;
  * frame-relative — a config that changed framing moved every mouth number
    without the mouth doing anything differently. Measured face scale ranges from
    0.32 to 0.57 of frame height across our own runs, so cross-config comparisons
    of the old `gape95` were never valid.

Now the mouth comes from YuNet's landmarks (it returns both mouth corners and
both eyes), the ROI is sized in units of INTER-OCULAR DISTANCE, and the crop is
tracked per frame and resampled to a fixed grid. Framing, face size and head
translation therefore cancel out of every mouth metric.

Metrics
    margin   lip-sync. Correlation of mouth motion with the audio's loudness
             envelope, minus the best of two misaligned controls (time-reversed,
             and circularly shifted by half). Clearly positive => the mouth
             follows THIS audio and not merely something speech-shaped.
    gape95   95th-pct dark fraction of the mouth ROI — how wide it gets.
    gape_rng p95 - p10. The honest "too open" number: a puppet holding its mouth
             open has a high p10 too, real speech returns to closed. High gape95
             with a LOW p10 is just an animated talker.
    silence  mean gape over the quietest 20% of audio frames. Should be near
             zero; separates "mouth too open" from "not animated" outright.
    head     mean per-frame face-centre displacement, in inter-ocular units.
    hands    mean inter-frame motion in the band below the face, after removing
             the face's own translation so a drifting crop is not scored as
             gesturing.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "briefing" / "assets" / "models"

MOUTH_W_IOD = 1.10      # ROI width  as a multiple of inter-ocular distance
MOUTH_H_IOD = 0.80      # ROI height as a multiple of inter-ocular distance
GRID = (48, 96)         # every mouth crop is resampled to this (h, w)


def _detector(size):
    import cv2
    return cv2.FaceDetectorYN.create(str(MODELS / "yunet.onnx"), "", size,
                                     score_threshold=0.6, nms_threshold=0.3, top_k=5000)


def _track(frames_bgr):
    """Per frame: (mouth crop on a fixed grid, face median, centre, iod, box).

    Frames with no detected face inherit the previous frame's geometry, so one
    dropped detection does not punch a hole in the motion signal.
    """
    import cv2
    det = _detector((frames_bgr[0].shape[1], frames_bgr[0].shape[0]))
    out, last = [], None
    for f in frames_bgr:
        det.setInputSize((f.shape[1], f.shape[0]))
        _, faces = det.detect(f)
        geom = last
        if faces is not None and len(faces):
            fc = faces[np.argmax(faces[:, 3])]
            reye, leye = fc[4:6], fc[6:8]
            rmc, lmc = fc[10:12], fc[12:14]
            iod = float(np.linalg.norm(np.array(reye) - np.array(leye))) or 1.0
            centre = ((np.array(rmc) + np.array(lmc)) / 2.0)
            geom = {"centre": centre, "iod": iod, "box": fc[:4].copy(),
                    "fcentre": np.array([fc[0] + fc[2] / 2, fc[1] + fc[3] / 2])}
            last = geom
        if geom is None:
            continue
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        cx, cy = geom["centre"]
        hw, hh = geom["iod"] * MOUTH_W_IOD / 2, geom["iod"] * MOUTH_H_IOD / 2
        x0, x1 = int(max(0, cx - hw)), int(min(g.shape[1], cx + hw))
        y0, y1 = int(max(0, cy - hh)), int(min(g.shape[0], cy + hh))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        crop = cv2.resize(g[y0:y1, x0:x1], (GRID[1], GRID[0]), interpolation=cv2.INTER_AREA)
        bx, by, bw, bh = [int(v) for v in geom["box"]]
        face = g[max(0, by):by + bh, max(0, bx):bx + bw]
        out.append({"crop": crop, "fmed": float(np.median(face)) if face.size else 128.0,
                    "iod": geom["iod"], "fcentre": geom["fcentre"], "box": (bx, by, bw, bh),
                    "gray": g})
    return out


def _envelope(wav: Path, n: int) -> np.ndarray:
    import soundfile as sf
    w, sr = sf.read(wav)
    w = w if w.ndim == 1 else w.mean(1)
    edges = np.linspace(0, len(w), n + 1).astype(int)
    return np.array([np.sqrt(np.mean(w[a:b] ** 2) + 1e-12) if b > a else 0.0
                     for a, b in zip(edges[:-1], edges[1:])])


def _z(x):
    s = x.std()
    return (x - x.mean()) / (s if s > 1e-9 else 1.0)


def _corr(a, b) -> float:
    return float(np.mean(_z(a) * _z(b)))


def score(video: Path, audio: Path) -> dict:
    import cv2
    import imageio.v3 as iio
    import soundfile as sf

    frames = [cv2.cvtColor(f, cv2.COLOR_RGB2BGR) for f in iio.imiter(video, plugin="pyav")]
    tr = _track(frames)
    if len(tr) < 8:
        return {"faces": len(tr)}

    crops = np.stack([t["crop"] for t in tr])
    # Mouth motion on TRACKED, size-normalised crops: head movement and framing
    # cannot leak into this the way they did into a fixed frame-relative box.
    d = np.abs(np.diff(crops, axis=0)).mean(axis=(1, 2))
    motion = np.concatenate([[d[0]], d])

    fmed = np.array([t["fmed"] for t in tr])[:, None, None]
    gape = (crops < 0.55 * fmed).mean(axis=(1, 2))

    fc = np.stack([t["fcentre"] for t in tr])
    iod = np.array([t["iod"] for t in tr])
    head = float(np.mean(np.linalg.norm(np.diff(fc, axis=0), axis=1) / iod[1:]))

    # Hands: motion below the face box, with the face's own translation removed
    # so a drifting crop does not read as gesturing.
    hands_vals = []
    for i in range(1, len(tr)):
        bx, by, bw, bh = tr[i]["box"]
        y = min(tr[i]["gray"].shape[0] - 1, by + bh)
        a, b = tr[i - 1]["gray"][y:], tr[i]["gray"][y:]
        if a.shape != b.shape or a.size == 0:
            continue
        shift = np.linalg.norm(fc[i] - fc[i - 1])
        hands_vals.append(max(0.0, float(np.abs(a - b).mean()) - shift))
    hands = float(np.mean(hands_vals)) if hands_vals else 0.0

    info = sf.info(str(audio))
    n = min(len(motion), max(4, int(round(info.duration * 24))))
    m, env = motion[:n], _envelope(audio, n)
    g = gape[:n]

    true = _corr(m, env)
    rev = _corr(m, env[::-1])
    shift_c = _corr(m, np.roll(env, n // 2))
    quiet = env <= np.percentile(env, 20)

    # SCREEN-RELATIVE mouth size. gape95 is deliberately scale-invariant so that
    # framing changes cannot fake a mouth change — which also means it cannot see
    # a real improvement made BY changing the framing. "The mouth looks too wide"
    # is a complaint about how much screen the open mouth occupies, so measure
    # that directly: open-mouth area as a fraction of the whole frame (x1000).
    fh, fw = frames[0].shape[:2]
    roi_area = np.mean([(MOUTH_W_IOD * t["iod"]) * (MOUTH_H_IOD * t["iod"]) for t in tr])
    screen = float(np.percentile(g, 95) * roi_area / (fh * fw) * 1000.0)

    # Windowed travel: the whole-clip range passes a mouth that is alive for
    # 5 s and frozen after (user-caught 2026-08-11: broken clip windows
    # 0.032/0.017/…/0.005 vs healthy clips never under 0.035). Per ~4 s
    # window, p95-p10; the min is the gate's second number.
    win = 96
    wins = [float(np.percentile(g[s:s + win], 95) - np.percentile(g[s:s + win], 10))
            for s in range(0, max(1, len(g) - win // 2), win)
            if len(g[s:s + win]) >= win // 2] or [0.0]

    return {"faces": len(tr), "scored": n,
            "true": true, "reversed": rev, "shifted": shift_c,
            "margin": true - max(rev, shift_c),
            "screen_mouth": screen,
            "gape95": float(np.percentile(g, 95)),
            "gape_rng": float(np.percentile(g, 95) - np.percentile(g, 10)),
            "gape_win_min": min(wins),
            "silence": float(g[quiet].mean()) if quiet.any() else float("nan"),
            "head": head, "hands": hands,
            "scale": float(np.mean([t["box"][3] for t in tr]) / frames[0].shape[0])}

