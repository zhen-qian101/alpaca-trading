#!/usr/bin/env python3
"""LTX-2.3 host-clip helpers used by the production recap (briefing.host_briefing).

  * tts() -- the presenter's voice, one wav per beat, run in the qwen_tts venv;
  * _free_gpu() -- free every ComfyUI on the box before a render;
  * strip_held_gesture() -- remove a hand held out and dwelling from a prompt;
  * guide_frames() -- where to re-anchor the keyframe: only in the presenter's
    pauses.

Production renders each clip through host_briefing.render_host_clip, which
drives briefing/ltx_ia2v_probe.py.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = "/mnt/sandisk/qwen_tts/venv/bin/python"


def tts(lines: list[str], outdir: Path, lang: str = "en",
        host_json: Path | None = None) -> list[Path]:
    """The presenter's voice, one wav per beat. Runs in the qwen_tts venv (torch 2.13).

    `host_json` picks WHICH presenter: None = the configured presenter (voice.py
    resolves config host.host_json). Nothing else about the call differs —
    presenters are the same pipeline pointed at different reference clips.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    payload = outdir / "lines.json"
    payload.write_text(json.dumps(lines, ensure_ascii=False))
    cmd = [VENV_PY, str(ROOT / "briefing" / "voice.py"),
           "--lines", str(payload), "--lang", lang, "--outdir", str(outdir)]
    if host_json:
        cmd += ["--host-json", str(host_json)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout[-1500:], r.stderr[-1500:])
        raise SystemExit("  [ltx-story] TTS failed")
    wavs = sorted(outdir.glob("[0-9]*.wav"))
    if len(wavs) != len(lines):
        raise SystemExit(f"  [ltx-story] expected {len(lines)} wavs, got {len(wavs)}")
    return wavs


# EVERY ComfyUI on the box, not just the one we are about to drive. Freeing only
# 8189 left the other server sitting on ~1 GB, and that was the whole difference
# between a 26.3 s render OOMing in stage 2 and the identical render succeeding.
# The near-miss cost more than the GB: it very nearly got written down as a "~26 s
# maximum shot length", which would have been a permanent, invented limit.
PORTS = (8188, 8189)


def _free_gpu() -> None:
    for port in PORTS:
        subprocess.run(["curl", "-s", "-X", "POST", f"http://127.0.0.1:{port}/free",
                        "-H", "Content-Type: application/json",
                        "-d", '{"unload_models":true,"free_memory":true}'],
                       capture_output=True)


# A HAND HELD OUT, DWELLING. The same rule as the hand-destination strip — "never
# give a hand a destination" — but the destination here is a DURATION, not a noun,
# so that strip's closed list of furniture (lap, chair, desk, sides) never saw it.
#
# Measured on the 2026-08-18 SOXL 中文 clip. Three beats, one seed (4242):
#
#   "his arms stay down at his sides, hands still"            -> clean
#   "his hand rises, palm open, a single gesture, then lowers" -> clean
#   "lifts his hand, palm open, HOLDING IT OUT FOR A BEAT"     -> a black
#       articulated magic-arm grip rig grew out of the bare wall beside him
#       from ~5.5s to ~9s, and his hand went up and touched it.
#
# A hand that rises and falls is a gesture. A hand held OUT, waiting, is an
# unanswered question, and LTX answers it: the "presenter performing" prior fills
# the empty half of a seamless-wall frame with the studio gear that co-occurs
# with it in training data. This is the same prior that drew a headset-mic
# capsule on the host's cheek on 2026-08-11 (see ltx_ia2v_probe.NEG) — that time
# it was suppressed by naming mics in NEG, so this time it reached for the
# nearest thing NEG did not name.
#
# Strip the dwell, keep the gesture: "lifts his hand, palm open, holding it out
# for a beat before lowering it" -> "lifts his hand, palm open, before lowering it".
# NEG is widened in the same change, but this is the load-bearing half: NEG can
# only push away what it has thought to name, whereas not asking for an
# outstretched hand removes the summons.
_REACH = re.compile(
    # the verb that extends
    r"\s*(?:,\s*)?\b(?:and\s+|then\s+)?"
    r"(?:hold(?:s|ing)?|keep(?:s|ing)?|extend(?:s|ing)?|reach(?:es|ing)?|"
    r"stretch(?:es|ing)?|offer(?:s|ing)?|present(?:s|ing)?)\s+"
    # what is extended (optional)
    r"(?:it|them|that|(?:his|her|their|the)\s+\w+)?\s*"
    # the DWELL itself. "open" is deliberately absent: "palm open" is a hand
    # SHAPE, and the clip that used it ("his hand rises, palm open, a single
    # gesture, then lowers") came back clean. So is "up" — a hand that goes up
    # and comes down is a gesture, not a question left hanging.
    r"\b(?:out|outward|outwards|there|aloft)\b"
    # an optional aim and an optional SHORT duration. Both are bounded on
    # purpose: an unbounded tail ate "before lowering it as he continues on the
    # leverage and the fear" out of the very sentence this was written for.
    r"(?:\s+(?:toward|towards|to|at)\s+(?:the|his|her|their)?\s*\w+)?"
    r"(?:\s+(?:for|in)\s+(?:a|an|one|two|three|four|five|several|\d+)\s+\w+)?",
    re.I)

# Left-overs from the strip above: a dangling connective, a doubled comma, a
# clause that now begins with "and". Cosmetic for a human, not for a text
# encoder — a prompt that reads as broken grammar is a prompt steering on noise.
_TIDY = [(re.compile(r"\s*,\s*(?=[,.;])"), ""),
         (re.compile(r"\s*\b(?:and|then)\s*(?=[,.;])", re.I), ""),
         (re.compile(r"(?<=[.;])\s*(?:and|then)\s+", re.I), " "),
         (re.compile(r"\s{2,}"), " "),
         (re.compile(r"\s+([,.;])"), r"\1"),
         (re.compile(r",\s*\."), ".")]


def strip_held_gesture(s: str) -> str:
    """Remove a hand held OUT and dwelling, keeping the gesture that surrounds it.

    Public because PRODUCTION (briefing.host_briefing) takes its prompt from
    ltx_official.compose(), which is a deliberate BY-THE-BOOK control that runs
    no clean-up pass of its own — so production applies this itself, next to its
    typographic-punctuation map.
    """
    s = _REACH.sub("", s)
    for pat, rep in _TIDY:
        s = pat.sub(rep, s)
    s = " ".join(s.split()).strip(" ,;")
    return s


def guide_frames(wav: Path, fps: int = 24, min_pause: float = 0.15,
                 min_gap: float = 3.0, quiet_frac: float = 0.08) -> list[int]:
    """Frame indices to re-anchor the keyframe at — only where the presenter is
    actually SILENT.

    Identity drifts because LTXVImgToVideoInplace anchors ONE latent frame and nothing
    re-asserts the reference afterwards; LTXVAddGuide can pin it again at any index,
    and doing so lifted identity 0.547 -> 0.653 with `min` crossing SFace's
    same-person threshold. But a guide writes the KEYFRAME into that frame, and the
    keyframe has a closed-lip smile, so a guide landing on a vowel orders the mouth
    shut mid-word. Measured: 2 of g3's 3 guides and 3 of g5's 5 sat on speech at
    1.0-1.9x median loudness, and the lip-sync damage ranked exactly with that count
    (base +0.093, g3 +0.028, g5 -0.033).

    So place them in the presenter's pauses, and — this is the part that bit on the
    first try — do not SNAP to the nearest multiple of 8. frame_idx must be a
    multiple of 8, which is 0.33 s of granularity; snapping a 90 ms pause to the
    nearest slot moved a guide back onto speech (frame 216 = 9.00 s at 1.49x
    median). A slot only qualifies if it lands INSIDE a long enough silence, so the
    count follows the audio: a dense script with few pauses gets fewer guides
    rather than badly placed ones.
    """
    import numpy as np, soundfile as sf
    x, sr = sf.read(wav)
    if x.ndim > 1:
        x = x.mean(1)
    hop = int(0.01 * sr)
    env = np.array([np.sqrt((x[i:i + hop] ** 2).mean())
                    for i in range(0, len(x) - hop, hop)])
    voiced = env[env > 0]
    if not len(voiced):
        return [0]
    quiet = env < quiet_frac * np.median(voiced)

    runs, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * 0.01 >= min_pause:
                runs.append((i * 0.01, j * 0.01))
            i = j
        else:
            i += 1

    # longest pauses first — they have the most room around a 0.33 s slot
    picked = [0]
    for a, b in sorted(runs, key=lambda r: r[1] - r[0], reverse=True):
        lo, hi = int(np.ceil(a * fps / 8)) * 8, int((b * fps) // 8) * 8
        if hi < lo:                       # no 8-frame slot fits inside this pause
            continue
        f = int(round(((a + b) / 2) * fps / 8)) * 8
        f = min(max(f, lo), hi)
        if all(abs(f - p) >= min_gap * fps for p in picked):
            picked.append(f)
    return sorted(picked)
