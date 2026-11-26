#!/usr/bin/env python3
"""Render the daily research briefing into narrated MP4 clips for the dashboard.

An OFFLINE, post-research step. It reads the same ``decisions/decisions.json``
the relay reads and turns each symbol's free-form research ``note`` into a short
narrated, subtitled video scene with animated callout cards:

    note text ──edge-tts──> narration .mp3 + .srt (word-timed subtitles)
    theme     ──SDXL─────> dimmed terminal background + small callout thumbnails
    the above ──hyperframes(HTML+GSAP → headless Chrome + ffmpeg)──> clip .mp4
    subtitles ──ffmpeg───> burned into each clip
    all clips ──ffmpeg concat──> briefing_<date>.mp4

Storage (see config.json):
  * The CURRENT briefing + its clips + manifest/history live under ``out_dir``
    (web/static/media, the system SSD) so the dashboard serves them locally.
  * EVERY briefing is also archived under ``archive_dir``
    (/mnt/sandisk/alpaca/briefings/<date>/) — the durable history. The dashboard
    serves those via the server's read-only /archive/ route.
  * out_dir is pruned to just the current date each run; older briefings live
    only in the archive.

This touches NOTHING on the trading/relay path — it only reads decisions.json
and writes media. Runs in the ``alpaca-media`` conda env (edge-tts + torch/
diffusers + node/ffmpeg), NOT the trading ``.venv``. See briefing/run_briefing.sh.

Usage:
    python -m briefing.generate                 # full render (SDXL backgrounds)
    python -m briefing.generate --no-images      # gradient bg, no callouts, no GPU
    python -m briefing.generate --symbols NVDA,AMZN
    python -m briefing.generate --max-symbols 3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import director

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "briefing" / "config.json"
DECISIONS_PATH = ROOT / "decisions" / "decisions.json"
WORK = ROOT / "briefing" / ".hf_work"          # scratch hyperframes project (gitignored)

# x264 preset for INTERMEDIATE encodes — the per-beat raws and per-beat composites
# that a later pass re-encodes anyway. x264 defaults to "medium", which spent real
# time optimising bits that the next generation throws away. Measured 2026-08-06 on
# one real 10s beat at 1280x704, identical crf:
#     split  medium 5.58s -> veryfast 1.74s   (3.2x, file 5.33 -> 5.20 MB)
#     beat   medium 6.92s -> veryfast 2.19s   (3.2x, file 4.31 -> 3.93 MB)
# veryfast is both faster AND slightly smaller here, at SSIM 0.972 against medium.
# ultrafast is faster still (0.90s) but 2.2x the bytes for SSIM 0.969 — not worth
# the disk churn. h264_nvenc was NOT the win it looked like: 1.39s but 2.2x the
# bytes, because per-invocation GPU setup dominates on clips this short.
# FINAL encodes (concat_with_transitions, the published master) deliberately keep
# the default preset — quality there is watched, not thrown away.
INTERMEDIATE_PRESET = "veryfast"

# Bias -> (on-screen accent colour, image-theme colour word).
BIAS_COLOR = {
    "long":  ("#34b892", "emerald green"),
    "short": ("#e2596b", "crimson red"),
    "flat":  ("#f2a93b", "amber"),
    None:    ("#7b8fff", "cyan teal"),          # overview / no bias
}
SPEAK_AS = {"BTC/USD": "Bitcoin", "ETH/USD": "Ethereum"}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_config() -> dict:
    """Read briefing/config.json — or the file named by $BRIEFING_CONFIG, so an
    experiment can run a variant config (different director model, desk off)
    without ever editing the live file the cron renders read."""
    path = Path(os.environ["BRIEFING_CONFIG"]) if os.getenv("BRIEFING_CONFIG") else CONFIG_PATH
    cfg = json.loads(path.read_text())
    cfg.pop("_comment", None)
    return cfg


# --------------------------------------------------------------------------
# text shaping (deterministic — no LLM)
# --------------------------------------------------------------------------
def _strip_preamble(note: str) -> str:
    """Drop the ``FLAT, priority 0 -- `` lead-in; the bias is shown/spoken already."""
    return re.sub(r"^\s*(FLAT|LONG|SHORT|long|short|flat),\s*priority\s*\d+\s*(--|—|,)?\s*",
                  "", note).strip()


def _clean_for_speech(text: str) -> str:
    """Make a research note read naturally out loud."""
    text = text.replace("--", ", ").replace("—", ", ")
    text = text.replace(">=", "at least ").replace("<=", "at most ")
    text = text.replace(">", " above ").replace("<", " below ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _trim_to_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    if cut >= limit * 0.5:
        return window[: cut + 1]
    return window.rsplit(" ", 1)[0].rstrip(",;: ") + "."


def spell_symbol(symbol: str) -> str:
    if symbol in SPEAK_AS:
        return SPEAK_AS[symbol]
    if symbol.isalpha() and len(symbol) <= 5:
        return " ".join(list(symbol)) + "."     # NVDA -> "N V D A."
    return symbol


def speak_text(symbol: str, bias: str | None, note: str, limit: int) -> str:
    src = note if symbol == "OVERVIEW" else _strip_preamble(note)
    body = _trim_to_sentence(_clean_for_speech(src), limit)
    if symbol == "OVERVIEW":
        return f"Market overview. {body}"
    head = spell_symbol(symbol)
    if bias:
        head = f"{head} Bias, {bias}."
    return f"{head} {body}"


def headline_text(note: str, limit: int = 150) -> str:
    """Short on-screen caption / manifest headline: the note's first sentence."""
    first = _clean_for_speech(_strip_preamble(note))
    m = re.search(r"[.?!]\s", first)
    if m and m.end() <= limit + 30:
        first = first[: m.start() + 1]
    return _trim_to_sentence(first, limit)


# --- callout extraction ---------------------------------------------------
_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"


def extract_price_move(note: str) -> tuple[str, str]:
    """Return (label, direction) e.g. ('+1.8%', 'up'). Falls back to RED/GREEN cues."""
    m = re.search(r"([+-]\d+(?:\.\d+)?%)", note)
    if m:
        val = m.group(1)
        return val, ("down" if val.startswith("-") else "up")
    low = note.lower()
    if re.search(r"\bred\b|breaking down|slides?|falling|down the", low):
        return "RED", "down"
    if re.search(r"\bbounce|\brally|\bgreen\b|pop\b|higher", low):
        return "BID", "up"
    return "FLAT", "flat"


def extract_catalyst(note: str) -> str:
    """A short upper-case tag for the catalyst card (earnings date, or a keyword)."""
    m = re.search(rf"earnings\s+(?:\w+\s+)?((?:{_MONTHS})\s+\d{{1,2}})", note, re.I)
    if m:
        return f"EARNINGS {m.group(1).upper()}"
    for kw, tag in [("upgrade", "UPGRADE"), ("downgrade", "DOWNGRADE"),
                    ("debt", "DEBT / CREDIT"), ("delay", "PRODUCT DELAY"),
                    ("backlog", "DEMAND BACKLOG"), ("buy ", "ANALYST BUY"),
                    ("capex", "AI CAPEX"), ("inflow", "FUND INFLOWS")]:
        if kw in note.lower():
            return tag
    return "IN FOCUS"


# --------------------------------------------------------------------------
# scene selection
# --------------------------------------------------------------------------
def select_scenes(dec: dict, cfg: dict, override: list[str] | None) -> list[dict]:
    notes = dec.get("notes", {})
    bias = dec.get("bias", {})
    priority = dec.get("priority", {})
    watchlist = dec.get("watchlist", [])

    def order_key(s: str):
        return (bias.get(s, "flat") != "flat", priority.get(s, 0),
                -(watchlist.index(s) if s in watchlist else 999))

    if override:
        picks = [s for s in override if s in notes]
    else:
        cand = [s for s in watchlist if s in notes and s != "_"]
        cand.sort(key=order_key, reverse=True)
        picks = cand[: cfg.get("max_symbols", 5)]

    scenes: list[dict] = []
    if notes.get("_"):
        scenes.append({"symbol": "OVERVIEW", "bias": None, "note": notes["_"],
                       "limit": cfg.get("max_overview_chars", 850)})
    for s in picks:
        scenes.append({"symbol": s, "bias": bias.get(s), "note": notes[s],
                       "limit": cfg.get("max_note_chars", 380)})
    return scenes


# --------------------------------------------------------------------------
# edge-tts narration (+ word-timed subtitles)
# --------------------------------------------------------------------------
def synth_audio(text: str, out: Path, srt: Path, cfg: dict,
                voice: str | None = None, rate: str | None = None) -> float:
    out.parent.mkdir(parents=True, exist_ok=True)
    voice = voice or cfg.get("voice", "en-US-AndrewNeural")
    cmd = ["edge-tts", "--voice", voice,
           "--text", text, "--write-media", str(out), "--write-subtitles", str(srt)]
    if rate or cfg.get("rate"):
        cmd += ["--rate", rate or cfg["rate"]]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    if voice.startswith("zh"):
        _rechunk_cjk_srt(srt, int(cfg.get("subtitle_zh_max_chars", 24)))
    return _duration(out)


_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+),(\d+)")


def _rechunk_cjk_srt(srt_path: Path, max_chars: int = 24) -> None:
    """edge-tts emits ONE cue per zh sentence (a 13s 75-char wall), and libass
    can't wrap Chinese (no spaces to break on) so it runs off both screen edges.
    Split long cues at punctuation into <=max_chars cues, time-shared by length."""
    def t2s(m):
        return int(m[0]) * 3600 + int(m[1]) * 60 + int(m[2]) + int(m[3]) / 1000
    def s2t(s):
        ms = int(round(s * 1000))
        h, rem = divmod(ms, 3600000); m, rem = divmod(rem, 60000); sec, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    try:
        blocks = [b for b in srt_path.read_text(encoding="utf-8").split("\n\n") if b.strip()]
    except OSError:
        return
    out_cues, n, parsed = [], 1, 0
    for b in blocks:
        ln = b.strip().split("\n")
        times = _SRT_TIME.findall(ln[1]) if len(ln) >= 3 else []
        text = "".join(ln[2:]).strip() if len(ln) >= 3 else ""
        if len(times) != 2 or not text:
            # A block this function cannot parse is still a block edge-tts wrote:
            # pass it through VERBATIM rather than dropping it. Dropping was how
            # 2026-08-02's bz_AMZN_3.srt became a 1-byte file that later killed
            # ffmpeg and cost the whole Wan render.
            out_cues.append(b.strip())
            continue
        parsed += 1
        if len(text) <= max_chars:
            chunks = [text]
        else:
            units = re.split(r"(?<=[，。：；！？、,.!?])", text)   # break after punctuation
            chunks, cur = [], ""
            for u in units:
                while len(u) > max_chars:                        # punctuation-free run
                    if cur:
                        chunks.append(cur); cur = ""
                    chunks.append(u[:max_chars]); u = u[max_chars:]
                if len(cur) + len(u) > max_chars and cur:
                    chunks.append(cur); cur = u
                else:
                    cur += u
            if cur:
                chunks.append(cur)
        t0, t1 = t2s(times[0]), t2s(times[1])
        total = max(1, sum(len(c) for c in chunks))
        cursor = t0
        for c in chunks:
            end = min(t1, cursor + (t1 - t0) * len(c) / total)
            out_cues.append(f"{n}\n{s2t(cursor)} --> {s2t(end)}\n{c.strip()}")
            cursor, n = end, n + 1
    if not parsed:
        # Nothing understood -> this rewrite would only destroy information.
        # Keep edge-tts's original file: over-long cues display badly, but a
        # bad display beats a dead render.
        return
    srt_path.write_text("\n\n".join(out_cues) + "\n", encoding="utf-8")


def _duration(media: Path) -> float:
    """Runtime in seconds, or 0.0 if it cannot be probed.

    Deliberately non-fatal. This is the LAST thing main() does before archiving
    and publishing, and the value is cosmetic — the dashboard's "Runtime" cell.
    On 2026-07-31 a raising version threw away three finished editions (one of
    them ~2h of Wan GPU) because it was handed a path the renderer had not
    written. That naming bug is fixed; this makes the blast radius of the next
    late surprise a wrong number in a table rather than a lost video."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(media)],
            check=True, capture_output=True, text=True)
        return float(r.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        print(f"  [warn] cannot probe duration of {media.name} "
              f"({type(exc).__name__}) — recording 0s and continuing")
        return 0.0


# --------------------------------------------------------------------------
# scene "plan": a background prompt + up to N animated element cards. Comes from
# THE DIRECTOR (Gemma) when present, else from deterministic extraction.
# --------------------------------------------------------------------------
def _default_bg_prompt(sc: dict) -> str:
    color = BIAS_COLOR.get(sc["bias"], BIAS_COLOR[None])[1]
    subject = "the broad US equity market" if sc["symbol"] == "OVERVIEW" else sc["symbol"]
    return (f"cinematic dark financial data terminal for {subject}, subtle glowing "
            f"{color} candlestick chart and market ticker wall, moody, high detail, "
            f"cinematic lighting, depth of field, 16:9, no text, no words")


def _default_elements(sc: dict, cfg: dict) -> list[dict]:
    """Deterministic price + catalyst cards (fallback when the director is off)."""
    if sc["symbol"] == "OVERVIEW" or not cfg.get("callouts", True):
        return []
    color = BIAS_COLOR.get(sc["bias"], BIAS_COLOR[None])[1]
    price_val, direction = extract_price_move(sc["note"])
    arrow = {"up": "green upward arrow", "down": "red downward arrow"}.get(direction, "amber sideways arrow")
    els = [{"label": "Price action", "value": price_val, "motion": "slide-left",
            "image_prompt": f"small square icon, glowing {arrow} over candlestick bars, "
                            f"dark background, minimalist, cinematic, no text, no words"}]
    if int(cfg.get("callout_count", 2)) > 1:
        cat = extract_catalyst(sc["note"])
        theme = ("an earnings report calendar and financial documents" if "EARNINGS" in cat
                 else "a breaking financial news headline on a glowing screen")
        els.append({"label": "Catalyst", "value": cat, "motion": "rise",
                    "image_prompt": f"small square icon representing {theme}, {color}, "
                                    f"dark background, minimalist, cinematic, no text, no words"})
    return els[: int(cfg.get("callout_count", 2))]


def _scene_plan(sc: dict, cfg: dict) -> dict:
    d = sc.get("director")
    if d:
        els = [{"label": e["label"] or e["role"].title(), "value": e["value"],
                "motion": e["motion"], "image_prompt": e["image_prompt"]}
               for e in d["elements"]]
        plan = {"bg_prompt": d["background_prompt"] or _default_bg_prompt(sc),
                "elements": els}
    else:
        plan = {"bg_prompt": _default_bg_prompt(sc),
                "elements": _default_elements(sc, cfg)}
    if "brief" in sc:
        # HOST MODE follows the production slide convention (story.py bullets
        # style): emoji icons only — no generated images — catalyst cards plus a
        # key-number stat grid, from the same briefing catalysts/indicators the
        # intraday slideshows used. Being part of the plan also keys the slide
        # resume cache, so a design change re-renders slides automatically.
        from . import story as _story        # lazy: story imports us at module load
        cats, inds = _story._bullet_cards(sc.get("brief"), sc["note"])
        # HIS MONEY FIRST, then the market. The pills animate in at 1.5, 1.85,
        # 2.2 and 2.55 s, so only the first one lands inside the three-second
        # window — prepending is what makes it a hook rather than a footnote,
        # and it costs the fourth index stat, which is the least-missed of them.
        hero = sc.get("desk_hero")
        if hero:
            inds = [hero["pill"]] + [i for i in inds
                                     if i["label"] != hero["pill"]["label"]]
            plan["hero"] = hero
        plan["cats"], plan["inds"] = cats[:3], inds[:4]
        # full-canvas layout (user pick 2026-08-11, arm B): content anchored
        # LEFT on a transparent ground, the presenter untouched on the right.
        # Part of the plan so flipping the layout re-keys the slide cache.
        plan["layout"] = (cfg.get("host_pip", {}) or {}).get("layout", "canvas")
    return plan


# --------------------------------------------------------------------------
# SDXL images: one dimmed background + small element thumbnails per scene
# (model loads once for the whole run; optional — gradient/no-thumb fallback)
# --------------------------------------------------------------------------
def generate_images(scenes: list[dict], out_dir: Path, cfg: dict) -> dict:
    """Return {symbol: {"bg": path|None, "elements": [{label,value,motion,thumb}]}}."""
    plans = {sc["symbol"]: _scene_plan(sc, cfg) for sc in scenes}

    def no_thumbs():
        return {sym: {"bg": None, "elements": [{**e, "thumb": None} for e in p["elements"]],
                      "cats": p.get("cats", []), "inds": p.get("inds", []),
                      "hero": p.get("hero")}
                for sym, p in plans.items()}

    if any("host" in sc for sc in scenes):
        # host slides are emoji-led (the production bullets convention): flat
        # chroma ground, no SD background, no thumbnails — skip the model load.
        print("  [images] host mode: emoji-led slides, skipping image generation")
        return no_thumbs()
    if not cfg.get("images_enabled", True):
        return no_thumbs()
    os.environ.setdefault("HF_HOME", cfg.get("hf_cache", str(ROOT / ".hf_cache")))
    from . import story as _story                 # lazy: avoid import cycle at module load
    model = cfg.get("image_model", "stabilityai/sdxl-turbo")
    print(f"  [images] loading {model} …")
    pipe = _story.load_pipe(cfg)                   # model-aware (SDXL / SD 3.5)
    if pipe is None:
        print("  [images] model unavailable; gradient fallback")
        return no_thumbs()

    out_dir.mkdir(parents=True, exist_ok=True)
    steps = int(cfg.get("image_steps", 4))
    guidance = float(cfg.get("image_guidance", 0.0))
    px = int(cfg.get("callout_px", 512))

    def gen(prompt: str, path: Path, size: int) -> Path | None:
        if not prompt:
            return None
        try:
            img = pipe(prompt=prompt, num_inference_steps=steps, guidance_scale=guidance,
                       height=size, width=size).images[0]
            img.save(path)
            return path
        except Exception as exc:  # noqa: BLE001
            print(f"  [images] gen failed ({exc})")
            return None

    result: dict = {}
    for sym, p in plans.items():
        bg = gen(p["bg_prompt"], out_dir / f"bg_{_safe(sym)}.png", 1024)
        els = []
        for i, e in enumerate(p["elements"]):
            thumb = gen(e["image_prompt"], out_dir / f"call_{_safe(sym)}_{i}.png", px)
            els.append({"label": e["label"], "value": e["value"], "motion": e["motion"], "thumb": thumb})
        result[sym] = {"bg": bg, "elements": els,
                       "cats": p.get("cats", []), "inds": p.get("inds", []),
                       "hero": p.get("hero")}
    return result


# --------------------------------------------------------------------------
# hyperframes composition + render
# --------------------------------------------------------------------------
def ensure_hf_project(cfg: dict) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "assets").mkdir(exist_ok=True)
    ver = cfg.get("hyperframes_version", "0.7.65")
    (WORK / "package.json").write_text(json.dumps({
        "name": "alpaca-briefing", "private": True, "type": "module",
        "scripts": {"render": f"npx --yes hyperframes@{ver} render"},
    }, indent=2))
    (WORK / "hyperframes.json").write_text(json.dumps({
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
        "paths": {"blocks": "compositions", "components": "compositions/components",
                  "assets": "assets"},
        "media": {"autoProxy": True},
    }, indent=2))


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


# entrance motion verb -> GSAP from-vars (the card animates FROM these to rest).
_MOTION_FROM = {
    "slide-left": "{opacity:0,x:140}", "slide-right": "{opacity:0,x:-140}",
    "rise": "{opacity:0,y:80}", "pop": "{opacity:0,scale:0.6}",
    "fade": "{opacity:0}", "zoom": "{opacity:0,scale:1.28}",
}


def _callout_card(idx: int, eyebrow: str, value: str, accent: str, thumb_rel: str | None,
                  dur: float, glassy: bool = False, icon: str | None = None,
                  sub: str = "", canvas: bool = False) -> str:
    # canvas layout: left column under the title block; legacy: right stack.
    if canvas:
        top, anchor, width = 384 + idx * 150, "left:76px", 600
    else:                          # tighter stack that clears the subtitle band
        top, anchor, width = 296 + idx * 170, "right:120px", 560
    eyebrow_html = ("" if not eyebrow else
        f'<div style="font-size:19px;letter-spacing:.26em;color:#8f9bb0;text-transform:uppercase;font-weight:700">{_esc(eyebrow)}</div>')
    # glassy: the host-scene variant. The difference matte reproduces CSS alpha
    # exactly, so transparency declared here IS the transparency in the composite —
    # the presenter shows through the cards instead of disappearing behind them.
    card_bg = "rgba(18,22,31,.45)" if glassy else "rgba(18,22,31,.82)"
    card_border = "rgba(43,50,66,.55)" if glassy else "#2b3242"
    thumb_base = "rgba(18,22,31,.5)" if glassy else "#12161f"
    if icon:
        # production slide convention: the emoji IS the artwork — no SD thumb.
        lead = f'<span style="font-size:56px;line-height:1;flex:0 0 auto">{icon}</span>'
    elif thumb_rel:
        lead = (f'<div style="width:108px;height:108px;border-radius:13px;flex:0 0 auto;'
                f'background:url({thumb_rel}) center/cover,{thumb_base};filter:brightness(1.28) saturate(1.15);'
                f'border:1px solid {accent}66"></div>')
    else:
        lead = (f'<div style="width:108px;height:108px;border-radius:13px;flex:0 0 auto;'
                f'background:linear-gradient(135deg,{accent}44,{thumb_base});border:1px solid {accent}66"></div>')
    vfs = 40 if len(value) <= 26 else 31
    sub_html = ("" if not sub else
        f'<div style="font-size:22px;color:#b9c2d4;line-height:1.25;margin-top:5px;'
        f'overflow:hidden">{_esc(sub)}</div>')
    return (
        f'<div class="card clip" data-start="0" data-duration="{dur:.2f}" data-track-index="2" '
        f'id="card{idx}" style="position:absolute;top:{top}px;{anchor};width:{width}px;height:138px;'
        f'display:flex;gap:20px;align-items:center;padding:14px;border-radius:16px;transform-origin:center;'
        f'background:{card_bg};border:1px solid {card_border};backdrop-filter:blur(2px)">'
        f'{lead}'
        f'<div style="min-width:0">'
        f'{eyebrow_html}'
        f'<div style="font-size:{vfs}px;font-weight:800;color:{accent};line-height:1.06;margin-top:4px">{_esc(value)}</div>'
        f'{sub_html}'
        f'</div></div>')


# The size every slide is AUTHORED at. Every offset, font size and max-width below
# is a literal px against this, so it is a design unit, not an output resolution —
# rendering at a different size scales the whole stage rather than re-laying it out.
DESIGN_W, DESIGN_H = 1920, 1080


def build_composition(scene: dict, audio_rel: str, imgs: dict, duration: float,
                      cfg: dict, date: str, size: tuple[int, int] | None = None) -> str:
    # `size` overrides the configured slide raster (host mode passes the host
    # canvas). Text is vector until it is rasterised, so rendering the page at the
    # canvas size keeps it crisp; letting compose_scene's `scale=W` stretch a
    # 1920-wide raster up to 2560 would soften every glyph, which is exactly what
    # the 1920x1056 canvas was chosen to avoid in the first place.
    w, h = size or (cfg.get("width", DESIGN_W), cfg.get("height", DESIGN_H))
    accent = BIAS_COLOR.get(scene["bias"], BIAS_COLOR[None])[0]
    sym = scene["symbol"]
    label = "MARKET BRIEFING" if sym == "OVERVIEW" else sym
    bias_txt = "" if sym == "OVERVIEW" else f"BIAS · {(scene['bias'] or '—').upper()}"
    # THE HERO. On OVERVIEW the title block is the biggest thing on the slide and
    # it renders at frame 0 with no entry animation, which makes it the only
    # element that is fully on screen for the whole three-second hook. It was
    # spending that on the words "MARKET BRIEFING"; it now spends it on his own
    # number. The date eyebrow above it still identifies the slide, and the BIAS
    # slot below is empty on OVERVIEW, so the subline costs nothing.
    hero = (imgs or {}).get("hero")
    tick_col = "#f4f6fb"
    if hero and sym == "OVERVIEW":
        label, bias_txt = hero["tick"], hero["sub"]
        # Sign-coloured, by the SAME rule the key-number pills use below — the
        # hero and its own pill are the same figure at two sizes, and a white
        # headline sitting above a green pill reads as two different numbers.
        tick_col = ("#2ecc71" if label.startswith("+")
                    else "#e5484d" if re.match(r"-[\d$]", label) else "#f4f6fb")
    directed = scene.get("director") or {}
    tagline = directed.get("title", "")

    bg_path = imgs.get("bg")
    if scene.get("bg_solid"):
        # HOST MODE: a flat chroma ground that compose_scene keys out, so the
        # typography floats on the host clip's wall instead of sitting in a
        # dark card.
        bg = (f'<div id="bg" class="clip" data-start="0" data-duration="{duration:.2f}" '
              f'data-track-index="0" style="position:absolute;inset:0;'
              f'background:{scene["bg_solid"]}"></div>')
    elif bg_path:
        bg = (f'<img id="bg" class="clip" data-start="0" data-duration="{duration:.2f}" '
              f'data-track-index="0" src="assets/{Path(bg_path).name}" '
              f'style="position:absolute;top:0;left:0;width:{w}px;height:{h}px;'
              f'object-fit:cover;filter:brightness(0.46) saturate(1.12);transform-origin:60% 40%"/>')
    else:
        bg = (f'<div id="bg" class="clip" data-start="0" data-duration="{duration:.2f}" '
              f'data-track-index="0" style="position:absolute;inset:0;'
              f'background:radial-gradient(1300px 850px at 68% 32%,{accent}22,#0b0e14 60%),'
              f'linear-gradient(120deg,#0b0e14,#12161f)"></div>')

    # animated cards. HOST MODE follows the production slide convention (the
    # bullets slideshow): emoji-led catalyst cards + a key-number stat grid from
    # the briefing itself. Everything else keeps the SD-thumb callout cards.
    cards, card_anim = "", ""
    host = bool(scene.get("bg_solid"))
    canvas = host and (cfg.get("host_pip", {}) or {}).get("layout", "canvas") == "canvas"
    cats = (imgs.get("cats") or [])[:3] if host else []
    if cats:
        n_cards = len(cats)
        for i, c in enumerate(cats):
            cards += _callout_card(i, "", c["label"][:60], accent, None, duration,
                                   glassy=True, icon=c["icon"], sub=c["detail"][:80],
                                   canvas=canvas)
            at = 1.3 + i * 0.4
            xin = -140 if canvas else 140      # canvas cards enter from the left
            card_anim += (f'tl.from("#card{i}",{{opacity:0,x:{xin},duration:0.8,ease:"power3.out"}},{at});'
                          f'tl.to("#card{i}",{{y:"-=12",duration:{duration:.2f},ease:"sine.inOut",yoyo:true,repeat:1}},{at});')
    else:
        n_cards = len(imgs.get("elements", [])[:3])
        for i, e in enumerate(imgs.get("elements", [])[:3]):
            th = e.get("thumb")
            th_rel = f"assets/{Path(th).name}" if th else None
            # if the model left `value` empty, promote the label to the big line.
            value = e["value"] or e["label"]
            eyebrow = e["label"] if e["value"] else ""
            cards += _callout_card(i, eyebrow, value, accent, th_rel, duration,
                                   glassy=host)
            frm = _MOTION_FROM.get(e.get("motion", "fade"), _MOTION_FROM["fade"])
            at = 1.3 + i * 0.4
            card_anim += (f'tl.from("#card{i}",{{...{frm},duration:0.8,ease:"power3.out"}},{at});'
                          f'tl.to("#card{i}",{{y:"-=12",duration:{duration:.2f},ease:"sine.inOut",yoyo:true,repeat:1}},{at});')

    # key-number pills (host only): every production slide lists its numbers.
    # canvas layout: a single column beside the cards; legacy: 2x2 under them.
    for j, dstat in enumerate((imgs.get("inds") or [])[:4] if host else []):
        if canvas:
            top, pos, pw = 384 + j * 108, "left:716px", 300
        else:
            top = 296 + n_cards * 170 + 14 + (j // 2) * 108
            pos, pw = f"right:{408 if j % 2 == 0 else 120}px", 272
        v = dstat["value"]
        vcol = ("#2ecc71" if v.startswith("+")
                else "#e5484d" if re.match(r"-\d", v) else "#f4f6fb")
        # long values shrink instead of clipping ("semiconductors" at 30px hit
        # the pill edge) — same treatment the bullets slides give their grid
        ifs = 30 if len(v) <= 12 else 24 if len(v) <= 20 else 19
        cards += (
            f'<div class="clip" data-start="0" data-duration="{duration:.2f}" data-track-index="2" '
            f'id="ind{j}" style="position:absolute;top:{top}px;{pos};width:{pw}px;'
            f'padding:14px 18px;border-radius:14px;background:rgba(18,22,31,.45);'
            f'border:1px solid rgba(43,50,66,.55)">'
            f'<div style="font-size:15px;letter-spacing:.14em;color:#93a0b4;text-transform:uppercase;'
            f'font-weight:700;white-space:nowrap;overflow:hidden">{dstat["icon"]} {_esc(dstat["label"][:20])}</div>'
            f'<div style="font-size:{ifs}px;font-weight:800;color:{vcol};margin-top:4px;line-height:1.08;'
            f'white-space:nowrap;overflow:hidden">{_esc(v[:26])}</div></div>')
        card_anim += (f'tl.fromTo("#ind{j}",{{opacity:0,y:36,scale:0.88}},{{opacity:1,y:0,scale:1,'
                      f'duration:0.6,ease:"back.out(1.6)"}},{1.5 + j * 0.35:.2f});')

    # canvas layout: tighter left-anchored title block (the slide overlays the
    # full frame 1:1, so nothing may reach into the presenter's right third),
    # and NO full-frame gradient wash — it would darken the PRESENTER, not a
    # background.
    if canvas:
        g = {"eyebrow": "top:64px;left:80px", "tick": "top:104px;left:74px",
             "tick_fs": 118, "rule": "top:250px;left:80px",
             "bias": "top:268px;left:82px", "tagline": "top:316px;left:82px",
             "bias_fs": 34, "wash": ""}
    else:
        g = {"eyebrow": "top:78px;left:140px", "tick": "top:150px;left:134px",
             "tick_fs": 158, "rule": "top:322px;left:146px",
             "bias": "top:352px;left:150px", "tagline": "top:420px;left:150px",
             "bias_fs": 40, "wash": (f'<div class="clip" data-start="0" data-duration="{duration:.2f}" '
                      f'data-track-index="1" style="position:absolute;inset:0;'
                      f'background:linear-gradient(90deg,rgba(11,14,20,.9),'
                      f'rgba(11,14,20,.28) 55%,rgba(11,14,20,.12))"></div>')}
    tagline_html = ("" if not tagline else
        f'<div id="tagline" class="clip" data-start="0" data-duration="{duration:.2f}" data-track-index="2" '
        f'style="position:absolute;{g["tagline"]};font-size:34px;font-style:italic;font-weight:500;'
        f'color:#cbd5e1;max-width:900px">{_esc(tagline)}</div>')
    tagline_anim = "" if not tagline else f'tl.from("#tagline",{{opacity:0,y:20,duration:0.7}},1.1);'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width={w}, height={h}"/>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{w}px;height:{h}px;overflow:hidden;background:#0b0e14;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}}</style>
</head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{duration:.2f}"
     data-width="{w}" data-height="{h}">
 <div id="stage" style="position:absolute;top:0;left:0;
      width:{DESIGN_W}px;height:{DESIGN_H}px;
      transform:scale({w / DESIGN_W:.6f});transform-origin:0 0">
  {bg}
  {g["wash"]}
  <div id="eyebrow" class="clip" data-start="0" data-duration="{duration:.2f}" data-track-index="2"
       style="position:absolute;{g["eyebrow"]};font-size:23px;letter-spacing:.4em;
       color:#7c889c;text-transform:uppercase;font-weight:700">Daily Research · {date}</div>
  <div id="rule" class="clip" data-start="0" data-duration="{duration:.2f}" data-track-index="2"
       style="position:absolute;{g["rule"]};width:120px;height:5px;background:{accent};border-radius:3px"></div>
  <div id="tick" class="clip" data-start="0" data-duration="{duration:.2f}" data-track-index="2"
       style="position:absolute;{g["tick"]};font-size:{g["tick_fs"]}px;font-weight:800;
       color:{tick_col};letter-spacing:1px">{_esc(label)}</div>
  <div id="bias" class="clip" data-start="0" data-duration="{duration:.2f}" data-track-index="2"
       style="position:absolute;{g["bias"]};font-size:{g["bias_fs"]}px;font-weight:700;letter-spacing:.06em;
       color:{accent}">{_esc(bias_txt)}</div>
  {tagline_html}
  {cards}
 </div>
  <audio src="{audio_rel}" data-start="0"></audio>
</div>
<script>
window.__timelines=window.__timelines||{{}};
const tl=gsap.timeline({{paused:true}});
tl.to("#bg",{{scale:1.09,duration:{duration:.2f},ease:"none"}},0);           // ambient Ken Burns
tl.from("#eyebrow",{{opacity:0,y:-24,duration:0.8}},0);
tl.from("#tick",{{opacity:0,x:-90,duration:1.0,ease:"power3.out"}},0.15);
tl.from("#rule",{{scaleX:0,transformOrigin:"left center",duration:0.7}},0.7);
tl.from("#bias",{{opacity:0,x:-50,duration:0.8}},0.85);
{tagline_anim}
{card_anim}
window.__timelines["main"]=tl;
</script></body></html>"""


def render_clip(scene: dict, audio: Path, srt: Path | None, imgs: dict, duration: float,
                cfg: dict, date: str, dest: Path, pip: Path | None = None) -> None:
    assets = WORK / "assets"
    for f in assets.glob("*"):
        f.unlink()
    a_dst = assets / f"audio_{_safe(scene['symbol'])}.mp3"
    shutil.copy(audio, a_dst)
    # stage bg + callout thumbnails into assets/
    if imgs.get("bg"):
        shutil.copy(imgs["bg"], assets / Path(imgs["bg"]).name)
    for th in imgs.get("callouts", {}).values():
        if th:
            shutil.copy(th, assets / Path(th).name)

    # HOST MODE ONLY: rasterise the slide at the host canvas so typography is
    # native at the ship resolution. Scoped to `pip is not None` on purpose — the
    # three daily slideshows share this function and must keep their own size.
    slide_size = None
    _canvas = (cfg.get("host_pip") or {}).get("canvas")
    if pip is not None and _canvas:
        _cw = int(_canvas[0])
        slide_size = (_cw, round(_cw * DESIGN_H / DESIGN_W / 2) * 2)

    def _hf_render(tag: str) -> Path:
        html = build_composition(scene, f"assets/{a_dst.name}", imgs, duration, cfg,
                                 date, size=slide_size)
        (WORK / "index.html").write_text(html)
        renders = WORK / "renders"
        if renders.exists():
            shutil.rmtree(renders)
        subprocess.run(["npm", "run", "render"], cwd=WORK, check=True,
                       capture_output=True, text=True)
        mp4s = sorted(renders.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if not mp4s:
            raise RuntimeError(f"hyperframes produced no MP4 for {scene['symbol']}")
        # OUTSIDE renders/, which the next _hf_render call wipes — the black
        # render died in exactly that rmtree on the first live run
        keep = WORK / f"hf_{tag}.mp4"
        shutil.move(mp4s[-1], keep)
        return keep

    if pip is not None:
        # ONE scene, difference-matted: the same hyperframes composition rendered
        # on BLACK and on WHITE grounds (deterministic, so they align exactly),
        # matted onto the host clip's wall by compose_scene. Composite first,
        # burn subtitles after, so captions sit on top of the finished scene.
        from .host_briefing import compose_scene
        scene["bg_solid"] = "#000000"
        raw_b = _hf_render("black")
        scene["bg_solid"] = "#ffffff"
        raw_w = _hf_render("white")
        piped = raw_b.with_name(raw_b.stem + "_scene.mp4")
        compose_scene(raw_b, raw_w, pip, scene.get("host_keyframe"), piped, cfg)
        raw = piped
    else:
        raw = _hf_render("plain")

    if cfg.get("subtitles", True) and srt_has_cues(srt):
        burn_subtitles(raw, srt, dest, cfg)
    else:
        shutil.copy(raw, dest)


def srt_has_cues(srt: Path | None) -> bool:
    """True only when ffmpeg's subtitles filter can actually open the file: at
    least one timed cue. The old guard everywhere was `st_size > 0`, and on
    2026-08-02 a 1-byte .srt (a single newline — see _rechunk_cjk_srt) sailed
    through it; libass then failed with "Unable to open", ffmpeg exited 183, and
    that one cue-less Chinese subtitle threw away a finished 24-shot Wan render.
    Bytes are not cues; look for the arrow."""
    try:
        return bool(srt) and srt.exists() and "-->" in srt.read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return False


def burn_subtitles(clip_in: Path, srt: Path, clip_out: Path, cfg: dict) -> None:
    """Burn word-timed subtitles into a clip. ffmpeg's subtitles filter is fussy
    about paths, so we run it from WORK with plain relative filenames."""
    tmp_srt = WORK / "sub.srt"
    shutil.copy(srt, tmp_srt)
    fs = int(cfg.get("subtitle_fontsize", 11))
    mv = int(cfg.get("subtitle_margin_v", 14))
    style = (f"Fontsize={fs},PrimaryColour=&H00F4F6FB,OutlineColour=&HC0000000,"
             f"BorderStyle=1,Outline=2,Shadow=1,MarginV={mv},Alignment=2,Bold=1")
    out_tmp = WORK / "with_subs.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(clip_in.resolve()),
         "-vf", f"subtitles=sub.srt:force_style='{style}'",
         "-c:a", "copy", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_tmp.name)],
        cwd=WORK, check=True, capture_output=True, text=True)
    shutil.copy(out_tmp, clip_out)


def _clip_format(p: Path) -> tuple[int, int, str]:
    """(width, height, r_frame_rate) of the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(p)],
        capture_output=True, text=True).stdout.strip().split(",")
    return int(out[0]), int(out[1]), out[2]


def normalize_clip(src: Path, dest: Path, w: int, h: int, fps: str) -> Path:
    """Conform a clip to (w, h, fps): scale-to-cover, centre-crop, resample.

    Mixed formats must never reach a concat: xfade refuses them (the configured
    fades silently degrade to hard cuts) and copy-concat shifts the odd clip's
    video timestamps by the timebase ratio — the viewer sees the previous
    scene's last frame frozen for seconds while the next scene's audio plays.
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src),
         "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=increase:"
                 f"flags=lanczos,crop={w}:{h},fps={fps},setsar=1"),
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "copy", str(dest)], check=True, capture_output=True, text=True)
    return dest


def loudnorm(src: Path, dest: Path, cfg: dict) -> bool:
    """One EBU R128 pass over the FINISHED video. Returns True if it ran.

    Applied at assembly, never per clip: loudnorm is not idempotent, so per-clip
    normalisation would both compound on a re-concat and flatten the deliberate
    level differences between a quiet aside and an emphatic line.

    Two-pass. Single-pass loudnorm is a dynamic compressor working blind, and on
    speech with pauses it pumps; measuring first turns it into one static gain plus
    a limiter, which is what we actually want here — the narration's own dynamics
    are the performance. Video is copied, so this costs seconds and touches no
    pixels; a failure leaves the un-normalised file in place rather than none.
    """
    lc = cfg.get("audio_loudness") or {}
    if not lc.get("enabled"):
        return False
    I, TP, LRA = float(lc.get("i", -16)), float(lc.get("tp", -1.5)), float(lc.get("lra", 11))
    base = f"loudnorm=I={I}:TP={TP}:LRA={LRA}"
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
                            "-af", base + ":print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True)
        blob = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
        m = json.loads(blob.group(0)) if blob else {}
        second = (f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                  f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                  f":offset={m['target_offset']}:linear=true:print_format=summary")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src),
                        "-af", second, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        str(dest)], check=True, capture_output=True, text=True)
        print(f"  [loudness] {float(m['input_i']):+.1f} -> {I:+.1f} LUFS "
              f"(true peak {float(m['input_tp']):+.1f} -> {TP:+.1f} dBTP)")
        return True
    except Exception as exc:                                # noqa: BLE001
        print(f"  [loudness] FAILED ({type(exc).__name__}: {exc}) — shipping raw level")
        return False


def concat_clips(clips: list[Path], dest: Path, cfg: dict | None = None) -> None:
    try:
        fmts = [_clip_format(c) for c in clips]
    except Exception:  # noqa: BLE001 — probe failure: concat as before
        fmts = []
    if fmts and len(set(fmts)) > 1:
        w, h, fps = fmts[0]
        print(f"  [concat] MIXED formats {sorted(set(fmts))} — conforming "
              f"outliers to {w}x{h}@{fps}")
        conformed = []
        for i, (c, f) in enumerate(zip(clips, fmts)):
            if f == fmts[0]:
                conformed.append(c)
            else:
                conformed.append(normalize_clip(c, WORK / f"norm_{i}_{c.name}",
                                                w, h, fps))
        clips = conformed
    listf = WORK / "concat.txt"
    # ABSOLUTE paths, always: ffmpeg resolves relative entries against the concat
    # file's own directory (WORK), not the cwd. Production never noticed because
    # its default out-dir is absolute; the first relative --out-dir died here.
    listf.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in clips))
    base = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listf)]
    # normalise into `dest` from a temp joint, so a loudnorm failure still leaves a
    # complete video at the expected path
    joint = (WORK / f"joint_{dest.name}") if (cfg or {}).get("audio_loudness",
                                                             {}).get("enabled") else dest
    try:
        subprocess.run(base + ["-c", "copy", str(joint)], check=True,
                       capture_output=True, text=True)
    except subprocess.CalledProcessError:
        subprocess.run(base + ["-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
                               str(joint)], check=True, capture_output=True, text=True)
    if joint != dest and not loudnorm(joint, dest, cfg or {}):
        shutil.move(str(joint), str(dest))


def concat_with_transitions(clips: list[Path], dest: Path, transition: str = "fade",
                            dur: float = 0.4) -> None:
    """Stitch clips with a smooth cross-dissolve between each (video ``xfade`` +
    audio ``acrossfade``), instead of a hard cut. Keeps A/V in sync because both
    filters shorten the timeline by the same overlap. Falls back to a plain concat
    on any failure or when a transition can't fit (a clip shorter than the fade)."""
    clips = [c for c in clips if c and c.exists()]
    if len(clips) <= 1:
        if clips:
            shutil.copy(clips[0], dest)
        return
    try:
        durs = [_duration(c) for c in clips]
    except Exception:  # noqa: BLE001
        return concat_clips(clips, dest)
    d = max(0.12, min(dur, min(durs) * 0.4))       # fade must fit the shortest clip
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    # xfade hard-requires equal size AND fps across inputs; a mixed set (the
    # 1080p30 desk slide among 704p24 host scenes) made it fail and silently
    # degrade to hard cuts. Conform every input in-graph — this path re-encodes
    # anyway, so a matching clip loses nothing.
    try:
        w0, h0, f0 = _clip_format(clips[0])
    except Exception:  # noqa: BLE001
        return concat_clips(clips, dest)
    vf, af, voff = [], [], 0.0
    for i in range(len(clips)):
        vf.append(f"[{i}:v]scale={w0}:{h0}:force_original_aspect_ratio=increase:"
                  f"flags=lanczos,crop={w0}:{h0},fps={f0},setsar=1[nv{i}]")
    vprev, aprev = "[nv0]", "[0:a]"
    for i in range(1, len(clips)):
        voff += durs[i - 1] - d                     # start the fade d before the running end
        vlab, alab = f"[v{i}]", f"[a{i}]"
        vf.append(f"{vprev}[nv{i}]xfade=transition={transition}:duration={d:.3f}:offset={voff:.3f}{vlab}")
        af.append(f"{aprev}[{i}:a]acrossfade=d={d:.3f}{alab}")
        vprev, aprev = vlab, alab
    fc = ";".join(vf + af)
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", fc,
             "-map", vprev, "-map", aprev, "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", str(dest)],
            check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        print(f"  [concat] xfade failed ({exc.stderr[-200:] if exc.stderr else exc}); hard-cut fallback")
        concat_clips(clips, dest)


def stitch(clips: list[Path], dest: Path, cfg: dict, scope: str = "scene") -> None:
    """Concat helper that honours the configured transition. ``scope`` picks the
    fade length: 'beat' (short, between shots inside a scene) or 'scene' (between
    per-symbol scenes). transition='cut' (or 0 duration) = a plain hard concat."""
    s = cfg.get("story", {})
    trans = s.get("transition", "cut")
    base = float(s.get("transition_s", 0.4))
    dur = base * (0.55 if scope == "beat" else 1.0)
    if not trans or trans == "cut" or dur <= 0:
        return concat_clips(clips, dest)
    concat_with_transitions(clips, dest, transition=trans, dur=dur)


def briefing_for(dec: dict, sym: str) -> dict | None:
    """The optional richer VIDEO briefing entry for a symbol ('_' = overview).
    Returns {story, catalysts[], indicators[]} or None. Trade-path files that
    never set `briefing` simply yield None (renderers fall back to `notes`)."""
    b = dec.get("briefing")
    if not isinstance(b, dict):
        return None
    key = "_" if sym in ("_", "OVERVIEW") else sym
    entry = b.get(key) or b.get(sym.split("/")[0])
    return entry if isinstance(entry, dict) else None


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


# --------------------------------------------------------------------------
# cover sheet
# --------------------------------------------------------------------------
_LATIN_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
# NOT under truetype/dejavu — the Noto CJK faces ship as OpenType collections in
# their own directory. Naming the wrong dir used to work only because Pillow
# silently falls back to searching the XDG font dirs by BASENAME, so the Chinese
# covers were one Pillow change away from rendering tofu.
_CJK_FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")

# Framing for the RECAP poster's klein plate. The subject must sit high because
# make_cover_recap lays its type into a dark scrim across the bottom third; an
# earlier version asked for empty space at the top instead and put the whole scene
# underneath the headline.
_RECAP_FRAMING = (", one wide cinematic scene CENTRED and filling the frame edge to edge, "
                  "the main subject large in the UPPER TWO THIRDS, the bottom third calm "
                  "and uncluttered.")


def _cover_font(text: str, size: int):
    """Bold display font; CJK-capable fallback when the string needs it."""
    from PIL import ImageFont
    path = _CJK_FONT if re.search(r"[一-鿿]", text) else _LATIN_FONT
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:      # distro moved it — let Pillow search the font dirs
        return ImageFont.truetype(path.name, size)


# Chinese line-breaking (禁则) forbids a line STARTING with closing punctuation.
# When a break would land there, keep the character on the line it closes.
_NO_LINE_START = "，。、；：？！）］｝》〉」』”’…—%"


def _wrap_text(draw, text: str, font, maxw: float) -> list[str]:
    """Greedy wrap to `maxw`, per word for Latin and per character for CJK.

    Two things the naive per-character split gets wrong on these headlines, which
    are always a mix: runs of Latin/digits have to stay ATOMIC or a ticker snaps
    across lines ("AI" as "A" then "I", "Nvidia" as "Nvid|ia"), and the spaces
    around them are real — dropping them glues "Nvidia 暴跌 4.5%" into one word.
    So keep spaces as tokens while accumulating and trim only at line ends.
    """
    if re.search(r"[一-鿿]", text):
        toks = re.findall(r"[0-9A-Za-z][0-9A-Za-z.'%&+-]*|.", text)
        sep = ""
    else:
        toks = text.split()
        sep = " "
    lines, cur = [], ""
    for w in toks:
        t = f"{cur}{sep}{w}" if cur else w
        if (draw.textlength(t, font=font) > maxw and cur.strip()
                and w not in _NO_LINE_START):
            lines.append(cur.strip())
            cur = w.lstrip()
        else:
            cur = t
    if cur.strip():
        lines.append(cur.strip())
    return lines


def _scrim(w: int, h: int, top_frac: float = 0.42, max_alpha: int = 250,
           ease: float = 0.8):
    """A bottom-up dark gradient to lay type over artwork.

    The slideshow sheet drops its words on an opaque cream panel that hides most of
    the frame. The recap poster keeps the art visible the whole way across and
    darkens only the band it needs — which is most of what makes the two read as
    different tiers at thumbnail size, before any of the type is legible.

    Two settings here were tuned against real renders rather than guessed:

    `ease` below 1 darkens FAST then levels off, the opposite of the obvious choice.
    The crayon world is near-white (#faf6ee paper), so a gentle ease-in left only
    ~60/255 of cover under the headline and the white type disintegrated against pale
    paper.

    `top_frac` starts the ramp low, at 42%. An earlier 28% reached deep into the
    middle of the frame and turned the artwork — the only reason to look at a
    thumbnail — into grey mud. The art plate is prompted to keep its subject in the
    upper two thirds precisely so this band can stay dark without covering anything.
    """
    from PIL import Image
    grad = Image.new("L", (1, h), 0)
    px = grad.load()
    y0 = int(h * top_frac)
    for y in range(h):
        t = 0.0 if y <= y0 else (y - y0) / max(1, h - 1 - y0)
        px[0, y] = int(max_alpha * (t ** ease))
    layer = Image.new("RGBA", (w, h), (12, 16, 26, 255))
    layer.putalpha(grad.resize((w, h)))
    return layer


def _tracked(d, xy, text: str, font, fill, track: int = 0,
             stroke_width: int = 0, stroke_fill=None):
    """Draw `text` with extra letter-spacing; returns the width used.

    PIL has no tracking and an eyebrow line in plain caps reads as an accident
    rather than a design, so step the glyphs by hand. The stroke matters as much as
    the spacing: the eyebrow sits high enough that the scrim is still thin there, so
    without an outline it vanishes wherever the artwork happens to be busy.
    """
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill,
               stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += d.textlength(ch, font=font) + track
    return x - xy[0] - (track if text else 0)


def make_cover_recap(head: dict, label: str | None, date: str, art: Path | None,
                     dest: Path, cfg: dict) -> None:
    """The flagship recap's poster.

    The three daily slideshows and the recap were publishing the same cover design,
    so in a subscriptions feed or a channel grid the flagship looked like a fourth
    slideshow. This composes the opposite way round: klein's art runs FULL-BLEED,
    a dark cinematic scrim rises from the bottom, and the type sits in it —
    letterspaced amber eyebrow naming the tier, big white headline, and the day's
    keywords as pills that quote the video's own HUD.

    The palette is lifted from `story._beat_overlay_png` (amber #f2a93b on the
    near-black #0c101a pill) on purpose: the poster should look like a frame of the
    thing it advertises, which the cream slideshow sheet never did.

    NOTE the art plate must be composed for the FULL frame: main() prompts it
    centred and high (_RECAP_FRAMING); `art=None` degrades to a flat ground
    rather than shipping a lopsided poster.
    """
    from PIL import Image, ImageDraw
    W, H = 1920, 1080
    AMBER, INK, PILL = "#f2a93b", "#0c101a", (12, 16, 26, 208)

    if art and art.exists():
        im = Image.open(art).convert("RGB")
        s = max(W / im.width, H / im.height)
        im = im.resize((round(im.width * s), round(im.height * s)))
        img = im.crop(((im.width - W) // 2, (im.height - H) // 2,
                       (im.width - W) // 2 + W, (im.height - H) // 2 + H)).convert("RGBA")
    else:
        img = Image.new("RGBA", (W, H), (250, 246, 238, 255))

    img = Image.alpha_composite(img, _scrim(W, H))
    d = ImageDraw.Draw(img)
    x0, maxw = 96, W - 2 * 96

    # --- keyword pills along the bottom, laid out first so the type stacks up from
    #     a known baseline instead of guessing at the headline's height.
    kws = [k.upper() for k in (head.get("keywords") or [])[:3] if k]
    chip_h, chip_y = 60, H - 128
    if kws:
        kf = _cover_font(" ".join(kws) or "X", 30)
        x = x0
        for kw in kws:
            tw = d.textlength(kw, font=kf)
            if x + tw + 44 > x0 + maxw:
                break
            d.rounded_rectangle([x, chip_y, x + tw + 40, chip_y + chip_h],
                                radius=chip_h // 2, fill=PILL)
            d.text((x + 20, chip_y + (chip_h - 38) // 2), kw, font=kf, fill=AMBER)
            x += tw + 40 + 16

    # --- headline, wrapped, growing upward from just above the pills
    title = (head.get("title") or "").strip()
    size = 88
    while True:
        tf = _cover_font(title, size)
        lines = _wrap_text(d, title, tf, maxw)[:3]
        lh = round(size * 1.16)
        if len(lines) * lh <= 380 or size <= 52:
            break
        size -= 6
    title_top = chip_y - 46 - len(lines) * lh
    ty = title_top
    for ln in lines:
        d.text((x0, ty), ln, font=tf, fill="#ffffff",
               stroke_width=3, stroke_fill=INK)
        ty += lh

    # --- eyebrow: the tier, spelled out. This is the line that tells a viewer in
    #     the channel grid that they are looking at the recap and not a briefing.
    eb = " · ".join(x for x in [(label or "").upper(), date] if x)
    ef = _cover_font(eb, 34)
    ey = max(24, title_top - 78)
    d.line([x0, ey - 22, x0 + 132, ey - 22], fill=AMBER, width=6)
    _tracked(d, (x0, ey), eb, ef, AMBER, track=4, stroke_width=3, stroke_fill=INK)

    img.convert("RGB").save(dest, "PNG")


class _CoverDone(Exception):
    """Control-flow sentinel: the host poster path finished; skip klein art."""


def make_cover_host(head: dict, label: str | None, date: str, plate: Path | None,
                    dest: Path, cfg: dict, pnl: dict | None = None) -> None:
    """The host-era daily poster: the presenter on the right, bold headline left.

    Same dark-cinematic family as make_cover_recap (scrim, amber eyebrow, white
    headline, keyword pills) so the daily video still reads as the flagship — but
    the art is the day's keyframe, not crayon. A portrait plate cannot go
    full-bleed on a 16:9 canvas, so it stands full-height on the RIGHT and the
    left is extended from the plate's own edge colours (the keyframe pad trick):
    one continuous studio wall, headline in the clear half, nothing fighting the
    presenter.

    make_cover_recap stays untouched — the weekend recap still ships that
    design.
    """
    from PIL import Image, ImageDraw
    import numpy as np
    W, H = 1920, 1080
    AMBER, INK, PILL = "#f2a93b", "#0c101a", (12, 16, 26, 208)

    if plate and plate.exists() and Image.open(plate).width / Image.open(plate).height > 1.4:
        # staged 16:9 keyframe (the presenter already stands right on the real
        # wall): FULL-BLEED — scale to cover, crop the overflow off the LEFT so
        # the presenter stays anchored right and the headline gets genuine
        # textured wall instead of a median-colour extension.
        im = Image.open(plate).convert("RGB")
        s = H / im.height
        im = im.resize((round(im.width * s), H))
        x_off = max(0, im.width - W)
        img = im.crop((x_off, 0, x_off + W, H)).convert("RGBA")
    elif plate and plate.exists():
        im = Image.open(plate).convert("RGB")
        pw = round(im.width * H / im.height)
        im = im.resize((pw, H))
        arr = np.asarray(im).astype(float)
        left_col = np.median(arr[:, :48], axis=1)          # (H,3) per-row wall colour
        canvas = np.zeros((H, W, 3), np.uint8)
        canvas[:, W - pw:] = np.asarray(im)
        canvas[:, :W - pw] = np.repeat(left_col[:, None, :], W - pw,
                                       axis=1).astype(np.uint8)
        img = Image.fromarray(canvas).convert("RGBA")
        seam = W - pw
        blur = img.crop((max(0, seam - 30), 0, min(W, seam + 30), H)) \
                  .resize((60, H)).resize((60, H))
        img.paste(blur, (max(0, seam - 30), 0))
    else:
        img = Image.new("RGBA", (W, H), (236, 230, 222, 255))

    img = Image.alpha_composite(img, _scrim(W, H, top_frac=0.30))
    d = ImageDraw.Draw(img)
    x0, maxw = 96, 1000                                    # type stays out of the presenter's half

    kws = [k.upper() for k in (head.get("keywords") or [])[:3] if k]
    chip_h, chip_y = 60, H - 128
    if kws:
        kf = _cover_font(" ".join(kws) or "X", 30)
        x = x0
        for kw in kws:
            tw = d.textlength(kw, font=kf)
            if x + tw + 44 > x0 + maxw:
                break
            d.rounded_rectangle([x, chip_y, x + tw + 40, chip_y + chip_h],
                                radius=chip_h // 2, fill=PILL)
            d.text((x + 20, chip_y + (chip_h - 38) // 2), kw, font=kf, fill=AMBER)
            x += tw + 40 + 16

    # BOLDER than the recap's: starts 108pt against 88, floor 60 against 52 —
    # the user asked for a bold headline, and with a clean wall behind it there
    # is nothing to collide with.
    title = (head.get("title") or "").strip()
    size = 108
    while True:
        tf = _cover_font(title, size)
        lines = _wrap_text(d, title, tf, maxw)
        lh = round(size * 1.14)
        # shrink until the WHOLE title fits in three lines — a dangling clause
        # ("— I almost") is worse than a smaller font, so truncation is the last
        # resort at the floor size, not the default
        if (len(lines) <= 3 and len(lines) * lh <= 470) or size <= 60:
            lines = lines[:3]
            break
        size -= 6
    title_top = chip_y - 46 - len(lines) * lh
    ty = title_top
    for ln in lines:
        d.text((x0, ty), ln, font=tf, fill="#ffffff",
               stroke_width=4, stroke_fill=INK)
        ty += lh

    eb = " · ".join(x for x in [(label or "").upper(), date] if x)
    ef = _cover_font(eb, 34)
    ey = max(24, title_top - 78)
    d.line([x0, ey - 22, x0 + 132, ey - 22], fill=AMBER, width=6)
    _tracked(d, (x0, ey), eb, ef, AMBER, track=4, stroke_width=3, stroke_fill=INK)

    # --- the presenter's day P&L, top-left. The presenter's OWN account is the
    # whole premise of the host era, so the poster leads with how that account's
    # day went — green pill up, red pill down, before the viewer reads a word of
    # the headline. Omitted silently when the desk digest is unavailable (a wrong
    # or stale number is worse than none).
    if pnl and pnl.get("day_pl") is not None:
        v = float(pnl["day_pl"])
        pct = pnl.get("day_pl_pct")
        up = v >= 0
        txt = f"{'▲' if up else '▼'} {'+' if up else '−'}${abs(v):,.2f}"
        if pct is not None:
            txt += f" · {'+' if up else '−'}{abs(float(pct)):.2f}%"
        txt += "  TODAY"
        colour = "#59d99d" if up else "#ef6a5f"
        bf = _cover_font(txt, 40)
        tw = d.textlength(txt, font=bf)
        bx, by, bh = 56, 48, 76
        d.rounded_rectangle([bx, by, bx + tw + 56, by + bh],
                            radius=bh // 2, fill=PILL)
        d.text((bx + 28, by + (bh - 50) // 2), txt, font=bf, fill=colour,
               stroke_width=2, stroke_fill=INK)

    img.convert("RGB").save(dest, "PNG")


# --------------------------------------------------------------------------
# archive + history
# --------------------------------------------------------------------------
def briefing_kind(meta: dict) -> str:
    """A day can hold BOTH a fast slideshow and a flagship video; classify by the
    renderer so they archive side by side instead of clobbering each other."""
    return "slideshow" if meta.get("renderer") == "hyperframes" else "video"


def _story_scene_render(story_mod, dec: dict, cfg: dict, date: str, out_dir: Path,
                        stem: str):
    """Render the per-symbol scenes, honouring the wan→hyperframes fallback.

    Returns (renderer_used, briefing, clip_paths, clips_meta) — the first
    element records what ACTUALLY ran, which is what meta.json must say.

    The fallback exists for one case only: Wan could not render. A
    WanCompositingError means the opposite — every raw shot is on disk and only
    the cheap assembly failed — so it propagates. On 2026-08-02 the blanket
    fallback caught exactly that, threw away ~2h of finished GPU work, and
    published a slideshow in the flagship recap's slot."""
    renderer = cfg.get("story", {}).get("renderer", "hyperframes")
    if renderer == "wan":
        # The wan pipeline hosts two video engines; meta must say which one
        # actually made the shots (story.video_engine: "wan" | "h3").
        engine = cfg.get("story", {}).get("video_engine", "wan")
        try:
            briefing, clip_paths, clips_meta = story_mod.render_scenes_wan(
                dec, cfg, date, out_dir, stem)
            print(f"✅ story (scenes · {engine}) -> {briefing.name}")
            return engine, briefing, clip_paths, clips_meta
        except story_mod.WanCompositingError:
            raise       # raws are done; a fallback would silently discard them
        except Exception as exc:  # noqa: BLE001 — Wan is heavy; fall back to hyperframes
            print(f"  [wan] failed ({type(exc).__name__}: {exc}); falling back to hyperframes")
            briefing, clip_paths, clips_meta = story_mod.render_scenes(
                dec, cfg, date, out_dir, stem)
            print(f"✅ story (scenes · hyperframes) -> {briefing.name}")
            return "hyperframes", briefing, clip_paths, clips_meta
    briefing, clip_paths, clips_meta = story_mod.render_scenes(dec, cfg, date, out_dir, stem)
    print(f"✅ story (scenes) -> {briefing.name}")
    return renderer, briefing, clip_paths, clips_meta


def edition_stem(label: str | None, date: str) -> str:
    """The filename stem every file of ONE edition shares: `<label-slug>_<date>`.

    Everything used to be `briefing_<date>`, so the day's three slideshows and
    the recap all wrote the SAME filename into out_dir and overwrote each other
    there — only the archive kept them apart, because it files by slot. Reusing
    the label slug (what briefing_slot already keys on) means the file and its
    archive directory agree, and a downloaded mp4 says which edition it is.

    An unlabelled render — an ad-hoc `run_briefing.sh` with no --label — keeps
    the old `briefing_<date>` name.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return f"{slug}_{date}" if slug else f"briefing_{date}"


def briefing_slot(meta: dict) -> str:
    """The archive subdir for THIS render. The flagship video keeps the single
    'video' slot; slideshows get a slot per EDITION (label slug) so the day's
    open/midday/close runs coexist instead of the last one overwriting the rest."""
    kind = briefing_kind(meta)
    if kind != "slideshow":
        return kind
    slug = re.sub(r"[^a-z0-9]+", "-", (meta.get("label") or "").lower()).strip("-")
    return f"slideshow-{slug}" if slug else kind


def rebuild_history(archive_root: Path, out_dir: Path, current: str | None = None) -> None:
    """Rebuild history.json from the archive (the source of truth). One entry per
    (date, slot); every video is served via the /archive/<date>/<slot>/ route, so
    the dashboard only needs a correct history.json — no media files in out_dir."""
    briefings = []
    for d in (p.name for p in archive_root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        day = archive_root / d
        for kd in sorted(p.name for p in day.iterdir() if p.is_dir()):
            mp = day / kd / "meta.json"
            if not mp.exists():
                continue
            m = json.loads(mp.read_text())
            base = f"/archive/{d}/{kd}/"
            m["briefing"] = base + m["briefing_file"]
            if m.get("briefing_file_zh"):
                m["briefing_zh"] = base + m["briefing_file_zh"]
            if m.get("cover_file"):
                m["cover"] = base + m["cover_file"]
            if m.get("cover_file_zh"):
                m["cover_zh"] = base + m["cover_file_zh"]
            if m.get("cover_file_zh_bili"):
                m["cover_zh_bili"] = base + m["cover_file_zh_bili"]
            for c in m.get("clips", []):
                c["file"] = base + c["clip_file"]
                if c.get("clip_file_zh"):
                    c["file_zh"] = base + c["clip_file_zh"]
            m["kind"] = "slideshow" if kd.startswith("slideshow") else "video"
            m["slot"] = kd
            m["local"] = False
            briefings.append(m)
    # newest session first; within a date, newest render first.
    briefings.sort(key=lambda m: (m.get("date", ""), m.get("generated_at", "")), reverse=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "history.json").write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "current": current or (briefings[0]["date"] if briefings else None),
         "briefings": briefings}, indent=2))


def recent_titles(archive_root: Path, date: str, slot: str, n: int = 7) -> list[dict]:
    """The last `n` archived editions of `slot` dated STRICTLY before `date`,
    newest first, as {"date", "title", "title_zh"} (title_zh may be "").

    Feeds the headline's don't-repeat-yourself context. Strictly before, and by
    the directory's date rather than the file's mtime: an archive backfill
    re-renders an old date, and it must not see its own previous title (that
    would push it off its own story) or any later one. Cosmetic input, so it
    never raises — a missing, unreadable or half-written archive is just []."""
    out: list[dict] = []
    try:
        days = sorted((p for p in Path(archive_root).iterdir()
                       if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)
                       and p.name < date), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    for day in days:
        if len(out) >= n:
            break
        try:
            m = json.loads((day / slot / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        title = str(m.get("title") or "").strip() if isinstance(m, dict) else ""
        if title:
            out.append({"date": day.name, "title": title,
                        "title_zh": str(m.get("title_zh") or "").strip()})
    return out


def archive_and_index(date: str, briefing: Path, clip_paths: list[Path],
                      meta: dict, out_dir: Path, archive_root: Path) -> None:
    """Copy this render into the durable archive under <date>/<slot>/ (so a day's
    editions and the video coexist), then rebuild history.json from the archive."""
    slot = briefing_slot(meta)
    day_dir = archive_root / date / slot
    day_dir.mkdir(parents=True, exist_ok=True)
    for f in [briefing, *clip_paths]:
        shutil.copy(f, day_dir / f.name)
    zh = briefing.with_name(f"{briefing.stem}_zh.mp4")
    if meta.get("briefing_file_zh") and zh.exists():
        shutil.copy(zh, day_dir / zh.name)
    for key in ("cover_file", "cover_file_zh", "cover_file_zh_bili"):
        cov = briefing.parent / (meta.get(key) or "")
        if meta.get(key) and cov.exists():
            shutil.copy(cov, day_dir / cov.name)
    (day_dir / "meta.json").write_text(json.dumps(
        {**meta, "kind": briefing_kind(meta), "slot": slot}, indent=2))

    # out_dir is now just a staging area (the dashboard plays from /archive); keep
    # only the current date's full briefing there to avoid unbounded growth.
    for f in [*out_dir.glob("*.mp4"), *out_dir.glob("*.png")]:
        # clip_*.mp4 belongs to the CURRENT render and is archived from here —
        # pruning it was a real bug once. Editions are named <slug>_<date>.
        if f.name.startswith("clip_") or date in f.name:
            continue
        f.unlink()

    rebuild_history(archive_root, out_dir, current=date)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Render the daily research briefing to MP4")
    ap.add_argument("--symbols", help="comma-separated override of which symbols to render")
    ap.add_argument("--max-symbols", type=int)
    ap.add_argument("--no-images", action="store_true", help="skip SDXL; gradient bg, no callouts")
    ap.add_argument("--out-dir", help="override output dir (default web/static/media)")
    ap.add_argument("--archive-dir", help="override the durable archive root (tests: point at "
                    "a scratch dir so production history.json is untouched)")
    ap.add_argument("--renderer", choices=["wan", "hyperframes"],
                    help="override story renderer: 'wan' flagship video or fast 'hyperframes' slideshow")
    ap.add_argument("--decisions", help="read this decisions/recap JSON instead of decisions/decisions.json "
                    "(used by the post-market & weekend RECAP renders — never the live trade file)")
    ap.add_argument("--label", help="briefing kind shown on the dashboard card "
                    "(e.g. 'Intraday slideshow', 'Post-market recap', 'Weekend recap')")
    args = ap.parse_args()

    cfg = load_config()
    if args.max_symbols is not None:
        cfg["max_symbols"] = args.max_symbols
    if args.no_images:
        cfg["images_enabled"] = False
    if args.renderer:
        cfg.setdefault("story", {})["renderer"] = args.renderer
    override = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols
               else cfg.get("symbols"))

    dpath = Path(args.decisions) if args.decisions else DECISIONS_PATH
    if not dpath.exists():
        sys.exit(f"no decisions file at {dpath} — run the research step first")
    dec = json.loads(dpath.read_text())
    date = dec.get("date", datetime.now(timezone.utc).date().isoformat())

    out_dir = Path(args.out_dir) if args.out_dir else (ROOT / cfg.get("out_dir", "web/static/media"))
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_root = Path(args.archive_dir or cfg.get("archive_dir", str(ROOT / "briefing" / "archive")))
    ensure_hf_project(cfg)
    tmp = WORK / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    stem = edition_stem(args.label, date)
    briefing = out_dir / f"{stem}.mp4"
    briefing_zh = out_dir / f"{stem}_zh.mp4"
    if briefing_zh.exists():        # stale zh from an earlier run of the same date:
        briefing_zh.unlink()        # existence AFTER the render means it was made now.
    cover = out_dir / f"{stem}_cover.png"
    cover_zh = out_dir / f"{stem}_cover_zh.png"
    for c in (cover, cover_zh):     # same story for the cover sheets
        if c.exists():
            c.unlink()

    mode = cfg.get("mode", "story")
    # HOST MODE FORCES FACTUAL. The host-PiP product IS the slideshow: the
    # presenter's deck narration, the host clips, hyperframes slides. Leaving
    # mode=story here would let the story path win the race and the host gate
    # (inside the factual branch) would silently never fire — the E2E test
    # caught exactly that.
    if (bool(cfg.get("host", {}).get("enabled"))
            or os.environ.get("BRIEFING_HOST") == "1"):
        mode = "factual"
    clip_paths: list[Path] = []
    clips_meta: list[dict] = []
    used_images = used_director = False
    # What actually rendered the video — NOT the config's wish. The 2026-08-02
    # weekend recap was archived as renderer=wan when the wan run had fallen
    # back to the hyperframes slideshow; nothing downstream could tell.
    renderer_used = cfg.get("story", {}).get("renderer") if mode == "story" else None

    # --- STORY MODE: animated cartoon briefing (one scene per symbol) ---
    if mode == "story":
        from . import story as story_mod
        try:
            # per-symbol animated scenes: fixed character on a cartoon set with props.
            renderer_used, briefing, clip_paths, clips_meta = _story_scene_render(
                story_mod, dec, cfg, date, out_dir, stem)
            used_images = used_director = True
        except story_mod.WanCompositingError:
            raise       # finished raws on disk — see _story_scene_render
        except Exception as exc:  # noqa: BLE001 — fall back rather than fail the run
            print(f"  [story] failed ({type(exc).__name__}: {exc}); falling back to factual")
            mode, clips_meta, clip_paths = "factual", [], []

    # --- FACTUAL MODE: per-symbol scenes (also the story-mode fallback) --------
    if mode == "factual":
        scenes = select_scenes(dec, cfg, override)
        if not scenes:
            sys.exit("nothing to render (no notes in decisions.json)")
        print(f"briefing {date}: {len(scenes)} scene(s) -> {[s['symbol'] for s in scenes]}")

        host_on = (bool(cfg.get("host", {}).get("enabled"))
                   or os.environ.get("BRIEFING_HOST") == "1")
        if host_on:
            # HOST MODE: the presenter narrates the whole deck — OVERVIEW, the
            # live dashboard (DESK), then each symbol — in EN and 中文, one mood
            # per slide, standing in the slideshow. The DIRECTOR still runs, but
            # only for the slides' visual elements (titles, real callout
            # numbers); the narration is the presenter's.
            from . import host_briefing as HB
            from .ltx_story import _free_gpu as _host_free_gpu
            # Stale VRAM kills the FIRST stage, not the loud ones: a previous
            # run's SD3.5 + LTX residents left 30 MiB free and Qwen3-TTS OOMed on
            # a 20 MiB allocation. Every LTX render frees both servers before it
            # runs; the branch itself must too.
            _host_free_gpu()
            # WHICH PRESENTER: config host.host_json (BRIEFING_HOST_JSON
            # overrides one run); 陈谷雨/host2.json since 2026-08-12. There is
            # no default presenter — an unset key or a missing file stops here.
            hcfg = cfg.get("host", {})
            host_file = (os.environ.get("BRIEFING_HOST_JSON")
                         or hcfg.get("host_json"))
            if not host_file:
                raise SystemExit("briefing/config.json host.host_json is not set "
                                 "— no presenter")
            host_path = Path(__file__).parent / host_file
            if not host_path.exists():
                raise SystemExit(f"presenter file missing: {host_path} "
                                 f"(config host.host_json = {host_file})")
            hostj = json.loads(host_path.read_text())
            print(f"  [host] presenter: {hostj.get('name','?')} "
                  f"({hostj.get('name_en','?')}) — {host_file}")
            # EVERY host-dependent call below must be handed one of these two, and
            # the failure is silent when it is not: in the first 陈谷雨 rehearsal
            # the deck came out in HIS words (write_deck takes hostj) while the
            # audio came out in another presenter's voice (synth_host_audio had
            # its own default) and the composer wrote the wrong pronoun into the
            # render prompt — a bug no exception reports and only listening
            # catches.
            # `host_tag` goes in every cache key so a wav or deck made for one
            # presenter can never be served to another.
            host_tag = f"{host_file}:{hostj.get('name','')}"
            langs = ["en"] + (["zh"] if hcfg.get("zh", True) else [])
            secs = float(os.environ.get("BRIEFING_HOST_SECS",
                                        hcfg.get("per_slide_secs", 12)))

            # THE STYLIST (production since 2026-08-11): today's look from the
            # real NYC weather, drawn before anything GPU-heavy. Resume-safe (a
            # built day is never re-designed) and failure-safe (any miss —
            # weather API, cloud stylist, identity/framing gates — leaves the
            # locked look; the render never blocks on fashion).
            # THE HOST GETS A VOTE, AND SO DOES THE STYLIST MODULE. A stylist is
            # written for ONE presenter — host2_stylist.py is 陈谷雨's,
            # outfit-only because his crew cut is an identity anchor rather than
            # a styling slot — and writes into that presenter's own stylist_dir.
            #
            # DISPATCH ON THE PRESENTER, never on a default. Running one
            # presenter's stylist for another writes the wrong person's look into
            # a history that day_keyframe() then serves to whoever is presenting
            # — a silent wrong-person render. A presenter with no stylist module
            # here simply wears its locked keyframe.
            _sty = {"host2.json": "briefing.host2_stylist"}.get(host_file)
            if _sty is None:
                print(f"  [stylist] no stylist module for {host_file} — "
                      f"locked look stands")
            elif (os.environ.get("BRIEFING_STYLIST", "1") == "1"
                    and hcfg.get("stylist", True)
                    and hostj.get("stylist", True)):
                from importlib import import_module
                HSTY = import_module(f".{_sty.split('.')[-1]}", __package__)
                try:
                    if (HSTY.HISTORY_DIR / f"{date}.json").exists():
                        print(f"  [stylist] {date}: look already built")
                    else:
                        # comfy-env SUBPROCESS, the scorer pattern: draw_plate
                        # needs cv2, which alpaca-media deliberately lacks (the
                        # exact import that killed two rehearsals in
                        # compose_scene). Failure of any kind → locked look.
                        from .comfy import comfy_python
                        r = subprocess.run(
                            [comfy_python(), "-u",
                             "-m", _sty, "--date", date],
                            cwd=ROOT, capture_output=True, text=True,
                            timeout=1800)
                        sys.stdout.write("".join(
                            f"  {l}\n" for l in r.stdout.splitlines()[-6:]))
                        print(f"  [stylist] {date}: "
                              + ("look ready" if r.returncode == 0
                                 else "did not land — locked look stands"))
                except Exception as exc:                    # noqa: BLE001
                    print(f"  [stylist] failed ({type(exc).__name__}: {exc}) "
                          f"— locked look stands")

            payload, desk_dg, desk_facts = HB.prepare_desk(cfg)
            truth = HB._account_truth(desk_dg)
            held = {p["symbol"] for p in (desk_dg or {}).get("positions", [])}
            deck_in = []
            for sc in scenes:
                sym = sc["symbol"]
                tag = ("" if sym == "OVERVIEW" else
                       ("\n[SHE HOLDS THIS — it is her trade]" if sym in held else
                        "\n[NOT HELD — research only; she is watching, not trading]"))
                deck_in.append({"id": sym, "note": sc["note"] + tag})
                if sc["symbol"] == "OVERVIEW" and desk_dg:
                    deck_in.append({"id": "DESK", "note":
                        "Her own live trading dashboard is on screen — real "
                        "account, real positions. The facts visible on it:\n"
                        + "\n".join(desk_facts or [])})

            # THE VISUAL HALF OF THE HOOK. The writer opens the OVERVIEW line on
            # his own P&L; without this the slide behind that sentence is still
            # showing index moves (S&P -0.2%, Nasdaq -0.3%, "Mood: risk-off"),
            # so the two channels contradict each other for the three seconds
            # that decide whether anyone keeps watching. Same digest, same
            # branching — see host_briefing.desk_hero.
            hero = HB.desk_hero(desk_dg)

            directed = director.direct_briefing(scenes, cfg)
            for sc in scenes:
                sc["director"] = directed.get(sc["symbol"])
                # the briefing entry drives the slide's emoji catalyst cards and
                # key-number grid (production convention); presence of the key
                # also flips _scene_plan into host styling.
                sc["brief"] = briefing_for(dec, sc["symbol"])
                if sc["symbol"] == "OVERVIEW" and hero:
                    sc["desk_hero"] = hero

            decks = {}
            resume = os.environ.get("BRIEFING_HOST_RESUME", "1") == "1"
            deck_key = HB._sha(json.dumps(deck_in, sort_keys=True), secs,
                               truth, HB.DECK_PROMPT_VERSION, host_tag)
            # BRIEFING_DECK_FREEZE=1 reuses the deck ON DISK whatever the key says.
            #
            # The key covers `truth`, so anything that changes the account ground
            # truth rewrites every slide's narration — and since a clip's cache key
            # contains its line, that re-renders every clip too. That is correct for
            # a fresh briefing and wrong for a REPAIR: on 2026-08-13 the desk
            # snapshot had crashed, and restoring it would have re-rendered twelve
            # perfectly good clips (~2.5 h of GPU) to recover one slide. Freezing the
            # deck keeps the narration that already shipped and re-finishes the
            # video around it. Only ever set it deliberately, by hand.
            frozen = os.environ.get("BRIEFING_DECK_FREEZE") == "1"
            for lang in langs:
                deck_f = tmp / f"deck_{lang}.json"
                deck_m = tmp / f"deck_{lang}.meta.json"
                if frozen and deck_f.exists():
                    decks[lang] = {b["slide"]: b
                                   for b in json.loads(deck_f.read_text())}
                    print(f"  [deck] {lang}: FROZEN on disk "
                          f"({len(decks[lang])} slides: {', '.join(decks[lang])})")
                    continue
                if resume and deck_f.exists() and HB.cache_ok(deck_m, deck_key):
                    print(f"  [deck] {lang}: cached (same notes, same budget)")
                    decks[lang] = {b["slide"]: b
                                   for b in json.loads(deck_f.read_text())}
                    continue
                print(f"  [deck] writing {lang} ({secs:.0f}s/slide) …")
                deck = HB.write_deck(deck_in, hostj, cfg, lang=lang,
                                     per_slide_secs=secs, truth=truth)
                deck_f.write_text(json.dumps(deck, ensure_ascii=False, indent=1))
                HB.cache_write(deck_m, deck_key)
                decks[lang] = {b["slide"]: b for b in deck}

            desk_clips, desk_meta = {}, None
            # ONE keyframe for every clip of the day (user decision 2026-08-11;
            # per-clip mood keyframes retired). The writer's mood still shapes
            # the composer's performance text — just not the opening frame.
            day_kf = HB.day_keyframe(date, hostj)
            for sc in scenes:
                sym = sc["symbol"]
                sc["host"] = {}
                for lang in langs:
                    b = decks[lang].get(sym)
                    if b is None:
                        raise SystemExit(f"{lang} deck has no slide for {sym}")
                    sfx = "" if lang == "en" else "_zh"
                    wav = tmp / f"audio_{_safe(sym)}{sfx}.wav"
                    srt = tmp / f"audio_{_safe(sym)}{sfx}.srt"
                    tkey = HB._sha(lang, b["line"], HB.tts_version(lang), host_tag)
                    tmeta = wav.with_suffix(".meta.json")
                    if resume and wav.exists() and HB.cache_ok(tmeta, tkey):
                        import soundfile as _sf
                        _x, _r = _sf.read(wav)
                        dur = len(_x) / _r
                        print(f"  [host-tts] {sym} {lang}: cached")
                    else:
                        print(f"  [host-tts] {sym} {lang} ({b['mood']}) …")
                        dur = HB.synth_host_audio(b["line"], wav, srt, lang,
                                                  host_json=host_path)
                        HB.cache_write(tmeta, tkey)
                    print(f"  [host-clip] {sym} {lang} …")
                    clip = HB.render_host_clip(
                        b, wav, tmp / f"host_{_safe(sym)}{sfx}", cfg,
                        keyframe=day_kf, lang=lang, host=hostj)
                    sc["host"][lang] = {"beat": b, "audio": wav, "srt": srt,
                                        "duration": dur,
                                        "keyframe": day_kf,
                                        "clip": clip}
                # keep the en fields on the scene for the shared meta paths
                sc["host_beat"] = sc["host"]["en"]["beat"]
                sc["audio"] = sc["host"]["en"]["audio"]
                sc["srt"] = sc["host"]["en"]["srt"]
                sc["duration"] = sc["host"]["en"]["duration"]

            # the DESK slide: the account dashboard panned per sentence, the
            # presenter's inset, both languages. Rendered BEFORE the symbol
            # slides — it shares WORK/.
            if desk_dg:
                for lang in langs:
                    b = decks[lang].get("DESK")
                    if b is None:
                        print("  [desk] deck has no DESK slide — skipping")
                        break
                    sfx = "" if lang == "en" else "_zh"
                    wav = tmp / f"audio_DESK{sfx}.wav"
                    srt = tmp / f"audio_DESK{sfx}.srt"
                    tkey = HB._sha(lang, b["line"], HB.tts_version(lang), host_tag)
                    tmeta = wav.with_suffix(".meta.json")
                    if resume and wav.exists() and srt.exists() \
                            and HB.cache_ok(tmeta, tkey):
                        import re as _re
                        import soundfile as _sf
                        _x, _r = _sf.read(wav)
                        dur = len(_x) / _r
                        cues = []
                        for m in _re.finditer(
                                r"(\d+):(\d+):(\d+),(\d+) --> "
                                r"(\d+):(\d+):(\d+),(\d+)\n(.+)",
                                srt.read_text()):
                            g_ = [int(v) for v in m.groups()[:8]]
                            cues.append((g_[0]*3600+g_[1]*60+g_[2]+g_[3]/1000,
                                         g_[4]*3600+g_[5]*60+g_[6]+g_[7]/1000,
                                         m.group(9)))
                        print(f"  [host-tts] DESK {lang}: cached")
                    else:
                        print(f"  [host-tts] DESK {lang} ({b['mood']}) …")
                        dur, cues = HB.synth_host_audio(b["line"], wav, srt, lang,
                                                        host_json=host_path,
                                                        return_cues=True)
                        HB.cache_write(tmeta, tkey)
                    print(f"  [host-clip] DESK {lang} …")
                    hclip = HB.render_host_clip(b, wav, tmp / f"host_DESK{sfx}",
                                                cfg, keyframe=day_kf, lang=lang, host=hostj)
                    dest = out_dir / f"clip_DESK{sfx}.mp4"
                    # "canvas-v2": desk scenes are now conformed to the host
                    # canvas — stale 1080p30 desk clips must not resume.
                    dkey = HB._sha(wav, hclip, "canvas-v2")
                    dmeta = dest.with_suffix(".meta.json")
                    if resume and dest.exists() and HB.cache_ok(dmeta, dkey):
                        print(f"  [desk] {lang}: cached")
                        _desk_ok = True
                    else:
                        _desk_ok = HB.render_desk_scene(payload, desk_dg, cues, wav,
                                                        srt, hclip, cfg, date, dest,
                                                        keyframe=day_kf)
                        if _desk_ok:
                            HB.cache_write(dmeta, dkey)
                    if _desk_ok:
                        desk_clips[lang] = dest
                        if lang == "en":
                            desk_meta = {"symbol": "DESK", "clip_file": dest.name,
                                         "duration_s": round(dur, 2), "bias": None,
                                         "mood": b["mood"],
                                         "headline": "Her account, live",
                                         "desk": desk_dg}
        else:
            directed = director.direct_briefing(scenes, cfg)
            for sc in scenes:
                sc["director"] = directed.get(sc["symbol"])
            for sc in scenes:
                d = sc.get("director")
                spoken = d["narration"] if d else speak_text(sc["symbol"], sc["bias"], sc["note"], sc["limit"])
                sc["audio"] = tmp / f"audio_{_safe(sc['symbol'])}.mp3"
                sc["srt"] = tmp / f"audio_{_safe(sc['symbol'])}.srt"
                print(f"  [tts] {sc['symbol']} …")
                sc["duration"] = synth_audio(spoken, sc["audio"], sc["srt"], cfg)

        images = generate_images(scenes, tmp, cfg)
        if host_on:
            zh_clips = []
            for sc in scenes:
                sym = sc["symbol"]
                for lang in langs:
                    hv = sc["host"][lang]
                    sc["host_keyframe"] = hv["keyframe"]
                    sfx = "" if lang == "en" else "_zh"
                    dest = out_dir / f"clip_{_safe(sym)}{sfx}.mp4"
                    rkey = HB._sha(hv["audio"], hv["clip"],
                                   json.dumps(_scene_plan(sc, cfg), sort_keys=True,
                                              default=str))
                    rmeta = dest.with_suffix(".meta.json")
                    if resume and dest.exists() and HB.cache_ok(rmeta, rkey):
                        print(f"  [render] {sym} {lang}: cached")
                    else:
                        print(f"  [render] {sym} {lang} ({hv['duration']:.1f}s) …")
                        render_clip(sc, hv["audio"], hv["srt"], images.get(sym, {}),
                                    hv["duration"], cfg, date, dest, pip=hv["clip"])
                        HB.cache_write(rmeta, rkey)
                    (clip_paths if lang == "en" else zh_clips).append(dest)
                d = sc.get("director")
                hb = sc.get("host_beat")
                clips_meta.append({"symbol": sym, "clip_file": f"clip_{_safe(sym)}.mp4",
                                   **({"clip_file_zh": f"clip_{_safe(sym)}_zh.mp4"}
                                      if "zh" in langs else {}),
                                   "duration_s": round(sc["duration"], 2),
                                   "bias": sc["bias"],
                                   **({"mood": hb["mood"]} if hb else {}),
                                   "headline": (d["title"] if d and d.get("title")
                                                else headline_text(sc["note"]))})
                if sym == "OVERVIEW" and desk_clips.get("en"):
                    clip_paths.append(desk_clips["en"])
                    if desk_clips.get("zh"):
                        zh_clips.append(desk_clips["zh"])
                    if desk_meta:
                        clips_meta.append(desk_meta)
            if zh_clips:
                print(f"  [concat] {len(zh_clips)} zh clip(s) -> {briefing_zh.name}")
                concat_clips(zh_clips, briefing_zh, cfg)
        else:
            for sc in scenes:
                sym = sc["symbol"]
                dest = out_dir / f"clip_{_safe(sym)}.mp4"
                print(f"  [render] {sym} ({sc['duration']:.1f}s) …")
                render_clip(sc, sc["audio"], sc.get("srt"), images.get(sym, {}),
                            sc["duration"], cfg, date, dest, pip=sc.get("host_clip"))
                clip_paths.append(dest)
                d = sc.get("director")
                hb = sc.get("host_beat")
                clips_meta.append({"symbol": sym, "clip_file": dest.name,
                                   "duration_s": round(sc["duration"], 2), "bias": sc["bias"],
                                   **({"mood": hb["mood"]} if hb else {}),
                                   "headline": (d["title"] if d and d.get("title")
                                                else headline_text(sc["note"]))})
        print(f"  [concat] {len(clip_paths)} clip(s) -> {briefing.name}")
        concat_clips(clip_paths, briefing, cfg)
        used_images = any(v.get("bg") for v in images.values())
        used_director = any(sc.get("director") for sc in scenes)

    # --- headline + cover sheet: gemma distills the session's top fact into a
    # title + bold keywords + an art idea; klein draws the crayon art; the words
    # go on as crisp typography. Purely cosmetic — any failure skips it.
    head = head_zh = None
    if cfg.get("cover", {}).get("enabled", True) and clips_meta:
        try:
            from . import comfy
            comfy.free_memory(cfg)          # Wan/klein must vacate VRAM before gemma
            slot_slug = briefing_slot(
                {"renderer": cfg.get("story", {}).get("renderer") if mode == "story" else None,
                 "label": args.label})
            # the same slot's previous front pages, so the headline stops
            # re-leading with a story that already had the cover (the 10-year
            # yield "highest since 2007" led six editions, 08-18 → 09-24)
            recent = recent_titles(archive_root, date, slot_slug)
            head = director.headline(dec, clips_meta, cfg,
                                     recent=[r["title"] for r in recent])
            if head and briefing_zh.exists():
                head_zh = director.headline_zh(
                    head, cfg, recent_zh=[r["title_zh"] for r in recent if r["title_zh"]],
                    source=director.recap_source(dec))
            director.unload(cfg)            # …and gemma before klein loads
            if head:
                cov = cfg.get("cover", {})
                sb_style = cfg.get("story", {}).get("storyboard", {})
                rcov = cov.get("recap", {})
                # HOST MODE: the poster is the PRESENTER — the day's keyframe, no
                # klein art at all. The dominant mood is the first slide's (the
                # day at a glance), which host mode anchors to the account's day.
                if (bool(cfg.get("host", {}).get("enabled"))
                        or os.environ.get("BRIEFING_HOST") == "1"):
                    day_mood = next((c.get("mood") for c in clips_meta
                                     if c.get("mood")), "")
                    # the day's keyframe is the poster, so cover and video share
                    # a look (full-bleed when it is a STAGED 16:9 frame: the real
                    # textured wall behind the headline, user directive
                    # 2026-08-11). On a styled day day_keyframe() returns the
                    # stylist's stylist_<slug>_stage16x9.png, so the poster wears
                    # the same outfit as the video for free. That naming
                    # convention is load-bearing, which is why
                    # host2_stylist.draw_keyframe keeps it.
                    day_kf_ = HB.day_keyframe(date, hostj)
                    staged_ = day_kf_ if "stage16x9" in day_kf_.name else None
                    plate = staged_ or day_kf_
                    pnl = next((c.get("desk") for c in clips_meta
                                if c.get("desk")), None)
                    if pnl and pnl.get("day_pl_pct") is not None:
                        # the desk digest stores a FRACTION; the badge prints
                        # the number as-is — convert here (BRIEFING_PNL's test
                        # hook already passes percent units)
                        pnl = {**pnl, "day_pl_pct": float(pnl["day_pl_pct"]) * 100}
                    if pnl is None and os.environ.get("BRIEFING_PNL"):
                        # test hook: "212.40,4.41" — production gets the real
                        # numbers from the desk capture
                        _v = os.environ["BRIEFING_PNL"].split(",")
                        pnl = {"day_pl": float(_v[0]),
                               "day_pl_pct": float(_v[1]) if len(_v) > 1 else None}
                    make_cover_host(head, args.label, date, plate, cover, cfg,
                                    pnl=pnl)
                    print(f"  [cover] host poster ({day_mood or 'warm'}"
                          f"{', P&L badge' if pnl and pnl.get('day_pl') is not None else ''}) "
                          f"“{head['title']}” -> {cover.name}")
                    if head_zh:
                        make_cover_host(head_zh, args.label, date, plate, cover_zh,
                                        cfg, pnl=pnl)
                        print(f"  [cover] host poster 中文 -> {cover_zh.name}")
                    raise _CoverDone
                # The flagship recap gets its OWN poster design (make_cover_recap),
                # and its own klein plate: the poster runs the art FULL-BLEED, so it
                # needs the subject centred and high (the type sits in a scrim across
                # the bottom third).
                framing = rcov.get("framing", _RECAP_FRAMING)
                art = tmp / "cover_art_recap.png"
                prompt = (sb_style.get("scene_style", "") + head["art"] + framing +
                          sb_style.get("scene_tail", ""))
                seed = int(rcov.get("seed", 8484))
                ok = comfy.ensure_server(cfg, verbose=False) and comfy.klein_icon(
                    cfg, prompt, art, seed=seed,
                    width=int(cov.get("width", 1280)), height=int(cov.get("height", 720)))
                art_ok = art if ok and art.exists() else None
                make_cover_recap(head, args.label, date, art_ok, cover, cfg)
                print(f"  [cover] recap poster “{head['title']}” -> {cover.name}")
                if head_zh:                 # same art, Chinese typography
                    make_cover_recap(head_zh, args.label, date, art_ok, cover_zh, cfg)
                    print(f"  [cover] “{head_zh['title']}” -> {cover_zh.name}")
        except _CoverDone:
            pass
        except Exception as exc:  # noqa: BLE001 — cosmetic; never fail the render
            print(f"  [cover] skipped ({type(exc).__name__}: {exc})")

    # The desk slide travels with the account digest it was narrated from. It
    # belongs in meta once, not on every clip row — and the NEXT edition reads it
    # back as the baseline for "what changed since the last briefing".
    desk_digest = next((c.pop("desk") for c in clips_meta if c.get("desk")), None)

    meta = {
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "briefing_file": briefing.name,
        "duration_s": round(_duration(briefing), 2),
        "mode": mode,
        "label": args.label,
        "renderer": renderer_used if mode == "story" else None,
        "title": (head or {}).get("title") or None,
        "model": cfg.get("image_model") if used_images else None,
        "images": used_images,
        "subtitles": bool(cfg.get("subtitles", True)),
        "director": cfg.get("director", {}).get("model") if used_director else None,
        "voice": cfg.get("voice"),
        "clips": clips_meta,
    }
    if desk_digest:
        meta["desk"] = desk_digest
    if briefing_zh.exists():
        meta["briefing_file_zh"] = briefing_zh.name
        meta["voice_zh"] = cfg.get("zh", {}).get("voice")
    if head_zh:
        meta["title_zh"] = head_zh["title"]
        if head_zh.get("check"):
            # what the second model found, so a flagged title is visible in the
            # archive rather than only in the render log
            meta["title_zh_check"] = head_zh["check"]
    if head:
        meta["cover_head"] = head            # keywords + art idea, for exact re-renders
    if cover.exists():
        meta["cover_file"] = cover.name
    if cover_zh.exists():
        meta["cover_file_zh"] = cover_zh.name
    # manifest.json = latest only (kept for compatibility); history.json = full list.
    (out_dir / "manifest.json").write_text(json.dumps(
        {**meta, "briefing": f"/media/{briefing.name}",
         **({"briefing_zh": f"/media/{briefing_zh.name}"} if briefing_zh.exists() else {}),
         **({"cover": f"/media/{cover.name}"} if cover.exists() else {}),
         **({"cover_zh": f"/media/{cover_zh.name}"} if cover_zh.exists() else {}),
         "clips": [{**c, "file": f"/media/{c['clip_file']}",
                    **({"file_zh": f"/media/{c['clip_file_zh']}"} if c.get("clip_file_zh") else {})}
                   for c in clips_meta]}, indent=2))

    print(f"  [archive] -> {archive_root / date}")
    archive_and_index(date, briefing, clip_paths, meta, out_dir, archive_root)
    print(f"✅ wrote {briefing} ({meta['duration_s']:.0f}s), archived to {archive_root / date}")


if __name__ == "__main__":
    main()
