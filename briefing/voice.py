#!/usr/bin/env python3
"""The presenter's voice — Qwen3-TTS, replacing edge-tts for the host track.

The Base model CLONES one frozen reference clip (the presenter file's
voice.clone_ref, plus clone_ref_zh for Mandarin) for every line. Cloning one
frozen reference is what makes the presenter one person in two languages; a
model that re-rolled the timbre per call would sound like a different person in
every render.

This module runs under its OWN venv (/mnt/sandisk/qwen_tts/venv) because qwen-tts
pulls a newer torch than the production ComfyUI env, and that env's torch is
shared with the H3 worktree. It is invoked as a SUBPROCESS from the render, the
same way edge-tts already is — never imported into the pipeline, so it uses the
standard library only (no briefing package imports).

    # per render (model loads once, all lines synthesised in one go):
    voice.py --lines lines.json --lang en --outdir /tmp/vo

`lines.json` is either a list of strings or {"lines": [...]}; output is
NNN.wav per line, in order, plus an index.json of durations.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_JSON = ROOT / "briefing" / "config.json"
VENV_PY = Path("/mnt/sandisk/qwen_tts/venv/bin/python")


def configured_host_json() -> Path:
    """The presenter file production renders: config host.host_json, with
    BRIEFING_HOST_JSON overriding one run — the same resolution generate.py,
    run_stylist.sh and web/server.py use. There is no default presenter."""
    name = os.environ.get("BRIEFING_HOST_JSON")
    if not name:
        try:
            name = json.loads(CONFIG_JSON.read_text()).get("host", {}).get("host_json")
        except (OSError, ValueError) as exc:
            sys.exit(f"cannot read {CONFIG_JSON}: {exc}")
    if not name:
        sys.exit("briefing/config.json host.host_json is not set — no presenter")
    return ROOT / "briefing" / name


def load_host(host_json: Path | None = None) -> dict:
    """The presenter whose voice this is: `host_json`, or the configured one.

    Presenters differ only in the JSON (what `clone_ref` points at), not in how
    any of this works, so the module takes a path rather than growing a copy.
    """
    d = json.loads((host_json or configured_host_json()).read_text())
    d.pop("_comment", None)
    d.get("voice", {}).pop("_comment", None)
    return d


def _model(repo: str):
    import torch
    from qwen_tts import Qwen3TTSModel
    return Qwen3TTSModel.from_pretrained(
        repo, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")


def _write(wav, sr: int, dest: Path) -> float:
    import soundfile as sf
    dest.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dest), wav, sr)
    return len(wav) / float(sr)


# --- false-start QC -----------------------------------------------------------
#
# The Base model generates with do_sample=True / top_k=50 / temperature=0.9 and no
# seed, so it occasionally emits an autoregressive FALSE START: a syllable begun,
# abandoned, and restarted. It lands as a short voiced burst stranded in silence,
# most often before the line but the rule below is position-agnostic. Measured on
# 2026-08-12: TSLA_zh line 3 opened with two of them and did not reach real speech
# until 1.88s. Re-synthesising the identical string came out clean 16/16, which is
# what makes a retry the fix — nothing about the text is wrong.
#
# THE HARD PART IS NOT FINDING BURSTS, IT IS IGNORING A BREATH. Both look like a
# short sound alone in silence. What separates them is voicing and level, measured
# across the 70 lines of one render:
#
#                       rms vs speech   voicing conf   zcr
#   false starts (4)      50-109%         0.77-0.90     311-500
#   breaths      (3)      4.1-4.6%        0.10-0.14    2512-2876
#
# An order of magnitude apart on both axes, so the thresholds sit in open space.
# Retrying a breath would burn a GPU re-roll to remove something natural.
_HOP = 0.01
_SIL_FRAC = 0.02        # silence: below 2% of the clip's loudest 10ms
_MERGE_GAP = 0.12       # shorter dips are inside one phrase, not between sounds
_MAX_BURST = 0.20       # an artifact is short; a real word syllable runs longer
_MIN_ISOLATION = 0.25   # ...and stranded, not part of the speech around it
_MIN_LEVEL = 0.20       # vs median speech level — excludes breaths (max 4.6%)
_MIN_VOICING = 0.50     # autocorrelation peak — excludes breaths (max 0.14)


def _voicing(seg, sr: int) -> float:
    """Autocorrelation peak in the 70-350Hz band: how periodic (voiced) this is."""
    import numpy as np
    s = seg - seg.mean()
    lo, hi = int(sr / 350), int(sr / 70)
    if len(s) < 64:
        return 0.0
    ac = np.correlate(s, s, "full")[len(s) - 1:]
    if len(ac) <= hi or ac[0] <= 0:
        return 0.0
    return float(ac[lo:hi].max() / ac[0])


def artifacts(wav, sr: int) -> list[dict]:
    """Short voiced bursts stranded in silence — lead, interior or trailing.

    Returns one dict per suspect burst; an empty list means the clip is clean.
    """
    import numpy as np
    x = np.asarray(wav, dtype=float)
    if x.ndim > 1:
        x = x.mean(1)
    n = max(1, int(sr * _HOP))
    frames = len(x) // n
    if frames < 3:
        return []
    e = np.array([np.sqrt((x[i * n:(i + 1) * n] ** 2).mean()) for i in range(frames)])
    if e.max() <= 0:
        return []

    on = e >= e.max() * _SIL_FRAC
    runs, s = [], None
    for i, v in enumerate(on):
        if v and s is None:
            s = i
        if not v and s is not None:
            runs.append([s, i]); s = None
    if s is not None:
        runs.append([s, len(on)])
    segs: list[list[int]] = []
    for a, b in runs:
        if segs and (a - segs[-1][1]) * _HOP < _MERGE_GAP:
            segs[-1][1] = b
        else:
            segs.append([a, b])
    if len(segs) < 2:                       # one continuous sound: nothing stranded
        return []

    speech = np.median(e[e > e.max() * 0.15])
    total = frames * _HOP
    out = []
    for i, (a, b) in enumerate(segs):
        dur = (b - a) * _HOP
        if dur >= _MAX_BURST:
            continue
        before = a * _HOP if i == 0 else (a - segs[i - 1][1]) * _HOP
        after = (total - b * _HOP) if i == len(segs) - 1 else (segs[i + 1][0] - b) * _HOP
        # the first and last sounds only have one inner side to be stranded from
        if i == 0:
            stranded = after >= _MIN_ISOLATION
        elif i == len(segs) - 1:
            stranded = before >= _MIN_ISOLATION
        else:
            stranded = before >= _MIN_ISOLATION and after >= _MIN_ISOLATION
        if not stranded:
            continue
        seg = x[a * n:b * n]
        level = float(np.sqrt((seg ** 2).mean()) / speech) if speech > 0 else 0.0
        voiced = _voicing(seg, sr)
        if level < _MIN_LEVEL or voiced < _MIN_VOICING:
            continue                        # a breath, not a false start
        out.append({"where": "lead" if i == 0 else ("tail" if i == len(segs) - 1 else "mid"),
                    "start": round(a * _HOP, 2), "end": round(b * _HOP, 2),
                    "level": round(level, 2), "voicing": round(voiced, 2)})
    return out


def check_reference(ref: Path) -> list[tuple[float, float]]:
    """Interior stretches of EXACT digital silence in a cloning reference.

    Not a style note — a correctness check. Base clones in ICL mode: it is shown
    (ref_audio, ref_text) and continues the pattern. A reference spliced together
    from several generations carries mathematically-zero silence its transcript
    does not account for, and the clone reproduces that structure as a blip, a
    gap, then the line.

    MEASURED 2026-08-12. A reference spliced from several generations carried
    0.45 s of exact-zero silence mid-clip. Same probe line, same seed: 8/32 clips
    blipped with it, 0/32 after re-cutting it from a join-free span. A reference
    with no interior zeros blipped 0/61 in the same render. Ignoring the
    transcript entirely (raw whisper vs hand-corrected) changed nothing, which
    is what rules the transcript out.
    """
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(str(ref))
    if x.ndim > 1:
        x = x.mean(1)
    zero = np.abs(x) < 1e-7
    runs, s = [], None
    for i, v in enumerate(zero):
        if v and s is None:
            s = i
        if not v and s is not None:
            runs.append((s, i)); s = None
    if s is not None:
        runs.append((s, len(zero)))
    # a trailing pad is deliberate (anti phoneme-bleed) and measured HELPFUL:
    # 0/32 with it against 2/16 without. Only interior silence is the defect.
    return [(round(a / sr, 2), round(b / sr, 2)) for a, b in runs
            if (b - a) / sr >= 0.05 and b < len(zero) - int(0.01 * sr)]


# House level for a cloning reference, as speech-band RMS in dBFS. Every
# reference in the show sits between -18.3 and -19.8; the band below is that plus
# a couple of dB of slack either way.
#
# WHY THIS IS CHECKED AT ALL. A clone reproduces its reference's LEVEL as
# faithfully as its timbre — measured 2026-08-12, +7 dB on the reference moved
# the cloned lines +6.0 dB, near enough 1:1. And nothing downstream corrects it:
# the host render path only acrossfades, so whatever level the TTS emits is the
# level that ships. One reference sat ~5 dB below the rest of the house
# references, and the demo made from it went out ~7 LU quiet before anyone
# noticed by ear. A reference that is quiet is not audibly wrong on its own —
# it is eight seconds long — so it needs a number.
_LEVEL_BAND = (-22.0, -16.0)


def reference_level(ref: Path) -> float:
    """Speech-band RMS of a reference in dBFS.

    Not true LUFS: BS.1770 K-weighting would need ffmpeg, which is not on PATH in
    the qwen_tts venv this module runs under. Over speech the two track closely
    enough to catch the failure this exists to catch — the four house references
    span 1.5 dB on this measure and the bad one sat 5 dB below all of them.
    """
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(str(ref))
    if x.ndim > 1:
        x = x.mean(1)
    n = max(1, int(0.02 * sr))
    e = np.array([np.sqrt((x[i:i + n] ** 2).mean()) for i in range(0, len(x) - n, n)])
    if not len(e) or e.max() <= 0:
        return -np.inf
    spoken = e[e > e.max() * 0.15]           # ignore the pauses and the tail pad
    return float(20 * np.log10(np.sqrt((spoken ** 2).mean()) + 1e-12))


def speak(host: dict, lines: list[str], lang: str, outdir: Path,
          attempts: int = 3) -> None:
    """Clone the reference for every line. One model load for the whole batch.

    Lines that come back with a false start are re-rolled, up to `attempts` times,
    and only those lines — the model is already loaded, so a retry costs one short
    generation. We never seed: each call has to advance the RNG or the retry would
    reproduce the artifact it is trying to escape.
    """
    v = host["voice"]
    ref = ROOT / (v.get("clone_ref_zh") if (lang == "zh" and v.get("clone_ref_zh")) else v["clone_ref"])
    if not ref.exists():
        sys.exit(f"reference clip missing: {ref}")
    spliced = check_reference(ref)
    if spliced:
        print(f"  [voice] WARNING: {ref.name} contains interior digital silence "
              f"{spliced} — clones will emit blips at line starts. Re-cut it from a "
              f"join-free span of its source.", file=sys.stderr)
    lvl = reference_level(ref)
    if not _LEVEL_BAND[0] <= lvl <= _LEVEL_BAND[1]:
        print(f"  [voice] WARNING: {ref.name} sits at {lvl:.1f} dBFS, outside the "
              f"house band {_LEVEL_BAND[0]:.0f}..{_LEVEL_BAND[1]:.0f}. Level is "
              f"inherited ~1:1 by every cloned line and nothing downstream "
              f"normalises it, so the whole render will ship "
              f"{'quiet' if lvl < _LEVEL_BAND[0] else 'hot'}. Apply linear gain to "
              f"the reference — not a compressor, which would reshape the dynamics "
              f"the model copies.", file=sys.stderr)
    ref_text = (v.get("clone_ref_text_zh") if lang == "zh" and v.get("clone_ref_text_zh")
                else v.get("clone_ref_text")) or None
    language = "Chinese" if lang == "zh" else "English"

    m = _model(v["clone_model"])
    outdir.mkdir(parents=True, exist_ok=True)
    index = []

    def _clone(texts: list[str]):
        # Batched in one call: the model accepts a list, and per-line calls would
        # pay the reference encode over and over.
        return m.generate_voice_clone(
            text=texts, language=[language] * len(texts),
            ref_audio=[str(ref)] * len(texts),
            ref_text=[ref_text] * len(texts) if ref_text else None)

    wavs, sr = _clone(lines)
    wavs = list(wavs)
    flaws = [artifacts(w, sr) for w in wavs]

    for attempt in range(2, attempts + 1):
        bad = [i for i, f in enumerate(flaws) if f]
        if not bad:
            break
        for i in bad:
            what = ", ".join(f"{f['where']} {f['start']}-{f['end']}s" for f in flaws[i])
            print(f"  [voice] {i:03d} false start ({what}) — retry {attempt}/{attempts}")
        retry, _ = _clone([lines[i] for i in bad])
        for i, w in zip(bad, retry):
            f = artifacts(w, sr)
            # Keep the cleaner take. Equal counts keep the retry: it is no worse,
            # and a later take cannot be a repeat of the one we already rejected.
            if len(f) <= len(flaws[i]):
                wavs[i], flaws[i] = w, f

    still = [i for i, f in enumerate(flaws) if f]
    if still:
        print(f"  [voice] WARNING: {len(still)} line(s) still show a false start "
              f"after {attempts} attempts: {still} — kept the cleanest take")

    for i, w in enumerate(wavs):
        dest = outdir / f"{i:03d}.wav"
        secs = _write(w, sr, dest)
        rec = {"i": i, "file": dest.name, "seconds": round(secs, 3), "text": lines[i]}
        if flaws[i]:
            rec["false_starts"] = flaws[i]      # kept so a bad render is diagnosable
        index.append(rec)
        mark = "  !" if flaws[i] else ""
        print(f"  [voice] {i:03d} {secs:5.2f}s  {lines[i][:60]}{mark}")
    (outdir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
    print(f"  [voice] {len(index)} line(s) -> {outdir}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthesise the host's voice with Qwen3-TTS")
    ap.add_argument("--lines", help="JSON file: [str, ...] or {'lines': [...]}")
    ap.add_argument("--lang", default="en", choices=["en", "zh"])
    ap.add_argument("--outdir", default="/tmp/host_vo")
    ap.add_argument("--host-json", help="presenter to speak as (default: the one "
                                        "briefing/config.json host.host_json names)")
    args = ap.parse_args()

    if sys.prefix != str(VENV_PY.parent.parent):
        print(f"  [voice] note: expected to run under {VENV_PY}", file=sys.stderr)

    host = load_host(Path(args.host_json) if args.host_json else None)
    lines = None
    if args.lines:
        raw = json.loads(Path(args.lines).read_text())
        lines = raw["lines"] if isinstance(raw, dict) else raw
    if not lines:
        ap.error("pass --lines")
    speak(host, [str(x) for x in lines], args.lang, Path(args.outdir))


if __name__ == "__main__":
    main()
