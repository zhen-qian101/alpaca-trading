#!/usr/bin/env python3
"""The presenter's voice: Qwen3-TTS narration with subtitles derived, not guessed.

Drop-in for generate.synth_audio's contract — (text, wav_out, srt_out) -> seconds —
because that is the seam the slideshow already has. The difference is WHOSE voice
and where the subtitle timings come from:

  * edge-tts emits word-timed SRT as a by-product. Qwen3-TTS emits nothing, and no
    forced aligner is installed in either env (checked: whisper absent from comfy,
    faster_whisper absent from alpaca-media). Guessing timings from character count
    drifts audibly by the third sentence.
  * So synthesise PER SENTENCE — ltx_story.tts already takes a list of lines and
    returns one wav each — measure every wav, and write the SRT from the real
    durations. Sentence-level cues, no aligner, no guessing.

The inter-sentence gap is not padding: it is the pause the TTS does not always
leave, and those silences are exactly where guide_frames() plants the identity
guides. Per-sentence synthesis manufactures the pauses the rest of the pipeline
wants to find.
"""
from __future__ import annotations

import re
from pathlib import Path

from .ltx_story import tts

GAP_S = 0.22          # breath between sentences; also a guide anchor


def split_sentences(text: str, lang: str = "en") -> list[str]:
    """Sentences as SPOKEN units. zh splits on 。！？ only — commas are too fine
    and would turn one thought into six half-second cues."""
    if lang == "zh":
        parts = re.split(r"(?<=[。！？!?])", text)
    else:
        parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _srt_ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"


def synth_host_audio(text: str, out: Path, srt: Path, lang: str = "en",
                     workdir: Path | None = None,
                     return_cues: bool = False,
                     host_json: Path | None = None):
    """Synthesise `text` in the presenter's voice; write wav + true-timed SRT.

    Returns the total duration in seconds (synth_audio's contract), or
    (duration, cues) with cues = [(start, end, sentence)] when return_cues is
    set — the desk slide's pan choreography needs the per-sentence timeline.

    `host_json` selects the presenter: None = the configured presenter (config
    host.host_json, resolved by voice.py). It is passed straight through to
    voice.py; generate.py always passes it explicitly.
    """
    import numpy as np
    import soundfile as sf

    sentences = split_sentences(text, lang)
    if not sentences:
        raise ValueError("empty narration text")
    work = workdir or out.parent / f".tts_{out.stem}"
    # The work dir lives under the run's tmp, which PERSISTS between runs — a
    # leftover wav from yesterday's five-sentence narration makes tts()'s glob
    # count five for today's four ("expected 4 wavs, got 5", live in the E2E).
    if work.exists():
        import shutil
        shutil.rmtree(work)
    wavs = tts(sentences, work, lang, host_json=host_json)

    pieces, cues, t = [], [], 0.0
    sr = None
    for sent, wav in zip(sentences, wavs):
        x, r = sf.read(wav)
        if x.ndim > 1:
            x = x.mean(1)
        if sr is None:
            sr = r
        elif r != sr:                        # clone output rate is fixed; be loud if not
            raise RuntimeError(f"sample-rate mismatch {r} != {sr} in {wav}")
        dur = len(x) / sr
        cues.append((t, t + dur, sent))
        pieces.append(x)
        pieces.append(np.zeros(int(GAP_S * sr), dtype=x.dtype))
        t += dur + GAP_S
    total = t - GAP_S                        # no trailing breath
    audio = np.concatenate(pieces[:-1])

    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, audio, sr)
    lines = []
    for i, (a, b, sent) in enumerate(cues, 1):
        lines += [str(i), f"{_srt_ts(a)} --> {_srt_ts(b)}", sent, ""]
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text("\n".join(lines), encoding="utf-8")
    return (total, cues) if return_cues else total

