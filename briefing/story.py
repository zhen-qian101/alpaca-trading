"""Story mode: the slideshow and the cartoon story video.

Two renderers live here:

  * render_scenes -> the typographic BULLET slideshow (`--renderer hyperframes`,
    and the fallback when the Wan pipeline cannot render): one slide per symbol,
    narrated from the Claude-written briefing, with no image generation.
  * render_scenes_wan -> the weekend recap's cartoon story video: each symbol's
    FIXED character (briefing/characters.json) acts out a director-written
    screenplay, the shots rendered by MiniMax H3.

Shared helpers: the image pipeline loader and sprite cutout, and build_narration
(per-beat TTS concatenated into one voiceover + combined subtitles).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

# reuse the primitives already defined in generate.py's module
from . import desk as D
from . import director
from . import generate as G


# --------------------------------------------------------------------------
# sprites: SDXL flat character + AI background removal -> transparent PNG
# --------------------------------------------------------------------------
# appended to character/prop prompts to keep them clean and cut-out friendly.
_SPRITE_SUFFIX = (", isolated on plain solid white background, no scenery, no props, "
                  "full body, centered, simple, no text, no words")
_PROP_SUFFIX = (", single object, isolated on plain solid white background, centered, "
                "simple, no text, no words")


def _is_sd3(model: str) -> bool:
    """True for Stable Diffusion 3.x checkpoints (need bf16 + no fp16 variant)."""
    m = (model or "").lower()
    return "stable-diffusion-3" in m or "sd3" in m or "sd-3" in m


class WanCompositingError(RuntimeError):
    """Every Wan raw rendered, then the cheap assembly on top of them failed.

    generate.py's wan→hyperframes fallback exists for 'Wan could not render'.
    This class exists so that fallback can tell the difference: on 2026-08-02 a
    single cue-less Chinese .srt made a beat's ffmpeg composite exit 183 AFTER
    all 24 shots (~2h of GPU) were safely on disk, the blanket fallback threw
    the lot away, and a slideshow was published in the recap's place labelled
    renderer=wan. A compositing failure must surface loudly — the raws in
    .hf_work/tmp survive a crashed run and a targeted re-composite finishes the
    job in minutes — never silently downgrade the product."""


def load_pipe(cfg: dict, model: str | None = None):
    """Load the text-to-image pipeline (SDXL-Turbo default, or SD 3.5 Large).

    Model-aware: SD 3.x loads in bf16 with no ``fp16`` variant (its weights ship
    as the default safetensors); SDXL keeps the fp16 variant fast path. Returns
    None if torch/diffusers is unavailable or the (possibly gated) model can't
    be fetched — callers fall back to gradients/no backgrounds.

    When ``image_backend == "comfyui"`` (and no explicit model override), the image
    generation is routed to a headless ComfyUI server via a diffusers-shaped shim
    (briefing/comfy.ComfyPipe) — same call signature, model resident in VRAM, no
    CUDA context in this process. Falls through to in-process diffusers if the
    ComfyUI server/model isn't available."""
    import os
    os.environ.setdefault("HF_HOME", cfg.get("hf_cache", "/mnt/sandisk/hf_cache"))
    if model is None and cfg.get("image_backend", "diffusers") == "comfyui":
        try:
            from . import comfy
            cp = comfy.ComfyPipe.create(cfg)
            if cp is not None:
                return cp
        except Exception as exc:  # noqa: BLE001
            print(f"  [comfy] backend error ({type(exc).__name__}: {exc}); using diffusers")
    try:
        import torch
        from diffusers import AutoPipelineForText2Image
    except Exception as exc:  # noqa: BLE001
        print(f"  [images] torch/diffusers unavailable ({exc})")
        return None
    model = model or cfg.get("image_model", "stabilityai/sdxl-turbo")
    sd3 = _is_sd3(model)
    kw = dict(torch_dtype=torch.bfloat16 if sd3 else torch.float16)
    try:
        if sd3:                                    # SD 3.5 has no fp16 variant
            pipe = AutoPipelineForText2Image.from_pretrained(model, **kw)
        else:
            pipe = AutoPipelineForText2Image.from_pretrained(model, variant="fp16", **kw)
    except Exception:
        try:
            pipe = AutoPipelineForText2Image.from_pretrained(model, **kw)
        except Exception as exc:  # noqa: BLE001 — gated/missing model
            print(f"  [images] could not load {model} ({type(exc).__name__}: {str(exc)[:120]})")
            return None
    try:
        pipe = pipe.to("cuda")
    except Exception:                              # too big to fit resident -> stage on/off
        pipe.enable_model_cpu_offload()
    pipe.set_progress_bar_config(disable=True)
    return pipe


def load_seg(cfg: dict):
    """Load the rembg segmentation session (or None)."""
    import os
    os.environ.setdefault("U2NET_HOME", cfg.get("hf_cache", "/mnt/sandisk/hf_cache") + "/rembg")
    try:
        from rembg import new_session
        return new_session(cfg.get("story", {}).get("cutout_model", "isnet-general-use"))
    except Exception as exc:  # noqa: BLE001
        print(f"  [images] rembg unavailable ({exc})")
        return None


def _cut_out(raw: Path, cut: Path, session) -> None:
    """Segment the subject (rembg isnet-general — works on animals AND decorated
    backgrounds) and harden the soft alpha so flat fills stay fully opaque."""
    import numpy as np
    from PIL import Image
    from rembg import remove
    res = remove(Image.open(raw).convert("RGBA"), session=session)
    arr = np.array(res.convert("RGBA")).astype(float)
    a = arr[:, :, 3]
    arr[:, :, 3] = np.clip((a - 60) * (255.0 / 140.0), 0, 255)   # 60->0, 200->255
    Image.fromarray(arr.astype("uint8"), "RGBA").save(cut)


def make_sprite(pipe, seg, prompt: str, raw: Path, cut: Path, cfg: dict,
                suffix: str = _SPRITE_SUFFIX, px: int = 768) -> Path | None:
    """Generate one SDXL image, cut its background out to transparent, save. Returns
    the transparent PNG path (or the raw path if cutout fails, or None)."""
    steps = int(cfg.get("image_steps", 4))
    guidance = float(cfg.get("image_guidance", 0.0))
    try:
        img = pipe(prompt=prompt + suffix, num_inference_steps=steps,
                   guidance_scale=guidance, height=px, width=px).images[0]
        img.save(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"  [images] gen failed ({exc})")
        return None
    if seg is None:
        return raw
    try:
        _cut_out(raw, cut, seg)
        return cut
    except Exception as exc:  # noqa: BLE001
        print(f"  [images] cutout failed ({exc}); using opaque image")
        return raw


# --------------------------------------------------------------------------
# narration: one line per beat -> concatenated voiceover + combined subtitles
# --------------------------------------------------------------------------
def _srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _shift_srt(srt_path: Path, offset: float, start_idx: int) -> tuple[str, int]:
    """Return (srt_block_text, next_index) with all cue times shifted by offset."""
    if not srt_path.exists():
        return "", start_idx
    text = srt_path.read_text()
    cues = re.split(r"\n\s*\n", text.strip())
    out, idx = [], start_idx
    for cue in cues:
        lines = cue.splitlines()
        tl = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if tl is None:
            continue
        a, b = [t.strip() for t in lines[tl].split("-->")]
        def to_s(t):
            t = t.replace(",", ".")
            hh, mm, ss = t.split(":")
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
        na, nb = to_s(a) + offset, to_s(b) + offset
        body = "\n".join(lines[tl + 1:])
        out.append(f"{idx}\n{_srt_time(na)} --> {_srt_time(nb)}\n{body}")
        idx += 1
    return "\n\n".join(out) + "\n\n", idx


def build_narration(storyd: dict, tmp: Path, cfg: dict,
                    voice: str | None = None, rate: str | None = None) -> tuple[Path, Path, list[dict]]:
    """Synthesize each beat, concat into one voiceover, build one combined SRT.
    Returns (narration_mp3, combined_srt, beats_timing)."""
    gap = float(cfg.get("story", {}).get("beat_gap_s", 0.35))
    sil = tmp / "sil.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=24000:cl=mono", "-t", str(gap), str(sil)],
                   check=True, capture_output=True, text=True)

    parts, srt_all, idx, cursor, timing = [], "", 1, 0.0, []
    for b in storyd["beats"]:
        n = b["n"]
        mp3 = tmp / f"beat_{n}.mp3"
        srt = tmp / f"beat_{n}.srt"
        G.synth_audio(b["narration"], mp3, srt, cfg, voice=voice, rate=rate)
        dur = G._duration(mp3)
        block, idx = _shift_srt(srt, cursor, idx)
        srt_all += block
        timing.append({"n": n, "start": round(cursor, 3), "dur": round(dur, 3),
                       "narration": b["narration"], "action": b.get("action"),
                       "actor": b.get("actor"), "characters": b.get("characters", [])})
        parts.append(mp3)
        cursor += dur + gap

    # concat mp3s interleaved with silence
    listf = tmp / "narr_concat.txt"
    seq = []
    for i, p in enumerate(parts):
        seq.append(p)
        if i < len(parts) - 1:
            seq.append(sil)
    listf.write_text("".join(f"file '{p.as_posix()}'\n" for p in seq))
    narr = tmp / "story_narr.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listf), "-c", "copy", str(narr)],
                   check=True, capture_output=True, text=True)
    csrt = tmp / "story.srt"
    csrt.write_text(srt_all)
    return narr, csrt, timing


# ==========================================================================
# THE FIXED CAST: each symbol's character sprite (briefing/characters.json +
# briefing/assets/characters/) and the cartoon-set prompt suffix, shared by the
# Wan story pipeline below.
# ==========================================================================
CHAR_DIR = G.ROOT / "briefing" / "assets" / "characters"
REGISTRY = G.ROOT / "briefing" / "characters.json"
_BG_SUFFIX = (", flat minimalist cartoon illustration, wide establishing shot, soft muted "
              "colours, gentle, lots of negative space, no text, no words, no letters, no people")


def load_registry() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


# ==========================================================================
# BULLETS STYLE (slideshow default): typographic slides — no characters, no
# props, no image generation, no Gemma. Each symbol gets one slide: big ticker
# + bias chip, icon-led CATALYST cards and a KEY-NUMBERS/TECHNICALS stat grid,
# narrated with the Claude-written briefing story. Characters/props remain in
# the flagship Wan recap video only. Fully deterministic and GPU-free, so the
# slideshow renders in minutes and can never fight the director for VRAM.
# ==========================================================================
_ICON_RULES = [
    (("earnings", "revenue", "profit", "eps", "guidance", "quarter", "margin"), "💰"),
    (("ai ", " ai", "supercycle", "datacenter", "data center", "gpu", "chip", "semiconductor",
      "silicon", "tsmc"), "🤖"),
    (("cloud", "software", "subscription", "azure", "aws"), "☁️"),
    (("oil", "crude", "opec", "energy", "houthi", "tanker", "brent"), "🛢️"),
    (("fed", "rate", "cpi", "inflation", "yield", "treasury", "fomc", "powell"), "🏦"),
    (("china", "tariff", "export", "trade war", "geopolit"), "🌏"),
    (("deal", "acquisition", "merger", "partnership", "contract", "order book"), "🤝"),
    (("lawsuit", "regulat", "antitrust", "probe", "doj", "ftc"), "⚖️"),
    (("upgrade", "price target", "analyst", "initiat"), "🎯"),
    (("breakout", "momentum", "rally", "all-time high", "52-week", "donchian"), "🚀"),
    (("support", "resistance", "ema", "sma", "rsi", "macd", "vwap", "trend", "volume",
      "moving average", "bollinger"), "📊"),
    (("risk", "warning", "selloff", "sell-off", "drop", "miss", "downgrade", "halt"), "⚠️"),
]


def _icon_for(*texts: str, default: str = "📌") -> str:
    blob = " ".join(t or "" for t in texts).lower()
    for keys, icon in _ICON_RULES:
        if any(k in blob for k in keys):
            return icon
    return default


_SENT_RE = re.compile(r"(?<=[.!?])\s+")

# How much of a briefing has to survive translation for the 中文 cut to be worth
# shipping. A deck missing one symbol slide still reads as a briefing; a deck
# missing most of them reads as broken, and English-only is the better failure.
ZH_MIN_KEEP = 0.6


def _zh_enough(kept: int, total: int) -> bool:
    return kept >= 2 and total > 0 and kept >= ZH_MIN_KEEP * total


def _bullet_narration(brief: dict | None, note: str, target_s: float) -> list[str]:
    """Narration beats for a bullet slide: the Claude-written briefing story
    (fallback: the research note), whole sentences up to ~target seconds."""
    src = (brief or {}).get("story") or note or ""
    src = re.sub(r"\s+", " ", G._strip_preamble(src)).strip()
    budget = max(180.0, target_s * 16.0)          # ~16 chars/sec at rate +8%
    sents, kept, used = _SENT_RE.split(src), [], 0
    for s in sents:
        if kept and used + len(s) > budget * 1.25:
            break
        kept.append(s)
        used += len(s)
    # group into ~2-sentence beats so the SRT cues stay readable
    beats, cur = [], ""
    for s in kept:
        cur = f"{cur} {s}".strip()
        if len(cur) > 190:
            beats.append(cur)
            cur = ""
    if cur:
        beats.append(cur)
    return beats or ["No briefing content available."]


def _bullet_cards(brief: dict | None, note: str) -> tuple[list[dict], list[dict]]:
    """(catalyst cards, indicator cards) from the briefing; when a symbol has no
    briefing entry, fall back to the first note sentences as plain bullets."""
    cats, inds = [], []
    for c in (brief or {}).get("catalysts") or []:
        lab = (c.get("label") or "").strip()
        det = (c.get("detail") or "").strip()
        if lab or det:
            cats.append({"icon": _icon_for(lab, det, default="📰"),
                         "label": lab or det[:40], "detail": det if lab else ""})
    for i in (brief or {}).get("indicators") or []:
        lab = (i.get("label") or "").strip()
        val = (i.get("value") or "").strip()
        if lab or val:
            inds.append({"icon": _icon_for(lab, val, default="📊"),
                         "label": lab or "Indicator", "value": val or "—"})
    if not cats and note:
        clean = re.sub(r"\s+", " ", G._strip_preamble(note)).strip()
        for s in _SENT_RE.split(clean)[:3]:
            if len(s) > 15:
                cats.append({"icon": _icon_for(s), "label": s[:90], "detail": ""})
    return cats[:4], inds[:6]


_BIAS_CHIP = {"long": ("LONG", "#2ecc71"), "short": ("SHORT", "#e5484d"),
              "flat": ("FLAT", "#8b93a7")}


def build_bullet_slide_html(symbol: str, bias: str | None, cats: list[dict],
                            inds: list[dict], duration: float, cfg: dict,
                            date: str) -> str:
    w, h = cfg.get("width", 1920), cfg.get("height", 1080)
    accent = "#f2a93b"
    is_ov = symbol == "OVERVIEW"
    title = "MARKET" if is_ov else symbol
    chip_txt, chip_col = _BIAS_CHIP.get((bias or "").lower(), (None, None))

    clip = f'class="clip" data-start="0" data-duration="{duration:.2f}"'
    chip = ""
    if is_ov:
        chip = (f'<span style="font-size:26px;font-weight:800;letter-spacing:.14em;color:#0d1220;'
                f'background:{accent};padding:8px 22px;border-radius:999px">OVERVIEW</span>')
    elif chip_txt:
        chip = (f'<span style="font-size:26px;font-weight:800;letter-spacing:.14em;color:#0d1220;'
                f'background:{chip_col};padding:8px 22px;border-radius:999px">{chip_txt}</span>')

    cat_cards = ""
    for i, c in enumerate(cats):
        det = ("" if not c["detail"] else
               f'<div style="font-size:23px;color:#b9c2d4;line-height:1.3;margin-top:4px">'
               f'{G._esc(c["detail"][:110])}</div>')
        cat_cards += (
            f'<div id="cat{i}" style="display:flex;gap:20px;align-items:flex-start;'
            f'margin-bottom:22px;padding:20px 24px;border-radius:16px;'
            f'background:rgba(255,255,255,.045);border-left:5px solid {accent};'
            f'box-shadow:0 14px 30px -18px #000">'
            f'<span style="font-size:42px;line-height:1">{c["icon"]}</span><div>'
            f'<div style="font-size:31px;font-weight:800;color:#f4f6fb;line-height:1.15">'
            f'{G._esc(c["label"][:90])}</div>{det}</div></div>')

    ind_cards = ""
    for j, d in enumerate(inds):
        v = d["value"]
        vcol = "#2ecc71" if v.startswith("+") else "#e5484d" if re.match(r"-\d", v) else "#fff"
        vfs = 38 if len(v) <= 14 else 30 if len(v) <= 24 else 24
        ind_cards += (
            f'<div id="ind{j}" style="width:283px;padding:18px 20px;border-radius:16px;'
            f'background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.09)">'
            f'<div style="font-size:15px;letter-spacing:.18em;color:#93a0b4;'
            f'text-transform:uppercase;font-weight:700">{d["icon"]} {G._esc(d["label"][:24])}</div>'
            f'<div style="font-size:{vfs}px;font-weight:800;color:{vcol};margin-top:6px;'
            f'line-height:1.08">{G._esc(v[:40])}</div></div>')

    cat_anim = "".join(
        f'tl.fromTo("#cat{i}",{{opacity:0,x:-70}},{{opacity:1,x:0,duration:0.7,'
        f'ease:"power3.out"}},{0.9 + i * 0.85:.2f});' for i in range(len(cats)))
    ind_anim = "".join(
        f'tl.fromTo("#ind{j}",{{opacity:0,y:36,scale:0.88}},{{opacity:1,y:0,scale:1,'
        f'duration:0.6,ease:"back.out(1.6)"}},{1.3 + j * 0.45:.2f});' for j in range(len(inds)))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width={w}, height={h}"/>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{w}px;height:{h}px;overflow:hidden;background:#0d1220;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}}</style>
</head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{duration:.2f}"
     data-width="{w}" data-height="{h}">
  <div {clip} data-track-index="0" style="position:absolute;inset:0;
       background:linear-gradient(135deg,#0d1220,#131a2c 55%,#0e1626)"></div>
  <div id="blob1" {clip} data-track-index="0" style="position:absolute;left:-260px;top:-220px;
       width:900px;height:900px;border-radius:50%;filter:blur(70px);opacity:.5;
       background:radial-gradient(circle,{accent}26,transparent 65%)"></div>
  <div id="blob2" {clip} data-track-index="0" style="position:absolute;right:-300px;bottom:-330px;
       width:1000px;height:1000px;border-radius:50%;filter:blur(80px);opacity:.45;
       background:radial-gradient(circle,#3b6df226,transparent 65%)"></div>
  <div {clip} data-track-index="1" style="position:absolute;top:64px;left:130px;font-size:21px;
       letter-spacing:.4em;color:#e7eaf0aa;text-transform:uppercase;font-weight:700"
       id="eyebrow">Daily Research · {date}</div>
  <div {clip} data-track-index="1" id="head" style="position:absolute;top:92px;left:126px;
       display:flex;align-items:center;gap:34px">
    <span style="font-size:108px;font-weight:800;color:#fff;letter-spacing:1px;
          text-shadow:0 4px 24px #000a;line-height:1">{G._esc(title)}</span>{chip}</div>
  <div {clip} data-track-index="1" id="rule" style="position:absolute;top:238px;left:132px;
       width:300px;height:6px;border-radius:3px;background:{accent}"></div>
  <div {clip} data-track-index="2" id="catcol" style="position:absolute;top:300px;left:130px;width:1010px">
    <div style="font-size:18px;letter-spacing:.3em;color:#93a0b4;text-transform:uppercase;
         font-weight:700;margin-bottom:22px">📰 Why it matters</div>{cat_cards}</div>
  <div {clip} data-track-index="2" id="indcol" style="position:absolute;top:300px;left:1210px;width:590px">
    <div style="font-size:18px;letter-spacing:.3em;color:#93a0b4;text-transform:uppercase;
         font-weight:700;margin-bottom:22px">📊 Key numbers &amp; technicals</div>
    <div style="display:flex;flex-wrap:wrap;gap:24px">{ind_cards}</div></div>
  <audio src="assets/{G._safe(symbol)}_narr.mp3" data-start="0"></audio>
</div>
<script>
window.__timelines=window.__timelines||{{}};
const tl=gsap.timeline({{paused:true}});
tl.fromTo("#blob1",{{x:0,y:0}},{{x:120,y:70,duration:{duration:.2f},ease:"sine.inOut"}},0);
tl.fromTo("#blob2",{{x:0,y:0}},{{x:-140,y:-60,duration:{duration:.2f},ease:"sine.inOut"}},0);
tl.fromTo("#eyebrow",{{opacity:0,y:-18}},{{opacity:1,y:0,duration:0.6,ease:"power2.out"}},0);
tl.fromTo("#head",{{opacity:0,x:-70}},{{opacity:1,x:0,duration:0.8,ease:"power3.out"}},0.1);
tl.fromTo("#rule",{{width:0}},{{width:300,duration:0.7,ease:"power2.inOut"}},0.35);
tl.fromTo("#catcol",{{opacity:0}},{{opacity:1,duration:0.4}},0.7);
tl.fromTo("#indcol",{{opacity:0}},{{opacity:1,duration:0.4}},1.0);
{cat_anim}
{ind_anim}
window.__timelines["main"]=tl;
</script></body></html>"""


def render_bullet_slide(symbol: str, bias: str | None, brief: dict | None, note: str,
                        cfg: dict, date: str, dest: Path, lines: list[str] | None = None,
                        voice: str | None = None, rate: str | None = None) -> dict | None:
    """One typographic slide: narration synth + hyperframes render (no images).
    `lines`/`voice`/`rate` override the narration — used by the Mandarin variant,
    which re-times the same slide to the zh voiceover (the slide text stays EN)."""
    work = G.WORK
    assets = work / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for f in assets.glob("*"):
        f.unlink()
    tmp = work / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    tgt = cfg.get("director", {}).get("target_seconds", {})
    target = float(tgt.get("overview" if symbol == "OVERVIEW" else "symbol", 26))
    if lines is None:
        lines = _bullet_narration(brief, note, target)
    adapted = [{"n": i + 1, "narration": ln} for i, ln in enumerate(lines)]
    narr, csrt, _timing = build_narration({"beats": adapted}, tmp, cfg, voice=voice, rate=rate)
    shutil.copy(narr, assets / f"{G._safe(symbol)}_narr.mp3")
    duration = G._duration(narr) + 0.3

    cats, inds = _bullet_cards(brief, note)
    html = build_bullet_slide_html(symbol, bias, cats, inds, duration, cfg, date)
    (work / "index.html").write_text(html)

    renders = work / "renders"
    if renders.exists():
        shutil.rmtree(renders)
    subprocess.run(["npm", "run", "render"], cwd=work, check=True, capture_output=True, text=True)
    mp4s = sorted(renders.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not mp4s:
        return None
    if cfg.get("subtitles", True) and G.srt_has_cues(csrt):
        G.burn_subtitles(mp4s[-1], csrt, dest, cfg)
    else:
        shutil.copy(mp4s[-1], dest)
    return {"duration": duration, "narration": " ".join(lines), "lines": lines}


def _prepare_desk(cfg: dict) -> tuple[dict | None, dict | None, list[str] | None]:
    """Capture the live dashboard and write the desk slide's script.

    Everything here is best-effort by design: a briefing about the market must
    not fail because the operator's own dashboard is down, so each step that can
    go wrong returns (None, None, None) and the deck loses one slide instead of
    the whole render. Gemma writes the narration behind a numeric guard; if it
    cannot get through the guard, the deterministic fact-sheet script ships.
    """
    if not D.dcfg(cfg).get("enabled", True):
        return None, None, None
    try:
        payload = D.capture(cfg, G.WORK / "tmp")
        if not payload:
            return None, None, None
        dg, facts = D.digest(payload, D.previous_digest(cfg))
    except Exception as exc:  # noqa: BLE001 — never fail a render over the desk slide
        print(f"  [desk] snapshot unavailable ({type(exc).__name__}: {exc}) — no desk slide")
        return None, None, None

    n = int(D.dcfg(cfg).get("lines", 4))
    lines = None
    if cfg.get("director", {}).get("enabled", True):
        # Gemma must have the GPU to itself, same rule as the zh translation pass.
        if cfg.get("image_backend") == "comfyui":
            from . import comfy
            comfy.free_memory(cfg)
        director.unload(cfg)
        lines = director.desk_narration(facts, cfg, n=n)
        director.unload(cfg)
    if not lines:
        print("  [desk] using the deterministic fact-sheet script")
        lines = D.fallback_lines(dg, facts, n)
    print(f"  [desk] {len(lines)} narration line(s), "
          f"{'screenshot' if dg.get('has_shot') else 'no screenshot — typographic slide'}")
    return payload, dg, lines


def render_bullet_slideshow(dec: dict, cfg: dict, date: str, out_dir: Path,
                            stem: str | None = None) -> tuple[Path, list, list[dict]]:
    """The bullets-style slideshow: overview + one slide per symbol, stitched with
    the configured cross-fade. No GPU work at all.

    `stem` is the caller's edition filename stem (generate.edition_stem) — the ONE
    name every file of an edition shares. It must be threaded in rather than
    rebuilt here: when generate.py started naming editions per label and this
    still hardcoded `briefing_<date>`, the render succeeded and then main() went
    looking for a file nobody had written (2026-07-31, three editions lost)."""
    stem = stem or f"briefing_{date}"
    notes, biasd, wl = dec.get("notes", {}), dec.get("bias", {}), dec.get("watchlist", [])
    priority = dec.get("priority", {})
    picks = [s for s in wl if s in notes and s != "_"]
    picks.sort(key=lambda s: (biasd.get(s, "flat") != "flat", priority.get(s, 0), -wl.index(s)), reverse=True)
    picks = picks[: cfg.get("max_symbols", 5)]
    include_ov = cfg.get("story", {}).get("include_overview", True) and notes.get("_")
    scene_syms = (["OVERVIEW"] if include_ov else []) + picks
    if not scene_syms:
        raise RuntimeError("no symbols to render")

    # The DESK slide — the live dashboard, narrated from the account's own
    # numbers. Prepared BEFORE the render loop because it can decline: no
    # dashboard, no account, no narration that survives the numeric guard, and
    # the deck simply renders as it always did (see briefing/desk.py).
    desk_payload, desk_dg, desk_lines = _prepare_desk(cfg)
    order = list(scene_syms)
    if desk_lines:
        after = D.dcfg(cfg).get("after", "OVERVIEW")
        order.insert(order.index(after) + 1 if after in order else 0, "DESK")

    clip_paths, clips_meta, rendered = [], [], []
    for sym in order:
        dest = out_dir / f"clip_{G._safe(sym)}.mp4"
        print(f"  [slide] rendering {sym} …")
        if sym == "DESK":
            meta = D.render_desk_slide(desk_payload, desk_dg, desk_lines, cfg, date, dest)
            if not meta:
                print("  [slide] DESK skipped")
                continue
            clip_paths.append(dest)
            clips_meta.append({"symbol": "DESK", "clip_file": dest.name,
                               "duration_s": round(meta["duration"], 2), "bias": None,
                               "headline": meta["narration"][:150],
                               # lifted into meta["desk"] by generate.main — it is
                               # the baseline the NEXT edition diffs against.
                               "desk": desk_dg})
            rendered.append({"sym": "DESK", "kind": "desk", "lines": meta["lines"]})
            continue
        note = notes["_"] if sym == "OVERVIEW" else notes[sym]
        brief = G.briefing_for(dec, sym)
        meta = render_bullet_slide(sym, biasd.get(sym), brief, note, cfg, date, dest)
        if not meta:
            print(f"  [slide] {sym} skipped")
            continue
        clip_paths.append(dest)
        clips_meta.append({"symbol": "MARKET" if sym == "OVERVIEW" else sym,
                           "clip_file": dest.name, "duration_s": round(meta["duration"], 2),
                           "bias": None if sym == "OVERVIEW" else biasd.get(sym),
                           "headline": meta["narration"][:150]})
        rendered.append({"sym": sym, "brief": brief, "note": note, "lines": meta["lines"]})
    if not clip_paths:
        raise RuntimeError("no slides rendered")
    briefing = out_dir / f"{stem}.mp4"
    print(f"  [concat] {len(clip_paths)} slide(s) -> {briefing.name}")
    G.stitch(clip_paths, briefing, cfg, scope="scene")

    # Mandarin variant: translate the finished narration in ONE batched Gemma call,
    # then re-render each slide (same EN slide text/cards) timed to the zh voiceover.
    zh = cfg.get("zh", {})
    if zh.get("enabled") and rendered:
        from . import director
        # Fresh Gemma load for the translation: ComfyUI may hold VRAM from another
        # tier and a Gemma loaded under pressure keeps its bad GPU/CPU split
        # forever (documented ollama gotcha) -> timeouts/garbage. Free both first.
        if cfg.get("image_backend") == "comfyui":
            from . import comfy
            comfy.free_memory(cfg)
        director.unload(cfg)
        flat = [(ri, li) for ri, r in enumerate(rendered) for li in range(len(r["lines"]))]
        print(f"  [zh] translating {len(flat)} narration line(s) …")
        zh_lines = director.translate_lines([rendered[ri]["lines"][li] for ri, li in flat], cfg)
        director.unload(cfg)
        if zh_lines:
            for (ri, li), ln in zip(flat, zh_lines):
                rendered[ri].setdefault("lines_zh", [None] * len(rendered[ri]["lines"]))[li] = ln
            zh_clips, broke = [], False
            for ri, r in enumerate(rendered):
                sym = r["sym"]
                # A slide missing even ONE Mandarin line is dropped from the 中文
                # cut on its own — it does not take the other slides with it.
                if None in (r.get("lines_zh") or [None]):
                    print(f"  [zh] {sym}: line(s) untranslated — dropping this slide")
                    continue
                dest_zh = out_dir / f"clip_{G._safe(sym)}_zh.mp4"
                print(f"  [slide] rendering {sym} (zh) …")
                if r.get("kind") == "desk":
                    # Same photograph, same camera moves, re-timed to the Mandarin
                    # voiceover — the dashboard on screen stays in English.
                    mz = D.render_desk_slide(desk_payload, desk_dg, r["lines_zh"], cfg, date,
                                             dest_zh, voice=zh.get("voice", "zh-CN-YunyangNeural"),
                                             rate=zh.get("rate"))
                else:
                    mz = render_bullet_slide(sym, biasd.get(sym), r["brief"], r["note"], cfg, date,
                                             dest_zh, lines=r["lines_zh"],
                                             voice=zh.get("voice", "zh-CN-YunyangNeural"),
                                             rate=zh.get("rate"))
                if not mz:
                    # A RENDER failure is not a translation gap: it points at
                    # something systemic, so it still abandons the variant.
                    broke = True
                    break
                zh_clips.append((ri, dest_zh))
            if not broke and _zh_enough(len(zh_clips), len(rendered)):
                briefing_zh = out_dir / f"{stem}_zh.mp4"
                print(f"  [concat] zh variant ({len(zh_clips)}/{len(rendered)} slide(s)) "
                      f"-> {briefing_zh.name}")
                G.stitch([p for _, p in zh_clips], briefing_zh, cfg, scope="scene")
                for ri, zc in zh_clips:
                    clips_meta[ri]["clip_file_zh"] = zc.name
                # so the zh clips archive too
                clip_paths = clip_paths + [p for _, p in zh_clips]
            else:
                print("  [zh] slide variant incomplete — English-only slideshow")
        else:
            print("  [zh] translation unavailable — English-only slideshow")
    return briefing, clip_paths, clips_meta


def render_scenes(dec: dict, cfg: dict, date: str, out_dir: Path,
                  stem: str | None = None) -> tuple[Path, list, list[dict]]:
    """Render the slideshow: the typographic bullet slideshow (no characters,
    props or image generation — those live in the Wan recap video only).
    Returns (briefing_path, clip_paths, clips_meta); raises if nothing renders."""
    return render_bullet_slideshow(dec, cfg, date, out_dir, stem)


# ==========================================================================
# THE "WAN" STORY PIPELINE (`--renderer wan`; the weekend recap). Each symbol
# gets a multi-shot SCREENPLAY; each beat = a spoken line + a DISTINCT action.
# The pipeline is still named for Wan2.2, its first video engine, but the shots
# are now rendered by MiniMax H3 (story.video_engine "h3") from the cast sprite
# plus a klein storyboard per beat. Each beat clip's length = its spoken
# duration, so the shots fill the narration with varied motion (no looping). Key
# numbers/dates from the briefing pop on screen as crisp overlays.
# ==========================================================================
_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
def _first_frame(sym: str, character: dict, setting: str, cfg: dict, pipe, dest: Path) -> None:
    """The Wan I2V conditioning frame: an SDXL cartoon set for this scene with the
    FIXED character sprite composited on top, so Wan animates the exact character."""
    from PIL import Image
    steps = int(cfg.get("image_steps", 4))
    guidance = float(cfg.get("image_guidance", 0.0))
    try:
        bg = pipe(prompt=(setting or "a simple cartoon stage") + _BG_SUFFIX,
                  num_inference_steps=steps, guidance_scale=guidance,
                  height=704, width=1280).images[0].convert("RGBA")
    except Exception:  # noqa: BLE001
        bg = Image.new("RGBA", (1280, 704), (228, 234, 242, 255))
    sp = CHAR_DIR / f"{G._safe(sym)}.png"
    if sp.exists():
        ch = Image.open(sp).convert("RGBA")
        h = int(704 * 0.62)
        ch.thumbnail((h, h))
        bg.alpha_composite(ch, ((1280 - ch.width) // 2, 704 - 28 - ch.height))
    bg.convert("RGB").save(dest)


def _beat_overlay_png(symbol: str, name: str, callout: dict | None, W: int, H: int,
                      dest: Path, indicators: list | None = None) -> None:
    """Build the whole HUD as one transparent PNG: ticker/name (top-left), a
    persistent INDICATOR STRIP under the name (the key technical stats, listed on
    every shot), and — if the beat has a number — a rounded STAT CARD (top-right) =
    label + the SDXL price/prop thumbnail + the big value. Composited in one step."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def font(sz):
        return ImageFont.truetype(_FONT, sz)

    def shadow(xy, text, f, fill):
        d.text((xy[0] + 3, xy[1] + 3), text, font=f, fill=(0, 0, 0, 170))
        d.text(xy, text, font=f, fill=fill)

    def centre(x0, w, y, text, f, fill):
        tw = d.textlength(text, font=f)
        shadow((x0 + (w - tw) / 2, y), text, f, fill)

    shadow((64, 44), symbol, font(74), (255, 255, 255, 255))
    shadow((66, 132), name, font(33), (242, 169, 59, 255))

    # persistent indicator strip (up to 3): a pill per stat, label muted + value amber.
    inds = [i for i in (indicators or []) if isinstance(i, dict)][:3]
    fy = 190
    lf, vf = font(27), font(29)
    for it in inds:
        lab, val = (it.get("label") or "").strip()[:16], (it.get("value") or "").strip()[:18]
        if not (lab or val):
            continue
        lw = d.textlength(lab + "  ", font=lf) if lab else 0
        vw = d.textlength(val, font=vf) if val else 0
        d.rounded_rectangle([62, fy, 62 + 22 + lw + vw + 20, fy + 44], radius=11, fill=(12, 16, 24, 190))
        if lab:
            d.text((80, fy + 9), lab, font=lf, fill=(178, 190, 208, 255))
        if val:
            d.text((80 + lw, fy + 7), val, font=vf, fill=(242, 200, 120, 255))
        fy += 54

    if callout and callout.get("value"):
        pw, ph = 300, 336                    # kept compact — the card sits over the scene
        px, py = W - pw - 36, 64
        d.rounded_rectangle([px, py, px + pw, py + ph], radius=22, fill=(14, 18, 26, 170))
        lab, val = callout.get("label", ""), callout["value"]
        if lab:
            centre(px, pw, py + 20, lab.upper(), font(26), (200, 208, 220, 255))
        prop = callout.get("_prop")
        if prop and Path(prop).exists():
            th = Image.open(prop).convert("RGBA")
            th.thumbnail((200, 200))
            img.alpha_composite(th, (int(px + (pw - th.width) / 2), py + 60))
            vy = py + 254
        else:
            vy = py + 180
        # shrink the value font until it fits inside the card (long labels like
        # "AI Supercycle" overflowed the fixed size off the card edge).
        vsz = 70
        while vsz > 24 and d.textlength(val, font=font(vsz)) > pw - 32:
            vsz -= 6
        centre(px, pw, vy + (70 - vsz) // 2, val, font(vsz), (242, 169, 59, 255))
    img.save(dest)


def _beat_clip(raw: Path, mp3: Path, srt: Path | None, symbol: str, name: str,
               callout: dict | None, dur: float, cfg: dict, dest: Path,
               indicators: list | None = None) -> None:
    """One beat = the Wan shot + its voiceover + burned subtitle + the HUD overlay
    (ticker + indicator strip + stat card with the price/prop icon and number)."""
    work = G.WORK
    shutil.copy(raw, work / "wan_raw.mp4")
    W, H = 1280, 704                                # the Wan render size (see _wan_clip)
    _beat_overlay_png(symbol, name, callout, W, H, work / "hud.png", indicators=indicators)

    chain = "[0:v]tpad=stop_mode=clone:stop_duration=6"
    if cfg.get("subtitles", True) and G.srt_has_cues(srt):
        shutil.copy(srt, work / "sub.srt")
        fs = int(cfg.get("subtitle_fontsize", 11))
        mv = int(cfg.get("subtitle_margin_v", 14))
        style = (f"Fontsize={fs},PrimaryColour=&H00F4F6FB,OutlineColour=&HC0000000,"
                 f"BorderStyle=1,Outline=2,Shadow=1,MarginV={mv},Alignment=2,Bold=1")
        chain += f",subtitles=sub.srt:force_style='{style}'"
    fc = f"{chain}[b];[b][1:v]overlay=0:0[v]"
    # Intermediate: concat_with_transitions re-encodes this whole timeline again,
    # so the x264 default preset was optimising bits that get discarded one pass
    # later (see G.INTERMEDIATE_PRESET for the measurements).
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "wan_raw.mp4", "-i", "hud.png",
                    "-i", str(mp3), "-filter_complex", fc, "-map", "[v]", "-map", "2:a",
                    "-t", f"{dur:.2f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-preset", G.INTERMEDIATE_PRESET, "-c:a", "aac",
                    str(dest)], cwd=work, check=True, capture_output=True, text=True)


def render_scenes_wan(dec: dict, cfg: dict, date: str, out_dir: Path,
                      stem: str | None = None) -> tuple[Path, list, list[dict]]:
    """Image-to-video renderer: a market OVERVIEW scene plus one scene per symbol.
    Each scene's screenplay has varied shots that fill the narration; every shot
    is MiniMax H3 conditioned on the FIXED character sprite plus a per-beat
    storyboard, so the exact cast is preserved. Phases never share the GPU:
    (1) screenplays → unload; (2) edge-tts timing; (3) storyboards / first frames
    + number icons → free; (4) the batched H3 run. Then overlays + concat."""
    stem = stem or f"briefing_{date}"
    from . import director
    import json as _json
    import os as _os
    reg = load_registry()
    notes, biasd, wl = dec.get("notes", {}), dec.get("bias", {}), dec.get("watchlist", [])
    priority = dec.get("priority", {})
    picks = [s for s in wl if s in notes and s != "_"]
    picks.sort(key=lambda s: (biasd.get(s, "flat") != "flat", priority.get(s, 0), -wl.index(s)), reverse=True)
    picks = picks[: cfg.get("max_symbols", 5)]
    scene_syms = (["OVERVIEW"] if notes.get("_") else []) + picks
    if not scene_syms:
        raise RuntimeError("no scenes to render")

    def char_for(sym):
        return reg.get(sym) or reg.get(sym.split("/")[0]) or {
            "name": sym, "kind": "character", "description": f"a flat cartoon mascot for {sym}"}

    def note_for(sym):
        return notes["_"] if sym == "OVERVIEW" else notes[sym]

    tmp = G.WORK / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    # Free any ComfyUI-resident SD 3.5 VRAM AND force a fresh Gemma load before
    # planning (gemma think:high needs ~23GB; a resident image
    # model — or a stale CPU-offloaded Gemma from a killed render — starves planning).
    if cfg.get("image_backend") == "comfyui":
        from . import comfy
        comfy.free_memory(cfg)
        director.unload(cfg)

    # (1) screenplays (the director model) → unload. Fed the richer per-symbol
    # briefing (story + catalysts + indicators) so each scene is ~1 min of
    # fact-dense, varied shots.
    # Which video engine renders the shots: 'h3' = MiniMax H3 ref2va, 1-2
    # beats per generation with a real internal cut, conditioned on the cast
    # sprite + per-beat storyboards (proven 2026-08-04). h3 also asks the
    # director for per-beat camera. It is the only engine: Wan2.2 (A14B /
    # TI2V-5B per-beat I2V) was retired as a renderer, and refusing here —
    # before any GPU work — lets generate.py fall back to the slideshow.
    engine = cfg.get("story", {}).get("video_engine", "h3")
    if engine != "h3":
        raise RuntimeError(f"story.video_engine={engine!r}: Wan2.2 was retired as a "
                           f"renderer — story.video_engine must be 'h3'")
    _write_screenplay = director.scene_screenplay_h3

    def _one_screenplay(sym):
        sp = None
        for attempt in (1, 2):
            sp = _write_screenplay(sym, biasd.get(sym), note_for(sym), char_for(sym), cfg,
                                           brief=G.briefing_for(dec, sym))
            if sp and sp.get("beats"):
                return sp
            # An empty screenplay used to drop the scene SILENTLY — a timed-out
            # Gemma call (typically it stalling on CPU because something else still
            # held the VRAM) shipped a recap with no market-overview scene at all,
            # and nothing in the log said so. Retry once, then be loud.
            print(f"  [wan] {sym}: screenplay attempt {attempt} produced nothing"
                  + (" — retrying" if attempt == 1 else ""))
        return sp

    screens = {}
    d_model = cfg.get("director", {}).get("model", "gemma4:31b")
    if director._is_cloud(d_model) and len(scene_syms) > 1:
        # A cloud director has no VRAM to contend for, so the scenes' screenplays
        # can be written CONCURRENTLY — at think:max one screenplay is ~5 min, so
        # sequential would put ~30 min of pure waiting in front of the render.
        from concurrent.futures import ThreadPoolExecutor
        print(f"  [wan] {len(scene_syms)} screenplays in parallel ({d_model}) …")
        with ThreadPoolExecutor(max_workers=min(6, len(scene_syms))) as ex:
            futs = {sym: ex.submit(_one_screenplay, sym) for sym in scene_syms}
        results = {sym: f.result() for sym, f in futs.items()}
    else:
        results = {}
        for sym in scene_syms:
            print(f"  [wan] screenplay {sym} = {char_for(sym).get('name','?')} …")
            results[sym] = _one_screenplay(sym)
    for sym in scene_syms:                     # keep scene order deterministic
        sp = results.get(sym)
        if sp and sp.get("beats"):
            print(f"  [wan] screenplay {sym} = {char_for(sym).get('name','?')}: "
                  f"{len(sp['beats'])} beat(s)")
            screens[sym] = sp
        else:
            print(f"  [wan] WARNING: {sym} has NO screenplay after 2 attempts — "
                  f"this scene will be MISSING from the video")
    # Mandarin variant: translate the FINISHED narration now, while Gemma is still
    # loaded (one batched call across every scene). Same video — only audio + subs.
    zh = cfg.get("zh", {})
    if screens and zh.get("enabled"):
        flat = [(sym, i) for sym, sp in screens.items() for i in range(len(sp["beats"]))]
        print(f"  [zh] translating {len(flat)} narration line(s) …")
        zh_lines = director.translate_lines(
            [screens[sym]["beats"][i]["line"] for sym, i in flat], cfg)
        if zh_lines:
            for (sym, i), ln in zip(flat, zh_lines):
                screens[sym]["beats"][i]["line_zh"] = ln
        else:
            print("  [zh] translation unavailable — English-only render")
    director.unload(cfg)
    if not screens:
        raise RuntimeError("no screenplays")

    # (2) time every beat with edge-tts (plus the zh track when translated).
    for sym, sp in screens.items():
        for i, b in enumerate(sp["beats"]):
            mp3 = tmp / f"b_{G._safe(sym)}_{i}.mp3"
            srt = tmp / f"b_{G._safe(sym)}_{i}.srt"
            G.synth_audio(b["line"], mp3, srt, cfg)
            b["_mp3"], b["_srt"], b["_dur"] = mp3, srt, G._duration(mp3)
            if b.get("line_zh"):
                mp3z = tmp / f"bz_{G._safe(sym)}_{i}.mp3"
                srtz = tmp / f"bz_{G._safe(sym)}_{i}.srt"
                G.synth_audio(b["line_zh"], mp3z, srtz, cfg,
                              voice=zh.get("voice", "zh-CN-YunyangNeural"), rate=zh.get("rate"))
                b["_mp3_zh"], b["_srt_zh"], b["_dur_zh"] = mp3z, srtz, G._duration(mp3z)

    # (3a) KLEIN STORYBOARDS (default): one frame PER BEAT — the cast character
    # (ref_desc + adopted sprite as a ReferenceLatent) posed IN the crayon scene
    # doing that beat's action. Fixed scene style from config; the screenplay
    # supplies content only. Runs over the ComfyUI HTTP API (no CUDA context in
    # this process). Any scene with zero frames falls through to the composite
    # path below; klein wholly unavailable -> the old path handles everything.
    sb_cfg = cfg.get("story", {}).get("storyboard", {})
    sb_done = set()
    sb_jobs = []                # function scope: the wan-QC escalation reads it too
    if cfg.get("images_enabled", True) and sb_cfg.get("enabled", True) \
            and cfg.get("image_backend") == "comfyui":
        from . import comfy
        if comfy.klein_models_present(cfg) and comfy.ensure_server(cfg):
            for stale in tmp.glob("sb_*.png"):   # never reuse another run's frames
                stale.unlink()
            style = sb_cfg.get("scene_style", "")
            tail = sb_cfg.get("scene_tail", "")
            lock = sb_cfg.get("identity_lock", "")
            n_cand = max(1, int(sb_cfg.get("candidates", 3)))
            n_frames = sum(len(sp["beats"]) for sp in screens.values())
            print(f"  [wan] klein storyboards: {n_frames} beat frame(s)"
                  + (f" x {n_cand} candidates = {n_frames * n_cand} draw(s)" if n_cand > 1 else "")
                  + " …")
            for sym, sp in screens.items():
                char = char_for(sym)
                ref = CHAR_DIR / f"{G._safe(sym)}.png"
                desc = char.get("ref_desc")
                if not ref.exists() or not desc:
                    print(f"  [wan] {sym}: no ref sprite/ref_desc — composite fallback")
                    continue
                ok_beats = 0
                for i, b in enumerate(sp["beats"]):
                    ff = tmp / f"sb_{G._safe(sym)}_{i}.png"
                    prompt = (f"{style}{lock}The character is {desc}. Now the character "
                              f"{b['action']}, inside {sp.get('setting', 'a simple room')}."
                              f"{tail}")
                    # Draw n_cand alternative poses of this beat — same prompt, same
                    # setting, different seed — and let the review stage below keep the
                    # first anatomically clean one. The point isn't variety for its own
                    # sake: a single draw lands a 3-legged character often enough that
                    # we shipped two, and best-of-N kills that at klein prices. It also
                    # REPLACES a klein<->reviewer VRAM round-trip (what a flagged frame
                    # used to cost) with extra draws inside this one resident phase.
                    cands = []
                    for k in range(n_cand):
                        cf = ff if k == 0 else tmp / f"sb_{G._safe(sym)}_{i}.cand{k}.png"
                        if comfy.klein_storyboard(cfg, ref, prompt, cf,
                                                  seed=1000 + i + 104729 * k):
                            cands.append(cf)
                    # k=0 can fail while a later candidate succeeds; ff is the name the
                    # rest of the pipeline reads, so it must always be a real frame.
                    if cands and not ff.exists():
                        shutil.copy(cands[0], ff)
                    if cands:
                        b["_ff_beat"] = ff
                        ok_beats += 1
                        sb_jobs.append({"ref": ref, "prompt": prompt, "ff": ff,
                                        "seed": 1000 + i, "cands": cands})
                if ok_beats:
                    sp["_sb"] = True
                    sb_done.add(sym)
                print(f"  [wan] {sym}: {ok_beats}/{len(sp['beats'])} storyboard frame(s)")
            # HUD callout icons in the SAME crayon world (klein t2i, model already
            # resident). Raw white-background PNGs; first_frames_batch rembg-cuts
            # them. Any failure leaves the icon to the SD 3.5 fallback there.
            for stale in tmp.glob("kicon_*.png"):
                stale.unlink()
            istyle = sb_cfg.get(
                "icon_style",
                "A children's crayon drawing, minimalist doodle icon: one single object, "
                "thick wobbly hand-drawn crayon outline, sparse loose patches of bright "
                "color inside the lines, flat, no shading, cute and unpolished. ")
            n_icons = 0
            for sym, sp in screens.items():
                for i, b in enumerate(sp["beats"]):
                    c2 = b.get("callout")
                    if not (c2 and c2.get("image_prompt")):
                        continue
                    icon = tmp / f"kicon_{G._safe(sym)}_{i}.png"
                    iprompt = (f"{istyle}{c2['image_prompt']}, centered, isolated on a plain "
                               f"white paper background, no text, no words, no numbers")
                    if comfy.klein_icon(cfg, iprompt, icon, seed=7000 + i):
                        c2["_klein_raw"] = icon
                        n_icons += 1
            print(f"  [wan] klein icons: {n_icons} rendered")
            comfy.free_memory(cfg)          # drop klein before SD3.5 icons / Wan
            # Vision QC: the (multimodal) director model reviews every storyboard
            # frame for extra/floating limbs. It can't share VRAM with klein, so
            # the review runs batched AFTER klein is freed; flagged frames get a
            # fresh-seed klein re-render, up to sb_retries rounds.
            rv = cfg.get("story", {}).get("review", {})
            if rv.get("enabled", True) and sb_jobs:
                sb_retries = int(rv.get("sb_retries", 2))
                pending = sb_jobs
                for rnd in range(sb_retries + 1):
                    print(f"  [review] checking {len(pending)} storyboard frame(s) …")
                    bad = []
                    for j in pending:
                        # Walk this beat's candidates in order, stop at the first clean
                        # one. Lazy on purpose: when candidate 0 passes — the common
                        # case — this costs exactly one QC call, same as before
                        # candidates existed. Only a defect pays for the alternatives.
                        picked = next(
                            (c for c in (j.get("cands") or [j["ff"]])
                             if director.anatomy_ok(c, cfg) is not False), None)
                        if picked is None:
                            bad.append(j)
                            continue
                        if Path(picked) != Path(j["ff"]):
                            shutil.copy(picked, j["ff"])
                            print(f"  [review] {Path(j['ff']).stem}: candidate 0 flagged, "
                                  f"adopted {Path(picked).name}")
                    director.unload(cfg)     # free the reviewer before klein reloads
                    if not bad:
                        break
                    names = ", ".join(Path(j["ff"]).stem for j in bad)
                    if rnd == sb_retries:
                        print(f"  [review] still flagged, keeping last render: {names}")
                        break
                    print(f"  [review] {len(bad)} frame(s) flagged (extra limbs) — re-rendering: {names}")
                    if not comfy.ensure_server(cfg):
                        break
                    for j in bad:
                        comfy.klein_storyboard(cfg, j["ref"], j["prompt"], j["ff"],
                                               seed=j["seed"] + 7919 * (rnd + 1))
                        # This round redrew ff only; the candidate list is now stale and
                        # every entry in it has already been rejected once.
                        j["cands"] = [j["ff"]]
                    comfy.free_memory(cfg)
                    pending = bad
        else:
            print("  [wan] klein storyboard backend unavailable — composite first frames")

    # (3b) Composite first frames (for scenes WITHOUT storyboards) + number icons
    # for everyone. Generated in a SEPARATE process (first_frames_batch.py) so its
    # CUDA context is fully released before the Wan child runs — two live contexts
    # on one GPU make Wan I2V ~10x slower (a bare `del pipe; empty_cache()` does NOT
    # drop the parent process's context). This render process never touches CUDA.
    if cfg.get("images_enabled", True):
        import sys as _sys
        ff_scenes = []
        for sym, sp in screens.items():
            icons = [{"i": i, "prompt": b["callout"]["image_prompt"],
                      **({"klein_raw": str(b["callout"]["_klein_raw"])}
                         if b["callout"].get("_klein_raw") else {})}
                     for i, b in enumerate(sp["beats"])
                     if b.get("callout") and b["callout"].get("image_prompt")]
            ff_scenes.append({"sym": sym, "setting": sp.get("setting", ""),
                              "character": char_for(sym), "icons": icons,
                              "skip_ff": sym in sb_done})
        (tmp / "ff_jobs.json").write_text(_json.dumps({"cfg": cfg, "tmp": str(tmp), "scenes": ff_scenes}))
        print(f"  [wan] first frames + icons in a separate process ({len(ff_scenes)} scene(s)) …")
        # Clear any stale first frames/icons from a previous run FIRST — otherwise a
        # failed/short subprocess would silently reuse yesterday's sets (a real bug we
        # hit: the subprocess couldn't import `briefing` and the render used stale frames).
        for stale in list(tmp.glob("ff_*.png")) + list(tmp.glob("prop_*.png")) + list(tmp.glob("propraw_*.png")):
            stale.unlink()
        # Run first_frames_batch as a MODULE with the repo root on PYTHONPATH so its
        # `from briefing import ...` resolves regardless of how the parent was launched
        # (nohup'd `python -m briefing.generate` leaves no repo root on the child's path).
        env = {**_os.environ, "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
               "PYTHONPATH": str(G.ROOT) + _os.pathsep + _os.environ.get("PYTHONPATH", "")}
        r = subprocess.run([_sys.executable, "-m", "briefing.first_frames_batch",
                            str(tmp / "ff_jobs.json")], cwd=str(G.ROOT), env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [wan] first-frame subprocess failed (rc={r.returncode}): {r.stderr[-400:]}")
        # wire the produced artifacts back onto the screenplays
        n = 0
        for sym, sp in screens.items():
            ff = tmp / f"ff_{G._safe(sym)}.png"
            if ff.exists():
                sp["_ff"] = ff
            for i, b in enumerate(sp["beats"]):
                c = b.get("callout")
                if c and c.get("image_prompt"):
                    prop = tmp / f"prop_{G._safe(sym)}_{i}.png"
                    if prop.exists():
                        c["_prop"], n = prop, n + 1
        print(f"  [wan] first frames ready + {n} icon(s)")

    # If ComfyUI generated the first frames, it holds SD 3.5 resident — free that
    # VRAM before Wan (which needs ~30GB) loads, or they'd contend on the 32GB card.
    if cfg.get("image_backend") == "comfyui":
        from . import comfy
        comfy.free_memory(cfg)


    def _h3_beat_bad(mp4, dur, rv):
        """Same sampling QC the wan shots get, on a split h3 beat segment."""
        for frac in rv.get("wan_frames", (0.2, 0.4, 0.6, 0.8, 0.95)):
            fp = tmp / "review_frame.png"
            try:
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * frac:.2f}",
                                "-i", str(mp4), "-frames:v", "1", str(fp)],
                               check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError:
                continue
            if director.anatomy_ok(fp, cfg) is False:
                return True
        return False

    def _run_h3_batch():
        """MiniMax H3 ref2va engine: one generation per 1-2 beats with a real,
        timestamped internal cut, conditioned on the cast sprite (<Picture 1>,
        identity) plus each beat's klein storyboard (composition), rendered at
        the measured Wan-parity 960x544+sage point, then SPLIT back into the
        per-beat wanraw_* files at the narration boundaries — so the compositor,
        the zh variant and the HUD overlays run untouched. Server discipline:
        the v0.30 worktree server and the qwen reviewer never share VRAM."""
        from . import comfy as _comfy
        from . import h3 as h3mod
        hc = h3mod.hcfg(cfg)
        rv = cfg.get("story", {}).get("review", {})
        _comfy.free_memory(cfg)
        director.unload(cfg)
        clips = []
        for sym in scene_syms:
            sp = screens.get(sym)
            if not sp or not sp.get("_sb"):
                continue
            idref = h3mod.identity_ref(sym, tmp)
            beats = sp["beats"]
            durs = [b["_dur"] for b in beats]
            for gi, (idx, frames) in enumerate(director.h3_group_beats(durs)):
                gb = [beats[i] for i in idx]
                sbs = [b.get("_ff_beat") for b in gb]
                if not idref or not all(sbs):
                    print(f"  [h3] {sym} clip {gi}: missing sprite/storyboard — skipped")
                    continue
                for i in idx:
                    beats[i]["_raw"] = tmp / f"wanraw_{G._safe(sym)}_{i}.mp4"
                clips.append({
                    "sym": sym, "gi": gi, "frames": frames,
                    "durs": [durs[i] for i in idx],
                    "raws": [beats[i]["_raw"] for i in idx],
                    "refs": [idref] + [Path(s) for s in sbs],
                    "first_frame": Path(sbs[0]),
                    "prompt": director.h3_clip_prompt(
                        char_for(sym), sp.get("setting", "a simple room"), gb,
                        [durs[i] for i in idx], hc["style_lock"],
                        ref_pictures=hc.get("variant", "fl2va") == "ref2va"),
                    "dest": tmp / f"h3clip_{G._safe(sym)}_{gi}.mp4",
                    "seed": 21 + 17 * len(clips)})
        if not clips:
            raise RuntimeError("h3: no clips could be built (missing storyboards?)")
        print(f"  [h3] {hc.get('variant', 'fl2va')}: {len(clips)} clip(s) covering "
              f"{sum(len(c['raws']) for c in clips)} beat(s) …")
        todo = list(clips)
        for rnd in range(int(hc.get("retries", 1)) + 1):
            if not todo:
                break
            if not h3mod.ensure_server(cfg):
                raise RuntimeError("h3: ComfyUI worktree server unavailable")
            for n, c in enumerate(todo, 1):
                if hc.get("variant", "fl2va") == "ref2va":
                    c["rendered"] = h3mod.ref2va(cfg, c["prompt"], c["refs"], c["frames"],
                                                 c["seed"] + 9973 * rnd, c["dest"])
                else:
                    # fl2va: pixel-anchor on the first beat's storyboard
                    c["rendered"] = h3mod.i2v(cfg, c["prompt"], c["first_frame"],
                                              c["frames"], c["seed"] + 9973 * rnd,
                                              c["dest"])
                print(f"  [h3] clip {n}/{len(todo)} ({c['sym']}_{c['gi']}) "
                      f"{'done' if c['rendered'] else 'FAIL'}")
            h3mod.stop_server(cfg)              # VRAM to the qwen reviewer
            still = []
            for c in todo:
                if not c.get("rendered"):
                    still.append(c)
                    continue
                if not h3mod.split_beats(cfg, c["dest"], c["durs"], c["raws"]):
                    still.append(c)
                    continue
                if rv.get("enabled", True) and rnd <= int(hc.get("retries", 1)):
                    if any(_h3_beat_bad(raw, dur, rv)
                           for raw, dur in zip(c["raws"], c["durs"])):
                        if rnd < int(hc.get("retries", 1)):
                            print(f"  [h3] {c['sym']}_{c['gi']} flagged — re-rolling")
                            still.append(c)
                        else:
                            print(f"  [h3] {c['sym']}_{c['gi']} still flagged — keeping")
            director.unload(cfg)
            todo = still
        failed = [c for c in todo if not all(r.exists() for r in c["raws"])]
        if failed:
            print("  [h3] WARNING: no footage for: "
                  + ", ".join(f"{c['sym']}_{c['gi']}" for c in failed))

    _run_h3_batch()

    # ---- every Wan raw is on disk from here on. What follows is cheap
    # assembly (ffmpeg overlays + concat); if it breaks, the render did NOT
    # fail — surface WanCompositingError so generate.py refuses to fall
    # back to the slideshow and the raws stay salvageable (2026-08-02).
    try:
        # (5) compose each shot (HUD + subtitle + voiceover) and stitch per scene.
        # When the zh track exists, every shot is composed a second time from the SAME
        # Wan raw with the Mandarin voiceover + subtitles -> briefing_<date>_zh.mp4.
        clip_paths, clips_meta, zh_scene_clips = [], [], []
        for sym in scene_syms:
            sp = screens.get(sym)
            if not sp:
                continue
            char = char_for(sym)
            tick = "MARKET" if sym == "OVERVIEW" else sym
            inds = (G.briefing_for(dec, sym) or {}).get("indicators")
            beat_clips, zh_beat_clips = [], []
            for i, b in enumerate(sp["beats"]):
                raw = b.get("_raw")
                if not raw or not raw.exists():
                    continue
                bc = tmp / f"beat_{G._safe(sym)}_{i}.mp4"
                _beat_clip(raw, b["_mp3"], b["_srt"], tick, char.get("name", sym),
                           b.get("callout"), b["_dur"], cfg, bc, indicators=inds)
                beat_clips.append(bc)
                if b.get("_mp3_zh"):
                    bz = tmp / f"beatz_{G._safe(sym)}_{i}.mp4"
                    try:
                        _beat_clip(raw, b["_mp3_zh"], b["_srt_zh"], tick, char.get("name", sym),
                                   b.get("callout"), b["_dur_zh"], cfg, bz, indicators=inds)
                        zh_beat_clips.append(bz)
                    except Exception as exc:  # noqa: BLE001 — zh is the optional track:
                        # a bad zh beat costs the zh variant (same policy as the
                        # slideshow's "English-only" path), never the video itself.
                        print(f"  [zh] beat {G._safe(sym)}_{i} failed "
                              f"({type(exc).__name__}) — dropping the zh variant")
            if not beat_clips:
                continue
            dest = out_dir / f"clip_{G._safe(sym)}.mp4"
            G.stitch(beat_clips, dest, cfg, scope="beat")     # smooth cross-dissolve between shots
            clip_paths.append(dest)
            cm = {"symbol": tick, "clip_file": dest.name,
                  "duration_s": round(sum(b["_dur"] for b in sp["beats"]), 2),
                  "bias": biasd.get(sym),
                  "headline": " ".join(b["line"] for b in sp["beats"])[:150]}
            if zh_beat_clips and len(zh_beat_clips) == len(beat_clips):
                dest_zh = out_dir / f"clip_{G._safe(sym)}_zh.mp4"
                G.stitch(zh_beat_clips, dest_zh, cfg, scope="beat")
                zh_scene_clips.append(dest_zh)
                cm["clip_file_zh"] = dest_zh.name
            clips_meta.append(cm)
        if not clip_paths:
            raise RuntimeError("no wan scenes rendered")

        briefing = out_dir / f"{stem}.mp4"
        print(f"  [concat] {len(clip_paths)} wan scene(s) -> {briefing.name}")
        G.stitch(clip_paths, briefing, cfg, scope="scene")
        # Partial is fine here too: a scene whose beats did not all translate is
        # already skipped above, and losing one scene must not cost the variant.
        if _zh_enough(len(zh_scene_clips), len(clip_paths)):
            briefing_zh = out_dir / f"{stem}_zh.mp4"
            print(f"  [concat] zh variant ({len(zh_scene_clips)}/{len(clip_paths)} "
                  f"scene(s)) -> {briefing_zh.name}")
            G.stitch(zh_scene_clips, briefing_zh, cfg, scope="scene")
            clip_paths = clip_paths + zh_scene_clips          # so the zh clips archive too
        return briefing, clip_paths, clips_meta
    except Exception as exc:  # noqa: BLE001
        raise WanCompositingError(
            f"wan raws are rendered and intact in {tmp} — assembly failed "
            f"({type(exc).__name__}: {exc}). Re-composite from the raws; "
            "do NOT re-render or fall back.") from exc
