"""THE DIRECTOR — an optional Gemma pass that rewrites each raw research note
into a briefing-ready screenplay + shot plan before the video is rendered.

Runs a local ollama model (default the CLOUD-hosted ``gemma4:31b-cloud`` so it
uses NO local VRAM and never competes with SDXL or other GPU jobs). For each
scene it returns:

    {
      "narration":        witty-but-faithful spoken script (drives edge-tts + subs),
      "title":            1-3 word on-screen tagline,
      "mood":             one word,
      "background_prompt": SDXL prompt for the full-bleed background,
      "elements": [ {role,label,value,image_prompt,motion}, ... ]   # animated cards
    }

HARD CONSTRAINTS baked into the prompt: every ticker/number/%/level/bias/date
must come STRAIGHT from the note — the model may restyle and add wit but may not
invent, change, round, or flip facts. This layer is VIDEO-ONLY cosmetics: the
trade path reads decisions.json directly and never sees this output, so a funny
rewrite cannot affect a single order.

On any failure (ollama down, timeout, malformed JSON, OOM) ``direct_briefing``
simply omits that scene and the caller falls back to the deterministic
text-shaping in generate.py — a render never fails because of the director.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

MOTIONS = {"slide-left", "slide-right", "rise", "pop", "fade", "zoom"}
ROLES = {"price", "catalyst", "risk", "level", "sentiment"}

_HUMOR = {
    "wry": "wry, dry, quietly funny — sharp understated wit, facts first, never slapstick",
    "punchy": "punchy and funny — late-night-host energy, playful metaphors, still accurate",
    "straight": "clear and straight — no jokes, just a tighter, better-paced rewrite",
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "narration": {"type": "string"},
        "title": {"type": "string"},
        "mood": {"type": "string"},
        "background_prompt": {"type": "string"},
        "elements": {"type": "array", "items": {"type": "object", "properties": {
            "role": {"type": "string"}, "label": {"type": "string"},
            "value": {"type": "string"}, "image_prompt": {"type": "string"},
            "motion": {"type": "string"}}, "required": ["role", "label", "value", "image_prompt", "motion"]}},
    },
    "required": ["narration", "title", "mood", "background_prompt", "elements"],
}


def _strip_preamble(note: str) -> str:
    return re.sub(r"^\s*(FLAT|LONG|SHORT|long|short|flat),\s*priority\s*\d+\s*(--|—|,)?\s*",
                  "", note).strip()


def _content_block(note: str, brief: dict | None) -> str:
    """Fold the raw research note together with the richer VIDEO ``briefing``
    (story + catalysts + indicators, when the research produced it) into ONE
    faithful context block for the director. When a `story` is present it is the
    spoken-quality source (~1 min of material); the catalysts/indicators are
    listed so the director's narration and callouts use the REAL labels/numbers."""
    b = brief if isinstance(brief, dict) else {}
    parts = [_strip_preamble(b.get("story") or note)]
    cats = [c for c in (b.get("catalysts") or []) if isinstance(c, dict)]
    if cats:
        parts.append("CATALYSTS: " + "; ".join(
            (c.get("label", "") + (f" — {c['detail']}" if c.get("detail") else "")).strip()
            for c in cats))
    inds = [i for i in (b.get("indicators") or []) if isinstance(i, dict)]
    if inds:
        parts.append("INDICATORS (use these EXACT values in callouts, do not round): "
                     + "; ".join(f"{i.get('label','')} = {i.get('value','')}".strip() for i in inds))
    return "\n".join(p for p in parts if p)


def _system(cfg: dict) -> str:
    voice = _HUMOR.get(cfg.get("humor", "wry"), _HUMOR["wry"])
    return (
        f"You are THE DIRECTOR - a screenwriter and shot-planner for a daily quant "
        f"trading VIDEO briefing. Voice: {voice}. You turn one dry research note into a "
        f"short SPOKEN script plus a shot plan.\n\n"
        "HARD RULES you NEVER break: every ticker, number, %, price level, bias and date "
        "in your script and labels must come STRAIGHT from the note. You may restyle, "
        "compress and add wit, but you may NOT invent, change, round, or flip facts, and "
        "FLAT stays flat. No financial advice.\n\n"
        "Each element has: a `label` = a 1-2 word category header (e.g. Price, Catalyst, "
        "Trend, Risk, Level); a `value` = the single punchy fact to show LARGE, kept very "
        "short (a number, %, date, or 2-3 words, max ~16 characters) - put the vivid detail "
        "in the narration, not the card. For each element write a concise `image_prompt` for a "
        "SMALL icon: a vivid, high-contrast, centered symbol on a soft glowing or light neutral "
        "background (NEVER pure black); describe ONLY shapes, objects and colours, NEVER text, "
        "letters or numbers, and end every image_prompt with 'no text, no words, no letters'. "
        f"motion is exactly one of: {', '.join(sorted(MOTIONS))}. role is exactly one of: "
        f"{', '.join(sorted(ROLES))}. Output ONLY a single JSON object, no markdown."
    )


def _user(symbol: str, bias: str | None, note: str, secs: int, max_el: int) -> str:
    b = bias or "n/a (market overview)"
    subject = ("the whole market" if symbol == "OVERVIEW" else symbol)
    return (
        f"SYMBOL: {symbol}\nBIAS: {b}\nTARGET_SECONDS: {secs}\n\n"
        f"RAW RESEARCH NOTE:\n{_strip_preamble(note)}\n\n"
        f"Write a spoken `narration` (~{secs} seconds, ~{int(secs * 2.6)} words) about "
        f"{subject}, a 1-3 word on-screen `title`, a one-word `mood`, a full-bleed "
        f"`background_prompt`, and 1 to {max_el} `elements` (the facts that best tell this "
        f"scene's story — price action, catalyst, risk, level or sentiment)."
    )


def _parse(content: str) -> dict:
    c = content.strip()
    c = re.sub(r"^```[a-zA-Z]*\n?", "", c)
    c = re.sub(r"\n?```$", "", c).strip()
    i, j = c.find("{"), c.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("no JSON object in response")
    return json.loads(c[i:j + 1])


def _is_cloud(model: str) -> bool:
    return model.endswith("-cloud") or model.endswith(":cloud")


# think levels a LOCAL gemma runner understands; the cloud models also accept
# "max"/booleans. Mapped when a call falls back so the fallback never 500s on a
# level the local runner has no notion of.
_LOCAL_THINK = {"max": "high", True: "high"}


def _chat_json(payload: dict, d: dict, timeout: float, tag: str,
               salvage=None) -> dict:
    """POST /api/chat, parse the JSON content, and enforce the schema's
    `required` keys OURSELVES — then fall back to `director.fallback_model`.

    `salvage` is an optional `(raw_content) -> dict | None` hook, tried whenever
    the envelope is wrong (unparseable, or missing required keys) but the answer
    inside it may still be usable. See `_zh_salvage`.

    Two facts drive this (both measured 2026-08-03):
      * The LOCAL llama.cpp path grammar-constrains `format`, so required keys
        always land. The CLOUD path only *requests* the schema — deepseek
        returned {"visual_prop": …} through a schema requiring {"prop": …},
        plus markdown fences. Key presence must be checked client-side.
      * A cloud model is a network dependency; the local model is not. With a
        `fallback_model` configured, a cloud outage degrades to gemma instead
        of degrading to the deterministic text.
    One corrective retry per model (the retry names the missing keys), then the
    next model. Raises ValueError when every candidate fails, which every call
    site already catches."""
    fmt = payload.get("format")
    need = list(fmt.get("required", [])) if isinstance(fmt, dict) else []
    models = [payload["model"]]
    fb = d.get("fallback_model")
    if fb and fb not in models:
        models.append(fb)
    last = "no models to try"
    for model in models:
        p = dict(payload, model=model)
        if model != payload["model"]:
            print(f"  [director] {tag}: {payload['model']} failed ({last}) — "
                  f"falling back to {model}")
            th = p.get("think")
            p["think"] = _LOCAL_THINK.get(th, th) if not _is_cloud(model) else th
        for attempt in range(2):
            if attempt:
                p = dict(p)
                p["messages"] = list(payload["messages"]) + [{
                    "role": "user",
                    "content": "Your previous answer was missing required JSON keys. "
                               "Return ONLY one JSON object with exactly these "
                               f"top-level keys: {', '.join(need)}."}]
            try:
                req = urllib.request.Request(f"{OLLAMA}/api/chat",
                                             data=json.dumps(p).encode(),
                                             headers={"Content-Type": "application/json"})
                resp = json.load(urllib.request.urlopen(req, timeout=timeout))
                content = resp["message"]["content"]
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            try:
                out = _parse(content)
                missing = [k for k in need if k not in out]
            except (ValueError, json.JSONDecodeError):
                out, missing = None, list(need) or ["<json object>"]
            if missing and salvage is not None:
                alt = salvage(content)
                if alt is not None:
                    out, missing = alt, [k for k in need if k not in alt]
            if not missing:
                return out
            last = f"missing keys: {', '.join(missing)}"
    raise ValueError(f"{tag}: {last}")


def _norm_motion(m: str) -> str:
    m = (m or "").strip().lower().replace("_", "-").replace(" ", "-")
    return m if m in MOTIONS else "fade"


def _normalize(out: dict, max_el: int) -> dict:
    els = []
    for e in (out.get("elements") or [])[:max_el]:
        if not e.get("value") and not e.get("label"):
            continue
        els.append({
            "role": (e.get("role") or "").strip().lower(),
            "label": (e.get("label") or "").strip()[:22],
            "value": (e.get("value") or "").strip()[:26],
            "image_prompt": (e.get("image_prompt") or "").strip(),
            "motion": _norm_motion(e.get("motion")),
        })
    return {
        "narration": (out.get("narration") or "").strip(),
        "title": (out.get("title") or "").strip()[:28],
        "mood": (out.get("mood") or "").strip()[:20],
        "background_prompt": (out.get("background_prompt") or "").strip(),
        "elements": els,
    }


def direct_scene(scene: dict, cfg: dict) -> dict | None:
    d = cfg.get("director", {})
    secs = d.get("target_seconds", {}).get(
        "overview" if scene["symbol"] == "OVERVIEW" else "symbol",
        60 if scene["symbol"] == "OVERVIEW" else 26)
    max_el = int(d.get("max_elements", 3))
    payload = {
        "model": d.get("model", "gemma4:31b-cloud"),
        "messages": [{"role": "system", "content": _system(d)},
                     {"role": "user", "content": _user(scene["symbol"], scene.get("bias"),
                                                        scene["note"], secs, max_el)}],
        "stream": False, "format": _SCHEMA, "think": d.get("think", "high"),
        "keep_alive": d.get("keep_alive", "5m"),
        "options": {"temperature": d.get("temperature", 0.8),
                    "num_ctx": d.get("num_ctx", 8192),
                    "num_predict": d.get("num_predict", 4096)},
    }
    last = None
    for attempt in range(2):                          # one retry on transient failure
        try:
            out = _normalize(_chat_json(payload, d, d.get("timeout_s", 180),
                                        f"direct:{scene['symbol']}"), max_el)
            if len(out["narration"].split()) >= 6:    # sanity: real script, not a stub
                return out
            last = "narration too short"
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
    print(f"  [director] {scene['symbol']} fell back to deterministic ({last})")
    return None


def unload(cfg: dict) -> None:
    """Ask ollama to unload EVERY loaded model now (free VRAM before klein/Wan),
    then WAIT until they are actually gone: the unload is async, and a model
    loaded while an old runner still holds VRAM gets a terrible GPU/CPU split
    that ollama never rebalances (observed: 4.8GB of 21GB on-GPU -> glacial).
    Must cover ALL models, not just the director: the QC reviewer (qwen3-vl) is
    a different model and staying resident starved klein of VRAM ('VRAM grow
    failed' graph errors, 2026-07-25)."""
    names = {cfg.get("director", {}).get("model", "gemma4:31b")}
    try:
        ps = json.load(urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=5))
        names |= {m.get("name") or m.get("model") for m in ps.get("models", [])}
    except (OSError, ValueError):
        pass
    # Cloud models hold no local VRAM; "unloading" one is a pointless (and
    # billable) network round-trip to ollama.com.
    for model in sorted(n for n in names if n and not _is_cloud(n)):
        try:
            body = json.dumps({"model": model, "prompt": "", "stream": False,
                               "keep_alive": 0}).encode()
            req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
        except OSError:
            pass
    import time as _time
    for _ in range(20):                     # up to ~20s for the runner to exit
        try:
            ps = json.load(urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=5))
            if not ps.get("models"):
                return
        except (OSError, ValueError):
            return
        _time.sleep(1)


def _scene_screenplay_impl(symbol: str, bias: str | None, note: str, character: dict,
                           cfg: dict, brief: dict | None = None,
                           beats: int | None = None, schema: dict | None = None,
                           extra_sys: str = "") -> dict | None:
    """A multi-shot screenplay for one symbol: each beat is a spoken line PLUS an
    action that physically ACTS THAT LINE OUT, and an optional number/fact to pop
    on screen. Beat lines together are the narration; each beat becomes one video
    shot whose length is its spoken duration. Returns the screenplay or None.

    The beats are written as ONE continuous scene — a single setting, a prop carried
    through, and each action resuming where the last ended — because consecutive
    beats are only cross-faded, so unrelated per-beat poses read as a broken cut.

    ``brief`` (optional) is the symbol's richer research briefing
    ({story, catalysts, indicators}); when present it drives a longer, fact-dense
    screenplay. ``beats`` overrides the beat count (default: story.video_beats)."""
    d = cfg.get("director", {})
    s = cfg.get("story", {})
    n = int(beats or s.get("video_beats") or s.get("scene_beats", 4))
    voice = _HUMOR.get(d.get("humor", "wry"), _HUMOR["wry"])
    who = character.get("description") or f"{character.get('name', symbol)} the {character.get('kind','character')}"
    overview = symbol == "OVERVIEW"
    subject = ("THE WHOLE MARKET this session (which names bounced or sulked, the earnings 'wall', the "
               "macro mood) as your ANCHOR sets up the day" if overview else f"the stock {symbol}")
    facts = ("who moved, the dominant catalyst/earnings calendar, the overall tone" if overview
             else "bias, price move, catalyst, key levels, mechanics")
    system = (
        f"You are THE DIRECTOR of a funny animated financial briefing. Voice: {voice}. Star: a FIXED "
        f"character — {who}. Personality: {character.get('personality','')}.\n\n"
        f"Write a {n}-beat SCREENPLAY about {subject}, TRUE to the research ({facts}) — accurate, "
        "plain-English, and funny. Aim for roughly a MINUTE of narration total.\n\n"
        "`setting` — ONE room/place, the physical environment as CONTENT only: 3-6 concrete "
        "objects/furniture/places (e.g. 'a tall bar chart on an easel, a small desk with a computer, "
        "scattered papers'), NO art-style words (the drawing style is fixed elsewhere). Every beat "
        "happens HERE; it never changes mid-scene. Choose props the character can actually USE, because "
        "the actions below have to interact with them.\n\n"
        "`line` — the spoken narration, a full ~16-20 words (a complete thought, not a fragment).\n\n"
        "`action` — ONE continuous physical movement that ACTS OUT WHAT ITS OWN LINE SAYS. This is the "
        "most important rule and the easiest to get wrong: the action is not decoration and not a random "
        "verb, it is the line's meaning performed with the body and the props. If the line says a stock "
        "gave back its gains, the character climbs the easel then slides back down it; if the line says "
        "shorts were squeezed, the character is pressed between two stacked crates; if the line says a "
        "number was beaten, the character knocks a marker higher up the chart. A generic 'points at the "
        "chart' or 'celebrates' when the line is about something specific is a FAILURE.\n\n"
        "The beats must form ONE CONTINUOUS, SMOOTH STORY, not four unrelated poses:\n"
        "- Each action STARTS FROM WHERE THE PREVIOUS ACTION ENDED — same place, same posture, still "
        "holding whatever they picked up. Say so in the words (e.g. 'still holding the red folder, "
        "turns from the easel and walks to the desk'). Consecutive shots are cross-faded, so a "
        "character who teleports across the room between beats looks broken.\n"
        "- Carry ONE physical prop through the whole scene as a through-line — pick it up in beat 1, "
        "still have it at the end, let what happens to it mirror what happens to the stock.\n"
        "- Shape it as an arc: beat 1 establishes the situation, the middle beats turn it, the last "
        "beat lands the consequence.\n"
        "- Make the scene ONE EXTENDED METAPHOR for the note's key mechanic, chosen so the room's "
        "PHYSICS mirror the trade's mechanics: a rebound wants a spring or trampoline, a short "
        "squeeze wants something compressed that snaps, a rotation wants a seesaw or two pans of a "
        "scale, a slow grind higher wants stairs or a ladder. State the metaphor through the props "
        "and let the FINAL beat pay it off physically — do not explain it in the narration.\n"
        "- ONE clear slow movement per beat, described as MOTION rather than a frozen pose ('slowly "
        "leans over the desk', not 'is leaning over the desk'). Two actions crammed into one beat come "
        "out as a blur.\n\n"
        "WHENEVER the line mentions a number, %, price, level, date or catalyst, also give a "
        "`callout_label` (1-2 words), a `callout_value` (the exact number/date to POP on screen big, "
        "e.g. '+1.7%', '352', 'EARNINGS JUL 22'; keep it under 16 characters, it is cut off at 16), AND "
        "a `callout_image`: ONE simple bold SYMBOL that visualises it, named as CONTENT only in 2-6 "
        "words (e.g. 'a rising green candlestick chart', 'a falling red arrow', 'a calendar page', 'a "
        "target bullseye', 'a gavel', 'a warning triangle') — NO art-style words, the icon's drawing "
        "style is fixed elsewhere.\n\n"
        "Never invent facts. Output ONLY JSON."
    ) + extra_sys
    user = (f"SYMBOL: {symbol}\nBIAS: {bias or 'n/a'}\n\n"
            f"{'MARKET OVERVIEW' if overview else 'RESEARCH'} NOTE:\n{_content_block(note, brief)}\n\n"
            f"Write the screenplay JSON: setting, beats[line,action,callout_label,callout_value,callout_image] "
            f"({n} beats). Spread the CATALYSTS and INDICATORS above across the beats — when a beat's line "
            f"states one, set its callout to that EXACT label/value.\n"
            f"Before writing, decide the ONE place this scene happens, the ONE prop the character carries "
            f"through it, and how they move around that place beat by beat — then make every `action` a "
            f"literal physical performance of its own `line`, continuing from the previous beat's "
            f"end position.")
    payload = {
        "model": d.get("model", "gemma4:31b"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False, "format": schema, "think": s.get("think", d.get("think", "high")),
        "keep_alive": s.get("scene_keep_alive", "8m"),
        "options": {"temperature": s.get("temperature", 0.85),
                    "num_ctx": d.get("num_ctx", 8192), "num_predict": s.get("num_predict", 5000)},
    }
    beats = []
    for attempt in range(2):
        try:
            out = _chat_json(payload, d, s.get("timeout_s", 700), f"screenplay:{symbol}")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"  [scene] {symbol} screenplay failed ({type(exc).__name__}: {exc})")
            return None
        # Content sanity, not just key presence: the cloud path once returned a
        # beat whose line was the literal string "line" (the schema names the
        # key; nothing constrains the value). A spoken line is a sentence.
        beats = [b for b in out.get("beats", [])
                 if len(str(b.get("line", "")).split()) >= 5 and b.get("action")]
        if len(beats) >= min(3, n):
            break
        print(f"  [scene] {symbol}: only {len(beats)} usable beat(s) — retrying")
    if not beats:
        return None
    for b in beats:
        cv = (b.get("callout_value") or "").strip()
        b["callout"] = ({"label": (b.get("callout_label") or "").strip()[:16], "value": cv[:16],
                         "image_prompt": (b.get("callout_image") or "").strip()} if cv else None)
    return {"setting": out.get("setting", "").strip(), "beats": beats}


# --------------------------------------------------------------------------
# MiniMax H3 screenplay: same beats, plus per-beat CAMERA direction
# --------------------------------------------------------------------------
# The exact vocabulary from MiniMaxAI/MiniMax-H3's official prompt-writing
# guide (docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md). H3 renders a whole scene
# as ONE clip with internal, timestamped cuts, so unlike the Wan path a beat's
# camera is a real degree of freedom — the guide's rule: state framing, angle
# and ONE primary movement in plain production language.
_H3_FRAMINGS = ("wide shot", "medium-wide shot", "medium shot", "medium close-up",
                "close-up", "extreme close-up")
_H3_ANGLES = ("eye-level", "low angle", "high angle", "over-the-shoulder", "top-down")
_H3_MOVES = ("static shot", "push in", "pull out", "pan left", "pan right",
             "truck left", "truck right", "tilt up", "tilt down", "pedestal up",
             "pedestal down", "arc shot", "tracking shot", "zoom in", "zoom out")

# Guide §4.3 treats amplitude and speed as OPTIONAL third/second dimensions:
# "Add amplitude and speed only when they are meaningful; medium amplitude and
# normal speed are usually omitted." "" is the omit case.
_H3_AMPLITUDES = ("", "small", "large")
_H3_SPEEDS = ("", "slow", "fast")

_SCREEN_H3_SCHEMA = {
    "type": "object",
    "properties": {
        "setting": {"type": "string"},
        "beats": {"type": "array", "items": {"type": "object", "properties": {
            "line": {"type": "string"}, "action": {"type": "string"},
            "framing": {"type": "string", "enum": list(_H3_FRAMINGS)},
            "angle": {"type": "string", "enum": list(_H3_ANGLES)},
            "movement": {"type": "string", "enum": list(_H3_MOVES)},
            "amplitude": {"type": "string", "enum": list(_H3_AMPLITUDES)},
            "speed": {"type": "string", "enum": list(_H3_SPEEDS)},
            "callout_label": {"type": "string"}, "callout_value": {"type": "string"},
            "callout_image": {"type": "string"}},
            "required": ["line", "action", "framing", "angle", "movement"]}},
    },
    "required": ["setting", "beats"],
}

_H3_CAMERA_RULES = (
    "\n\nCAMERA (per beat): also choose `framing` (wide shot / medium-wide shot / medium "
    "shot / medium close-up / close-up / extreme close-up), `angle` (eye-level / low angle "
    "/ high angle / over-the-shoulder / top-down) and ONE `movement` (static shot / push in "
    "/ pull out / pan left|right / truck left|right / tilt up|down / pedestal up|down / arc "
    "shot / tracking shot / zoom in|out). Direct like an editor, not a tripod: a CUT between "
    "beats should reveal NEW information (a different distance, a new angle on the prop, the "
    "consequence just caused), while within a beat the single movement follows the action — "
    "push in on what the line makes important, track a character who crosses the room, hold "
    "static when the action itself is the spectacle. Beat 1 usually establishes wide; do not "
    "repeat the same framing+angle on consecutive beats."
    "\n\nAMPLITUDE AND SPEED (optional, per beat): `amplitude` (small | large) is how much "
    "the composition changes and `speed` (slow | fast) is how fast it changes. Leave BOTH as "
    "the empty string for ordinary motion — the guide omits them unless they carry meaning, "
    "and a clip where every shot crawls at small amplitude reads as timid. Reach for `large` "
    "or `fast` only when the beat earns it (a reveal, a sudden consequence), and never set "
    "either on a `static shot`."
)


def scene_screenplay_h3(symbol: str, bias: str | None, note: str, character: dict,
                        cfg: dict, brief: dict | None = None,
                        beats: int | None = None) -> dict | None:
    """The screenplay, extended for the MiniMax H3 renderer: identical beats
    (line / action / callout drive TTS, HUD and timing exactly as before) plus
    per-beat camera direction. Pairs of beats later become ONE H3 clip with a
    timestamped internal cut (see h3_clip_prompt), so the camera fields are the
    only genuinely new creative surface."""
    sp = _scene_screenplay_impl(symbol, bias, note, character, cfg, brief, beats,
                                schema=_SCREEN_H3_SCHEMA, extra_sys=_H3_CAMERA_RULES)
    if not sp:
        return None
    for b in sp["beats"]:                      # normalize; missing -> guide defaults
        b["framing"] = (b.get("framing") or "medium shot").strip().lower()
        b["angle"] = (b.get("angle") or "eye-level").strip().lower()
        b["movement"] = (b.get("movement") or "static shot").strip().lower()
        # "" is a real value here (guide §4.3: omit unless meaningful), so an
        # unusable answer degrades to omitted rather than to a hardcoded crawl.
        amp = (b.get("amplitude") or "").strip().lower()
        spd = (b.get("speed") or "").strip().lower()
        b["amplitude"] = amp if amp in _H3_AMPLITUDES else ""
        b["speed"] = spd if spd in _H3_SPEEDS else ""
    return sp


_H3_MAX_SECONDS = 362 / 24.0          # 15.083s — the trained 17k+5 grid ceiling


def h3_frames(seconds: float) -> int:
    """Snap a duration UP to H3's 17k+5 frame grid at 24fps, capped at the
    trained maximum (362 frames)."""
    n = max(5, int(seconds * 24))
    while n % 17 != 5:
        n += 1
    return min(n, 362)


def h3_group_beats(durations: list[float],
                   max_s: float = _H3_MAX_SECONDS) -> list[tuple[list[int], int]]:
    """Greedy duration-aware grouping of beats into H3 clips.

    2026-08-04's director test paired two ~9s lines into one clip and hit the
    15.08s ceiling — the last ~3s of narration would have played over a frozen
    frame. Beats are grouped consecutively while their spoken durations fit one
    clip; a beat that doesn't fit starts the next clip; a single beat longer
    than the ceiling still gets its own (clamped) clip rather than vanishing.
    Returns [(beat_indices, clip_frames), …] covering every beat in order."""
    groups: list[tuple[list[int], int]] = []
    cur: list[int] = []
    total = 0.0
    for i, d in enumerate(durations):
        if cur and total + d > max_s:
            groups.append((cur, h3_frames(total)))
            cur, total = [], 0.0
        cur.append(i)
        total += d
    if cur:
        groups.append((cur, h3_frames(total)))
    return groups


# Standing clause appended to every H3 prompt. The 2026-08-04 test leaked
# legible chart labels despite a no-text style lock — a chart PROP invites
# labels unless the prompt says what the chart shows instead.
_H3_NO_TEXT = ("Every chart, page, board or screen in frame shows only unlabeled "
               "colored lines, bars and shapes. There is no text, no letters, no "
               "digits, no symbols and no writing anywhere in any frame.")

# Guide §4.3: "Camera motion should be written as a natural English action within
# the shot, rather than stacked as separate labels at the end of a sentence." The
# schema collects the move as a noun (a stable enum the director can be scored
# on); this maps it back to the verb form the guide's examples are written in.
_H3_MOVE_VERBS = {
    "static shot": "holds a static shot",
    "push in": "pushes in",
    "pull out": "pulls out",
    "pan left": "pans left",
    "pan right": "pans right",
    "truck left": "trucks left",
    "truck right": "trucks right",
    "tilt up": "tilts up",
    "tilt down": "tilts down",
    "pedestal up": "pedestals up",
    "pedestal down": "pedestals down",
    "arc shot": "arcs around the subject",
    "tracking shot": "tracks the subject",
    "zoom in": "zooms in",
    "zoom out": "zooms out",
}


def _h3_camera(beat: dict) -> str:
    """One beat's camera motion as a guide-shaped English clause.

    Amplitude and speed are appended only when the director asked for them, and
    never on a static shot — "the camera does a static shot at slow speed with
    small amplitude" (the pre-2026-08-06 wording) is not a motion the guide's
    vocabulary describes, and hardcoding slow+small on every shot flattened the
    whole clip to one pace."""
    move = (beat.get("movement") or "static shot").strip().lower()
    verb = _H3_MOVE_VERBS.get(move, "holds a static shot")
    out = f"the camera {verb}"
    if move != "static shot":
        amp = (beat.get("amplitude") or "").strip().lower()
        spd = (beat.get("speed") or "").strip().lower()
        if amp in ("small", "large"):
            out += f" with {amp} amplitude"
        if spd in ("slow", "fast"):
            out += f" at {spd} speed"
    return out


def h3_clip_prompt(character: dict, setting: str, beats: list[dict],
                   durations: list[float], style_lock: str,
                   ref_pictures: bool = False, last_picture: bool = False,
                   clip_seconds: float | None = None) -> str:
    """Build ONE MiniMax H3 generation prompt covering `beats` (usually 2), in
    the official guide's grammar (skills/h3-prompt-writing, references/base-en):
    the I2VA alignment instruction on its own first line, then the three named
    fields — `integrated_multimodal_description` (all shots, one continuous
    field), `overall_soundscape`, `non_diegetic_music` — separated by blank
    lines. Inside the description: a style statement opening [Shot 1], numbered
    shots with 'At mm:ss.mmm, the camera cuts to', framing+angle+one movement
    per shot written as a natural English action, and cut times placed EXACTLY
    at the narration line boundaries (`durations` = each beat's spoken length,
    so the picture changes where the voiceover does). No quoted strings
    anywhere: the guide treats quoted text as a request to DRAW it.

    Two lessons from the first render baked in: the FULL room description is
    restated inside every shot block (stated once, the model kept the character
    but invented a new room after the first cut), and the no-text clause names
    what charts show instead of only banning writing.

    `ref_pictures=True` is the ref2va mode: <Picture 1> is the cast sprite
    (identity — described WITHOUT attribute text, because when the 2026-08-04
    test enumerated features that contradicted the sprite, the model followed
    the words over the image), and shot k's composition is bound to
    <Picture k+1>, its klein storyboard."""
    who = character.get("description") or character.get("visual_prompt") or \
        f"{character.get('name', 'the character')} the {character.get('kind', 'character')}"
    room = setting.strip().rstrip(".")
    style = style_lock.strip()
    shots: list[str] = []
    if ref_pictures:
        shots.append("The character in every shot is EXACTLY the character shown in "
                     "<Picture 1> — the same face, the same hair, the same glasses, the "
                     "same clothing, an identical character design, never redesigned.")
    t = 0.0
    for i, b in enumerate(beats):
        framing = b.get("framing") or "medium shot"
        angle = b.get("angle") or "eye-level"
        cam = _h3_camera(b)
        if i == 0:
            # Guide §4.1: the style statement opens [Shot 1]. Guide §3.1: anchor
            # the first frame, then move forward from it.
            anchor = (f"a composition matching <Picture 2>: {who} in {room}"
                      if ref_pictures else
                      f"{who} is in {room}, exactly as established in <Picture 1>, "
                      f"preserving the character design, the clothing, the room layout "
                      f"and the position of every object")
            shots.append(f"[Shot 1] {style} {anchor}. The framing is a {framing} from "
                         f"{angle}, and {cam}. {b['action']}")
        else:
            mm, ss = divmod(t, 60.0)
            comp = f"a composition matching <Picture {i + 2}>, " if ref_pictures else ""
            shots.append(f"[Shot {i + 1}] At {int(mm):02d}:{ss:06.3f}, the camera cuts to "
                         f"{comp}a {framing} from {angle}, inside the very same room as "
                         f"the previous shot — {room} — with the same character, the same "
                         f"clothing and the same objects in the same places, and {cam}. "
                         f"{b['action']}")
        t += durations[i] if i < len(durations) else 5.0
    if last_picture:
        # Guide §3.2: "The last frame must be reached by the final [Shot N] at the
        # end of the video", via progressively narrowing differences.
        shots[-1] += (" Toward the end of the shot the movement settles, and the "
                      "pose, spacing, framing and lighting converge exactly onto "
                      "the arrangement established by <Picture 2> at the final frame.")
    shots.append(_H3_NO_TEXT)

    out: list[str] = []
    if not ref_pictures:
        # Guide §2.1: the alignment instruction is the FIRST line, followed by one
        # blank line.
        if last_picture:
            # FL2VA — h3.i2v was given a last_frame, so both endpoints are real.
            # S.SS is the effective duration to exactly two decimals; N is the
            # index of the ACTUAL final shot.
            secs = clip_seconds if clip_seconds is not None else sum(durations)
            out.append("How the reference pictures align with the target video — "
                       "Picture 1 (from Shot 1) aligns with the 0.00-second mark of "
                       f"the target video; Picture 2 (from Shot {len(beats)}) aligns "
                       f"with the {secs:.2f}-second mark of the target video.")
        else:
            # I2VA — only first_frame is supplied; the FL2VA two-picture instruction
            # would promise a last frame the model never receives.
            out.append("For the target video, at 0.00 seconds into the target video, "
                       "<Picture 1> (from [Shot 1]) is fully referenced.")
        out.append("")
    out.append("integrated_multimodal_description: " + " ".join(shots))
    out.append("")
    out.append("overall_soundscape: Soft waxy crayon strokes brush against paper under a "
               "quiet room tone. Gentle rustles and light taps follow each object as it "
               "is moved.")
    out.append("")
    out.append("non_diegetic_music: N/A")
    return "\n".join(out)


def _translate_schema(n: int) -> dict:
    # minItems/maxItems pin the EXACT line count in the constrained decode —
    # without it gemma merges related lines (13 in → 6 out, one per symbol).
    return {"type": "object",
            "properties": {"lines": {"type": "array", "items": {"type": "string"},
                                     "minItems": n, "maxItems": n}},
            "required": ["lines"]}


def translate_lines(lines: list[str], cfg: dict) -> list[str] | None:
    """Translate the FINAL English narration lines to spoken Mandarin (zh-CN) for
    the Chinese audio/subtitle variant. Translation of the finished script — not a
    separate Chinese screenplay — so the zh track stays 1:1 with what's on screen.
    Returns a SAME-LENGTH list whose un-translatable entries are None; callers
    drop only the slide/scene that lost a line. Returns None only when NOTHING
    translated. (Until 2026-08-06 one bad line returned None for the whole list
    and cost the entire variant — line 7/16 took all seven slides with it.)"""
    if not lines:
        return []
    d = cfg.get("director", {})
    zh = cfg.get("zh", {})
    system = (
        "你是一位资深中文财经新闻主播兼撰稿人。下面是一段英文财经视频旁白，一行一句。"
        "请为每一行写出中文主播会怎么「说」这句话：自由地重组句式、用地道的口语化新闻播报语言，"
        "绝对不要逐字直译（不要翻译腔）。要求：(1) 事实、数字、涨跌方向必须与原文完全一致，"
        "不许增删事实；(2) 保留原文轻松、略带调侃的语气，可以用自然的中文幽默表达；"
        "(3) 数字按主播口播习惯写出（如 百分之八十二、五千一百四十亿美元、跌了近一成五）；"
        "(4) 股票代码和知名公司名保留英文（Nvidia、Tesla、S&P 500）；(5) 每行口播时长尽量与"
        "英文原句相当，不要明显变长；(6) 不加译注、不加拼音、输出行中不要带行号。"
        '只输出 JSON：{"lines": [...]}，一行对应一行：绝不合并或拆分行，行数必须与输入完全相同。'
    )
    # ONE LINE PER CALL, on purpose. Batch calls (all lines in one request) were
    # tried first and are stochastically unreliable on gemma: sometimes fine, but
    # ~half the time it merges related lines (13 in -> 6 out), pads a forced count
    # with junk ("z/1234567890-"), or rambles its thinking until num_ctx exhausts
    # (done_reason=length / 600s timeout). Per-line calls leave no room for any of
    # that, and one bad line only costs one retry, not the whole batch.
    zh_lines: list[str | None] = []
    last_ok: str | None = None
    for i, ln in enumerate(lines):
        fix = None
        # 3 attempts WITH BACKOFF: on 2026-08-04 a transient cloud blip failed
        # the same line twice back-to-back (it translated 3/3 cleanly minutes
        # later) and one bad line costs the ENTIRE zh track — a few seconds of
        # waiting is nothing against re-rendering the variant by hand.
        for attempt in range(3):
            if attempt:
                import time as _t
                _t.sleep(4 * attempt)
            fix, why = _translate_one(ln, system, zh, d, prev=last_ok)
            if fix:
                break
            # PRINT THE REASON. Until 2026-08-06 every failure mode collapsed to
            # one opaque "failed to translate" line and the cause had to be dug
            # out of the ollama journal after the fact.
            print(f"  [zh] line {i + 1}/{len(lines)} attempt {attempt + 1}/3: {why}")
        if fix is None:
            print(f"  [zh] line {i + 1}/{len(lines)} gave up — its slide drops "
                  f"from the 中文 cut, the rest still ships")
        else:
            last_ok = fix
        zh_lines.append(fix)
    n_bad = sum(x is None for x in zh_lines)
    if n_bad:
        print(f"  [zh] {len(lines) - n_bad}/{len(lines)} line(s) translated")
    return zh_lines if any(zh_lines) else None


_CJK = re.compile(r"[一-鿿]")


def _zh_line_ok(s: str) -> bool:
    """A sane spoken-Mandarin line: has CJK, no long digit runs (numbers must be
    written in hanzi for TTS), no leftover filler junk."""
    return bool(_CJK.search(s)) and not re.search(r"\d{5,}", s) and len(s.strip()) >= 2


def _zh_salvage(raw: str) -> dict | None:
    """Accept a bare Chinese sentence as the answer.

    The ollama CLOUD path only *requests* `format`; it does not grammar-constrain
    it. Measured 2026-08-06 over 32 calls: 12 of them (37.5%) returned the
    finished sentence as PLAIN TEXT instead of {"lines": [...]}. The translation
    itself was correct in every one of those 12 — only the envelope was missing,
    and `out[0]` on the absent key is what scored the line a failure. Misses are
    strongly content-correlated (one source line missed 3/4 times), so the retry
    ladder could not rescue them: that is exactly how 2026-08-06's line 7/16 lost
    six attempts in a row and killed the whole 中文 track.

    Deliberately narrow — one non-empty CJK line and no brace in sight. Anything
    that looks like a botched JSON object still goes back for the corrective
    retry rather than being guessed at."""
    t = re.sub(r"^```[a-zA-Z]*\n?", "", raw.strip())
    t = re.sub(r"\n?```$", "", t).strip()
    body = [x for x in (ln.strip() for ln in t.splitlines()) if x]
    if len(body) != 1 or "{" in t or "}" in t or not _CJK.search(t):
        return None
    return {"lines": [body[0]]}


def _translate_one(line: str, system: str, zh: dict, d: dict,
                   prev: str | None = None) -> tuple[str | None, str]:
    """Translate a single narration line. `prev` = the previous line's Chinese
    rendition, given as read-only context so consecutive lines flow naturally.

    Returns (line, "") on success or (None, reason) — the reason is printed by
    the caller instead of being swallowed."""
    ctx = f"上一句的中文（仅供衔接语气参考，绝不要输出它）：{prev}\n" if prev else ""
    payload = {
        "model": zh.get("model") or d.get("model", "gemma4:31b"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": f"{ctx}把下面这一句改写成中文口播，只输出这一句：\n{line}"}],
        # think MUST be False here: even at think:"low" gemma sporadically burns
        # the whole num_predict budget on thinking and returns EMPTY content
        # (done_reason=length). Rewriting one sentence needs no reasoning.
        "stream": False, "format": _translate_schema(1), "think": False,
        "keep_alive": zh.get("keep_alive", "2m"),
        "options": {"temperature": 0.3, "num_ctx": d.get("num_ctx", 8192), "num_predict": 1024},
    }
    try:
        out = _chat_json(payload, d, zh.get("timeout_s", 600), "zh",
                         salvage=_zh_salvage).get("lines", [])
    except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not out:
        return None, "empty `lines` array"
    txt = re.sub(r"^\s*\d+[\.、]\s*", "", str(out[0]).strip())
    if not _zh_line_ok(txt):
        return None, f"guard rejected {txt[:60]!r}"
    return txt, ""


# --- same-story guard for the cover headline -------------------------------
# The headline call has no memory, so a slow-burning story re-leads every day it
# is still the biggest number: "10-year yield ... highest since 2007" fronted SIX
# editions between 2026-08-18 and 09-24, the Fed hike two, the Nasdaq record two.
# The prompt now lists the recent front pages, but a prompt is a request, so the
# repeat is also caught in code.
#
# Plain word overlap cannot see those repeats: "30-year yield hits 5.33%, a
# 19-year high" and "Ten-year Treasury yield tops 5% for first time since 2007"
# share exactly one word. What they share is the STORY — a yield at a multi-year
# high — so titles are reduced to story terms first: the phrasings of one story
# collapse to one concept, numbers are dropped (they change every day while the
# story does not), and the verbs and glue that every headline shares are removed.
# Two titles are the same story when they share at least two terms and those
# cover most of the shorter title. Calibrated on the archive's real titles
# (tests/test_headline_repeat.py pins the pairs).
_STORY_PHRASES = [(re.compile(p), c) for p, c in (
    # a multi-year / record high, however it is phrased — before the tenor rule,
    # which would otherwise eat the "19-year" of "19-year high"
    (r"\b(?:highest|first time|first close) (?:since|in) (?:19|20)\d\d\b"
     r"|\b\d+-(?:year|month|week) highs?\b|\ball-time highs?\b|\brecords?\b"
     r"|\bnew highs?\b", "HIGH"),
    (r"\blowest since (?:19|20)\d\d\b|\b\d+-(?:year|month|week) lows?\b", "LOW"),
    (r"\b(?:\d+|two|five|ten|thirty)-year\b", ""),      # bond tenor
    (r"\b(?:treasury|treasuries|yields?|bonds?)\b", "YIELD"),
    (r"\b(?:rate[- ])?hik(?:e|es|ed|ing)\b|\brais(?:e|es|ed) rates\b", "HIKE"),
    (r"\b(?:rate[- ])?cuts?\b", "CUT"),
    (r"\bfed(?:eral reserve)?\b|\bfomc\b", "FED"),
    (r"\b(?:oil|crude|brent|wti)\b", "OIL"),
    (r"\b(?:war|strikes?|conflict|us-iran|iran)\b", "WAR"),
    (r"\bs&p(?: 500)?\b|\bspx\b|\bspy\b", "SPX"),
    (r"\bnasdaq(?:-100)?\b|\bqqq\b", "NASDAQ"),
    (r"\bdow(?: jones)?\b", "DOW"),
    (r"\b(?:chips?|chipmakers?|semis|semiconductors?)\b", "CHIPS"),
    (r"\b(?:jobs|payrolls?|employment)\b", "JOBS"),
    (r"\bnvidia\b", "nvda"), (r"\bmicrosoft\b", "msft"), (r"\bapple\b", "aapl"),
    (r"\bamazon\b", "amzn"), (r"\b(?:alphabet|google)\b", "goog"),
    (r"\btesla\b", "tsla"), (r"\boracle\b", "orcl"),
)]
# verbs of motion and the glue every market headline shares: they say THAT
# something moved, never WHICH story it is
_STORY_STOP = frozenset("""
a an the to of as on in at for and or from by with vs than its it is are be but
up down after before near above below about around over under back into amid
while first time since high highs low lows day days week points point percent
pct half quarter per new just still
january february march april may june july august september october november
december
hit hits tops top climbs climb rises rise rose jumps jump falls fall fell drops
drop slips slip slides slide surges surge soars soar sinks sink lifts lift
closes close closed ends end gains gain adds add loses lose sheds shed rallies
rally rallied bounces bounce rebounds rebound edges edge dips dip plunges plunge
tumbles tumble spikes spike leads lead snaps snap
""".split())
_REPEAT_AT = 0.6        # overlap of story terms at which two titles are one story
_REPEAT_WINDOW = 5      # how many of the most recent front pages the guard checks


def _story_terms(title: str) -> set[str]:
    t = (title or "").lower()
    for rx, concept in _STORY_PHRASES:
        t = rx.sub(f" {concept} ", t)
    out = set()
    for w in re.findall(r"[A-Za-z][A-Za-z&'-]*", t):
        w = w.strip("'-")
        if w.isupper():                 # a concept token from the table above
            out.add(w)
            continue
        if any(ch.isdigit() for ch in w) or w in _STORY_STOP or len(w) < 3:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def title_similarity(a: str, b: str) -> float:
    """0..1: how much two headlines are the SAME STORY (see the note above).
    Overlap of story terms over the shorter title's terms; below two shared
    terms it is 0 — a lone shared "SPX" is just the index, not the story."""
    ta, tb = _story_terms(a), _story_terms(b)
    shared = len(ta & tb)
    if shared < 2:
        return 0.0
    return shared / min(len(ta), len(tb))


def _mask_numbers(title: str) -> str:
    """A recent title as the MODEL sees it: the story, without its figures.

    The headline rules say every number must be copied from today's digest, and
    the recent front pages are appended to that same digest — so shown verbatim
    they would hand the model last week's "27,244" or "3.75-4.00%" as if they
    were today's. The story words are what the don't-repeat rule needs; the
    code guard (_story_terms) already ignores numbers."""
    return re.sub(r"\d[\d,.:%-]*", "#", title)


def _repeat_of(title: str, recent: list[str]) -> tuple[str, float] | None:
    """The most similar recent title at or above _REPEAT_AT, else None."""
    best = max(((r, title_similarity(title, r)) for r in recent[:_REPEAT_WINDOW]),
               key=lambda x: x[1], default=None)
    return best if best and best[1] >= _REPEAT_AT else None


_HEADLINE_SYS = (
    "You are the front-page editor of a cartoon financial news show. From the session "
    "digest, pick the SINGLE most important fact and write:\n"
    "- title: one eye-catching headline, max 60 characters, active voice; include the "
    "concrete number (percent move, price level) when there is one. No clickbait "
    "phrasing, no exclamation marks, not ALL CAPS. The title must restate the fact "
    "EXACTLY as the digest states it — same direction, same magnitude, same qualifier. "
    "Do not upgrade a rank to 'top'/'best' unless the digest says so, and never describe "
    "a disarmed/failed/blocked signal as strength; prefer the digest's own words over a "
    "punchier paraphrase (a 2026-08-03 headline turned 'RSI disarmed the buy trigger, "
    "ranked #2' into 'overtakes Microsoft as top AI bet' — wrong twice).\n"
    "- keywords: 2 to 4 bold cover words for a poster, each 1-3 words, concrete and "
    "punchy (e.g. \"NVDA -15%\", \"AI CAPEX FEAR\", \"BRENT $100\"). The first keyword "
    "must carry the headline fact.\n"
    "- art: one sentence describing a simple cartoon poster scene that fits the story, "
    "with concrete objects only and NO text/numbers in the drawing (e.g. \"a worried "
    "cartoon bear sliding down a jagged arrow while gold coins scatter\").\n"
    "When the digest ends with RECENT FRONT PAGES, those stories have already led the "
    "show. Do not lead with the same story as any of them, even with a new number (a "
    "yield a few basis points higher, a record a few points further, a second day of "
    "the same rate decision is still the same story). If today's single biggest fact "
    "is such a repeat, lead with the most important DIFFERENT development in the "
    "digest, and let the repeat live in the second or third keyword. Every rule above "
    "still applies to whatever you lead with.\n"
    'Answer ONLY JSON: {"title": "...", "keywords": ["...", "..."], "art": "..."}'
)


def headline(dec: dict, clips_meta: list[dict], cfg: dict,
             recent: list[str] | None = None) -> dict | None:
    """One gemma call that distills the finished briefing into a cover headline:
    {"title", "keywords", "art"}. Digest comes from the per-clip narration headlines
    (always present) plus the overview note when the decisions file is at hand, so
    it also works for archive backfills where only meta.json survives. Returns None
    on failure — the render carries on without a title/cover.

    `recent` is the slot's previous front pages, newest first (see
    generate.recent_titles). The model is told not to re-lead with any of them,
    and a title that still repeats one of the last _REPEAT_WINDOW (_repeat_of)
    gets ONE retry naming the collision. If the retry repeats too, or fails, the
    less repetitive of the two is kept: a repeated title is a cosmetic flaw, a
    missing one drops the cover."""
    d = cfg.get("director", {})
    lines = [f"{c.get('symbol')} ({c.get('bias') or 'n/a'}): {c.get('headline', '')}"
             for c in clips_meta if c.get("headline")]
    ov = (dec.get("notes", {}) or {}).get("_", "")
    digest = (f"Overview: {ov[:500]}\n" if ov else "") + "\n".join(lines[:12])
    if not digest.strip():
        return None
    recent = [str(t).strip() for t in (recent or []) if str(t).strip()]
    if recent:
        digest += ("\n\nRECENT FRONT PAGES (newest first; their figures are masked as "
                   "# — never use a number from here) — do not lead with these "
                   "stories again:\n" + "\n".join(f"- {_mask_numbers(t)}" for t in recent))
    messages = [{"role": "system", "content": _HEADLINE_SYS},
                {"role": "user", "content": digest}]
    payload = {
        "model": d.get("model", "gemma4:31b"),
        "messages": messages,
        # think False + format "json" (not a schema): the same gemma failure modes
        # documented in _translate_one / anatomy_ok apply here too.
        "stream": False, "format": "json", "think": False,
        "keep_alive": cfg.get("story", {}).get("keep_alive", "0"),
        "options": {"temperature": 0.7, "num_ctx": d.get("num_ctx", 8192),
                    "num_predict": 800},
    }

    def ask(msgs: list[dict]) -> dict | None:
        for _ in range(2):
            try:
                out = _chat_json(dict(payload, messages=msgs), d,
                                 d.get("timeout_s", 180), "headline")
                title = str(out.get("title", "")).strip()
                kws = [str(k).strip() for k in out.get("keywords", []) if str(k).strip()]
                art = str(out.get("art", "")).strip()
                if 8 <= len(title) <= 90 and kws and art:
                    return {"title": title, "keywords": kws[:4], "art": art}
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        return None

    head = ask(messages)
    hit = _repeat_of(head["title"], recent) if head else None
    if not hit:
        return head
    print(f"  [director] headline {head['title']!r} repeats {hit[0]!r} "
          f"(similarity {hit[1]:.2f}) — retrying once")
    retry = ask(messages + [
        {"role": "assistant", "content": json.dumps(head, ensure_ascii=False)},
        {"role": "user", "content":
            f"Your title \"{head['title']}\" leads with the same story as a recent "
            f"front page: \"{hit[0]}\". Lead with the most important DIFFERENT "
            "development in the digest instead, and move this story to the second "
            "or third keyword. Same rules, same JSON."}])
    if retry is None:
        return head
    again = _repeat_of(retry["title"], recent)
    if again:
        print(f"  [director] retry {retry['title']!r} still repeats {again[0]!r} — "
              "keeping the less repetitive title")
        return retry if again[1] < hit[1] else head
    return retry


_HEADLINE_ZH_SYS = (
    "你是中文财经海报的文案编辑。把下面的英文标题和海报关键词本地化成中文：\n"
    "- title：一句吸睛的中文标题（主播口吻，不要翻译腔），不超过 30 个字；"
    "股票代码和知名公司名保留英文。\n"
    "- keywords：逐个对应英文关键词的中文海报大字，每个不超过 8 个字，"
    "要短促有力；代码和数字保留原样（如 \"SOXL -5.5%\" 可原样保留或写成 "
    "\"SOXL 跳水5.5%\"）。数量与英文关键词一致。\n"
    '只输出 JSON：{"title": "...", "keywords": ["...", "..."]}'
)


# --- fact-check of the Chinese title -------------------------------------
# The localiser gets facts wrong in ways nothing downstream sees: "New Mexico"
# became 墨西哥州 (the Mexican state), and on 2026-08-12 "chips and clouds rip"
# shipped as 芯片云崩 — chips crashed — when they had rallied. Measured
# 2026-09-25 on all 46 archived editions (3 samples each, two independent blind
# judges, every verdict re-read against the whole recap file):
#   * a SECOND model checking the title against the recap, then ONE rewrite with
#     its critique, is what cuts the errors — the error rate before and after is
#     in the commit that added this;
#   * writing the Chinese title natively from the digest instead of translating
#     did NOT reduce errors and read worse (more 翻译腔, more over-length
#     titles), so translation stays and the check sits after it;
#   * glm-5.3-flash caught 97% of seeded single errors (wrong place, number,
#     direction, magnitude, invented cause) with no false alarms on clean titles.
# glm-5.3-flash ignores `format` and `think:false`: it reasons in prose and ENDS
# with the JSON verdict, so the verdict is salvaged from the tail.
_ZH_CHECK_SYS = (
    "你是财经新闻标题的事实核查员。对照【事实来源】（节目当天的研究资料，是唯一的事实依据），"
    "逐项核对【中文标题】和【海报关键词】里的每一个事实：\n"
    "1) 数字：必须能在来源中找到（格式不同但数值相同可以，比如 4.50% 与 4.5%）；\n"
    "2) 涨跌方向；\n"
    "3) 幅度用词是否与数字相称（例如小幅变动写成“暴跌”“狂飙”就是夸大）；限定词不能丢（盘中/一度/约/近/超）；\n"
    "4) 公司、产品、人名、地名、机构的中文译名是否正确、是否指对了对象；\n"
    "5) 是否出现来源中没有的说法、事件或因果关系。\n"
    "只报告真实的事实错误；措辞风格、标题好不好看都不算错误。拿不准就不要报。\n"
    '只输出 JSON：{"ok": true 或 false, "errors": ["每条错误一句话，指出原文和来源的出入"]}'
)
_ZH_CHECK_SCHEMA = {"type": "object", "required": ["ok", "errors"],
                    "properties": {"ok": {"type": "boolean"},
                                   "errors": {"type": "array", "items": {"type": "string"}}}}
_VERDICT = re.compile(r"\{[^{}]*\"ok\"\s*:\s*(?:true|false)[^{}]*\}", re.S)
_ZH_TITLE_MAX = 30


def _verdict_salvage(raw: str) -> dict | None:
    """The last {"ok": ...} object in a reply that wrapped it in reasoning."""
    for o in reversed(_VERDICT.findall(raw or "")):
        try:
            v = json.loads(o)
        except ValueError:
            continue
        if isinstance(v, dict) and "ok" in v:
            return {"ok": v["ok"], "errors": v.get("errors") or []}
    return None


def recap_source(dec: dict | None, limit: int = 24000) -> str:
    """Everything the edition's facts come from: the recap's notes AND its
    briefing. The notes alone are not enough — KOSPI's -4.37% on 2026-08-06 lived
    only in briefing._.catalysts, and a checker shown just the notes called the
    (correct) title unsupported."""
    dec = dec or {}
    notes = "\n\n".join(f"[{k}] {v}" for k, v in (dec.get("notes") or {}).items() if v)
    brief = json.dumps(dec.get("briefing") or {}, ensure_ascii=False)
    return (notes + ("\n\n" + brief if dec.get("briefing") else ""))[:limit]


def check_zh_title(zh: dict, source: str, en_title: str, cfg: dict) -> dict | None:
    """Fact-check a Chinese title + keywords against the recap with a SECOND
    model. {"ok", "errors", "model"}, or None when disabled or unavailable — a
    checker outage must never cost the cover, so the caller keeps the title."""
    tc = (cfg.get("zh", {}) or {}).get("title_check", {}) or {}
    if not tc.get("enabled", True) or not source:
        return None
    model = tc.get("model", "glm-5.3-flash:cloud")
    user = (f"【事实来源】\n{source}\n\n【英文封面标题】{en_title}\n\n"
            f"【中文标题】{zh['title']}\n【海报关键词】{json.dumps(zh['keywords'], ensure_ascii=False)}")
    payload = {"model": model,
               "messages": [{"role": "system", "content": _ZH_CHECK_SYS},
                            {"role": "user", "content": user}],
               "stream": False, "format": _ZH_CHECK_SCHEMA, "think": False, "keep_alive": "0",
               "options": {"temperature": 0.0, "num_ctx": int(tc.get("num_ctx", 32768)),
                           "num_predict": int(tc.get("num_predict", 8000))}}
    try:
        out = _chat_json(payload, {"model": model}, float(tc.get("timeout_s", 120)),
                         "zh_check", salvage=_verdict_salvage)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"  [director] zh title check unavailable ({exc}) — keeping the title unchecked")
        return None
    return {"ok": out.get("ok") is True,
            "errors": [str(e).strip() for e in (out.get("errors") or []) if str(e).strip()],
            "model": model}


def _zh_problems(zh: dict, verdict: dict | None) -> list[str]:
    """What a rewrite must fix: the checker's errors, plus the length rule, which
    is arithmetic and needs no model (7% of translated titles broke it)."""
    errs = list(verdict["errors"]) if verdict and not verdict["ok"] else []
    if verdict and not verdict["ok"] and not errs:
        errs = ["核查员判定有事实错误但没有说明"]
    if len(zh["title"]) > _ZH_TITLE_MAX:
        errs.append(f"标题有 {len(zh['title'])} 个字，超过 {_ZH_TITLE_MAX} 个字的上限")
    return errs


def headline_zh(head: dict, cfg: dict, recent_zh: list[str] | None = None,
                source: str | None = None) -> dict | None:
    """Localize the cover headline + poster keywords to Chinese (one gemma call).
    Returns {"title", "keywords"} or None (caller falls back to the EN cover).

    `recent_zh` is the slot's previous Chinese titles, newest first. Even when
    the English story is new, the localiser kept reaching for the same mould
    ("……，2007年以来最高！" three times in a row), so it sees them as phrasings
    to avoid — context only, the JSON it returns is unchanged.

    `source` (recap_source(dec)) turns on the fact-check: a second model checks
    the title against the recap, and if it finds an error — or the title is over
    30 characters — the localiser gets ONE rewrite with the critique, re-checked.
    The version with fewer problems ships (a tie keeps the first), and the check
    is attached as result["check"] for meta.json. Without a source, or when the
    checker is down, the title ships unchecked exactly as before."""
    d = cfg.get("director", {})
    src = json.dumps({"title": head["title"], "keywords": head["keywords"]},
                     ensure_ascii=False)
    recent_zh = [str(t).strip() for t in (recent_zh or []) if str(t).strip()]
    if recent_zh:
        src += ("\n\n最近几期的中文标题（仅供参考：不要套用它们的句式和模板，"
                "也不要照搬其中的说法；其中的数字已用 # 遮去）：\n"
                + "\n".join(f"- {_mask_numbers(t)}" for t in recent_zh))
    payload = {
        "model": d.get("model", "gemma4:31b"),
        "messages": [{"role": "system", "content": _HEADLINE_ZH_SYS},
                     {"role": "user", "content": src}],
        "stream": False, "format": "json", "think": False,
        "keep_alive": cfg.get("story", {}).get("keep_alive", "0"),
        "options": {"temperature": 0.4, "num_ctx": d.get("num_ctx", 8192),
                    "num_predict": 500},
    }
    def ask(msgs: list[dict]) -> dict | None:
        for _ in range(2):
            try:
                out = _chat_json(dict(payload, messages=msgs), d, d.get("timeout_s", 180),
                                 "headline_zh")
                title = str(out.get("title", "")).strip()
                kws = [str(k).strip() for k in out.get("keywords", []) if str(k).strip()]
                if _CJK.search(title) and 4 <= len(title) <= 60 and kws:
                    return {"title": title, "keywords": kws[:4]}
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        return None

    zh = ask(payload["messages"])
    if not zh or not source:
        return zh
    verdict = check_zh_title(zh, source, head["title"], cfg)
    problems = _zh_problems(zh, verdict)
    check = {"model": (verdict or {}).get("model"), "checked": verdict is not None,
             "ok": not problems, "errors": problems, "rewritten": False}
    if problems:
        print(f"  [director] zh title {zh['title']!r}: {len(problems)} problem(s) — "
              f"rewriting once: {problems[0][:80]}")
        fixed = ask(payload["messages"] + [
            {"role": "assistant", "content": json.dumps(zh, ensure_ascii=False)},
            {"role": "user", "content": "事实核查指出以下问题：\n"
             + "\n".join(f"- {p}" for p in problems)
             + "\n请改正这些问题后重写，其他规则不变，只输出 JSON。"}])
        if fixed:
            v2 = check_zh_title(fixed, source, head["title"], cfg)
            p2 = _zh_problems(fixed, v2)
            if len(p2) < len(problems):
                zh, check = fixed, {**check, "ok": not p2, "errors": p2, "rewritten": True,
                                    "first_errors": problems}
            else:
                check["rewrite_errors"] = p2
    return {**zh, "check": check}


_DESK_SYS = (
    "You are the narrator of a trading desk's own video briefing. The FACT SHEET below is a live "
    "snapshot of the operator's automated trading account: what it holds, what it made or lost, "
    "what the research layer decided today, and what changed since the last briefing.\n"
    "Write the spoken narration for ONE slide about the desk itself.\n"
    "VOICE: the internal trading-notes voice of the rest of this show — dry, specific, faintly "
    "self-deprecating, an operator talking about their own book. Never hype, never advice, never "
    "second person, no 'investors should'.\n"
    "STRUCTURE: exactly {n} lines, each TWO sentences, each 150 to 220 characters. Line 1: where "
    "the account stands — equity and the day's move. Middle: the open position, and what the "
    "engine did or refused to do since the last briefing. Last: the decision board — what is "
    "ranked long, and what that means for the next few hours.\n"
    "Write connected prose, not a read-out of the sheet: each line should say what the number "
    "MEANS for the book. Never use the words 'fact sheet'. No sentence may restate the one before "
    "it in other words — if a line has nothing left to add, make it shorter, not padded.\n"
    "HARD RULES: (1) Every number, ticker, percent and dollar figure must be COPIED CHARACTER FOR "
    "CHARACTER from the fact sheet. Do not compute, re-round, convert or infer any number — if a "
    "number is not on the sheet you may not say it. (2) Do not invent positions, fills, or "
    "decisions. (3) No markdown, no bullets, no line numbers — plain spoken sentences.\n"
    'Answer ONLY JSON: {{"lines": ["...", "..."]}}'
)

_NUM_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


def _num_key(tok: str) -> str:
    return tok.replace("$", "").replace(",", "").replace("+", "").rstrip("%").lstrip("-")


def _allowed_numbers(facts: list[str]) -> set[str]:
    ok: set[str] = set()
    for f in facts:
        for m in _NUM_RE.findall(f):
            v = _num_key(m)
            ok.add(v)
            if "." in v:                    # $5,031.30 and $5,031.3 are the same number
                ok.add(v.rstrip("0").rstrip("."))
    return ok


def bad_numbers(line: str, allowed: set[str]) -> list[str]:
    """Digits in `line` that were NOT on the fact sheet.

    Deliberately strict: a re-rounded number ("roughly $5,000"), a derived one
    ("cash is 5.5% of the account") and a truncated one ("up 97 dollars") are all
    rejected even when they happen to be arithmetically true, because the model
    is not the thing that gets to do arithmetic about a real account.
    """
    out = []
    for m in _NUM_RE.findall(line):
        v = _num_key(m)
        if v in allowed or v.rstrip("0").rstrip(".") in allowed:
            continue
        out.append(m)
    return out


def desk_narration(facts: list[str], cfg: dict, n: int = 4) -> list[str] | None:
    """Narration for the DESK slide, written from a fact sheet of live account
    numbers — and refused if it invents any of them.

    This is the one slide where gemma is handed raw account figures instead of a
    Claude-written note, so the output is checked, not trusted: every attempt is
    scanned for digits that are not on the sheet, and a single bad number throws
    the whole take away. Returns None once the retries are spent, and the caller
    ships the deterministic script instead (briefing/desk.py:fallback_lines).
    """
    if not facts:
        return None
    d = cfg.get("director", {})
    allowed = _allowed_numbers(facts)
    sheet = "FACT SHEET\n" + "\n".join(f"- {f}" for f in facts)
    payload = {
        "model": d.get("model", "gemma4:31b"),
        "messages": [{"role": "system", "content": _DESK_SYS.format(n=n)},
                     {"role": "user", "content": sheet}],
        # think False + format "json": same gemma failure modes as _translate_one.
        "stream": False, "format": "json", "think": False,
        "keep_alive": cfg.get("story", {}).get("keep_alive", "0"),
        "options": {"temperature": d.get("temperature", 0.8), "num_ctx": d.get("num_ctx", 8192),
                    "num_predict": 900},
    }
    for attempt in range(3):
        try:
            out = _chat_json(payload, d, d.get("timeout_s", 180), "desk")
            lines = [str(x).strip() for x in out.get("lines", []) if str(x).strip()]
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"  [desk] narration attempt {attempt + 1} failed ({type(exc).__name__})")
            continue
        if not lines:
            continue
        bad = {i: bad_numbers(ln, allowed) for i, ln in enumerate(lines)}
        offenders = sorted({b for v in bad.values() for b in v})
        if not offenders:
            return lines
        print(f"  [desk] narration attempt {attempt + 1} rejected — invented number(s): "
              f"{', '.join(offenders[:6])}")
    return None


_BBOX_PROMPT = (
    "Find the cartoon person in this image. Answer ONLY JSON: "
    '{"bbox": [x1, y1, x2, y2]} with coordinates normalized to 0-1000.'
)

_DETECT_PROMPT = (
    # DETECTION-style, not enumerate/count: asked to enumerate or count limbs, both
    # gemma4:31b and qwen3-vl:32b snap to the "humans have 2 arms" prior and report
    # a DIFFERENT pair of hands on each run of a 3-handed frame (megaphone+pointing,
    # then megaphone+hanging, ...) — always exactly 2. Framing it as object
    # detection with coordinates breaks that prior (calibrated 2026-07-25 on the
    # Anchor's two 3-handed storyboards + wan frames, with clean-frame controls).
    "This is a hand-drawn cartoon. DETECT every HAND of the character, like an object "
    "detector: include hands holding things, pointing hands, resting hands, hands at the "
    "waist or side, partially hidden hands, floating hands. Also DETECT every SHOE/FOOT. "
    # No per-point "desc" field: the verdict only ever reads x/y, and the free
    # text roughly tripled the output — truncated/unparseable JSON was the QC's
    # top failure mode (~6 silent band-skips across 2026-08-03's renders alone).
    'Output ONLY JSON: {"hands": [{"x": <int>, "y": <int>}], '
    '"feet": [{"x": <int>, "y": <int>}]} '
    "with normalized 0-1000 coordinates. Do not assume how many there should be — "
    "report exactly what is drawn. "
    # A band that legitimately contains no limbs is the COMMON case once the frame is
    # split 2 and 3 ways, and gemma4:31b-cloud answered that case in prose despite
    # format:json (measured 2026-08-14). Name the empty case explicitly.
    'If you detect none, still return that same JSON with empty arrays — '
    '{"hands": [], "feet": []} — and never answer in prose.'
)

# The prose gemma4 falls back to when it detects nothing. Deliberately narrow: it must
# not swallow a genuinely malformed reply, which still has to raise so the band is
# skipped loudly rather than silently counted as empty.
_NO_DETECTIONS = re.compile(
    r"(did\s*not|didn't|could\s*not|couldn't|unable\s+to|no)\s+"
    r"(find|detect|locate|bounding\s+box|detections?)", re.I)


# Cloud calls carry the image inline as base64, which inflates it by 4/3, so the
# WIRE size — not the PNG size — is what has to fit. 5 MB by default (user directive
# 2026-08-14); rv["max_upload_mb"] overrides.
_MAX_UPLOAD_MB = 5.0


def _fit_for_upload(img_bytes: bytes, budget_mb: float = _MAX_UPLOAD_MB) -> bytes:
    """Shrink one band until its base64 payload fits `budget_mb`.

    Bands are deliberately upscaled 2x before being sent, because small dark extra
    hands are only visible after crop+zoom — so this steps the long edge DOWN toward
    the band's native width rather than abandoning the zoom, and only falls back to
    JPEG if even native resolution will not fit. At the 2560x1408 canvas the bands
    weigh 1.0-2.6 MB on the wire and pass through untouched; this is the guard that
    keeps a larger canvas (or a 4K master) from silently exceeding the limit.
    """
    import io
    from PIL import Image

    raw_budget = int(budget_mb * 1024 * 1024 * 3 / 4)   # base64 inflates ~4/3
    if len(img_bytes) <= raw_budget:
        return img_bytes

    im = Image.open(io.BytesIO(img_bytes))
    cand = im
    for long_edge in (4096, 3072, 2560):
        if max(im.size) > long_edge:
            s = long_edge / max(im.size)
            cand = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        cand.save(buf, "PNG", optimize=True)
        if buf.tell() <= raw_budget:
            return buf.getvalue()
    # Lossless has run out of room. JPEG on a flat cartoon/studio frame costs far less
    # detail than dropping below native resolution would.
    buf = io.BytesIO()
    cand.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def _vqc_chat(model: str, prompt: str, png_bytes: bytes, rv: dict) -> dict:
    png_bytes = _fit_for_upload(png_bytes, float(rv.get("max_upload_mb", _MAX_UPLOAD_MB)))
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt,
                             "images": [base64.b64encode(png_bytes).decode()]}],
               "stream": False, "format": "json",
               "keep_alive": rv.get("keep_alive", "2m"),
               "options": {"temperature": 0}}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=rv.get("timeout_s", 180)))
    content = resp["message"]["content"]
    try:
        return _parse(content)
    except ValueError:
        # "I found nothing" is a NORMAL detector answer, but gemma4:31b-cloud says it
        # in prose ("I did not find any bounding box detections for hands, and feet.")
        # and ignores format:json for that one case — measured 2026-08-14, done_reason
        # "stop", so it is a formatting quirk and not truncation. _parse needs a
        # literal {...} and raised, which made anatomy_ok skip the band.
        #
        # Skipping an EMPTY band costs no accuracy — it contributes zero points either
        # way. It costs the `calls` counter, and that is the real bug: if every band
        # comes back empty, calls stays 0, anatomy_ok returns None, and None counts as
        # a PASS. A frame with genuinely nothing detectable would then pass for the
        # wrong reason. Read the reply as the empty result it plainly is.
        if _NO_DETECTIONS.search(content):
            return {"hands": [], "feet": []}
        raise


# v4: BAND-TILED detection. Horizontal splits of the padded character box, counted
# GLOBALLY after mapping every point back to frame coordinates.
#
# Why, measured on 2026-07-30 against the two frames that shipped visible defects
# (the Anchor's third leg, Pip's third hand):
#
#   * v3's single tight crop passed both. At temperature 0 that is one deterministic
#     answer, so re-running it can never disagree with itself.
#   * Re-framing did NOT help. Tight crop, a 30%-padded crop and the untouched full
#     frame ALL returned exactly 2 hands and 2 feet — raw, before any dedup — and the
#     descriptions calmly listed the two limbs a normal person has ("foot on the
#     floor", "foot on the stool's rung") while omitting the third. The detection
#     framing does not break the 2-limb prior on these frames; the prior wins.
#   * What DOES break it is denying the model a whole character to reason about. Split
#     into 3 bands, the band containing only feet reported feet=3 — correct. Split
#     into 2, Pip's hands summed to 3 — correct. Neither split catches both, so both
#     are run.
#
# Bands OVERLAP, so a limb near a boundary is seen twice and naive summing gives
# nonsense (a 2-handed Anchor summed to hands=4). Every point is therefore mapped from
# tile-normalised into FRAME-normalised coordinates and deduped once, globally, before
# the counts are compared — that is what makes cross-band totals meaningful.
_DETECT_BANDS = (2, 3)
_BAND_OVERLAP = 0.04        # fraction of the box added to each inner band edge
_BBOX_PAD = 0.30            # detached limbs sit outside a tight "person" box
_GLOBAL_TOL = 60            # frame-normalised 0-1000; > tile-mapping jitter, < limb spacing

# Which channels may FAIL a frame. Feet only, and that is a measured decision rather
# than caution — on 2026-07-30, banded counts on the two Pip frames came out INVERTED:
#
#   3-handed Pip (defect): only ever 2 distinct hands, at every tolerance from 20 to
#     110. The model reports "hand holding a red apple" + "resting hand at the side"
#     and never sees the SECOND hand cupping the apple at all.
#   clean Pip (2 hands): 3 distinct hands, at every tolerance — a spurious point at
#     the sleeve/bag strap alongside the two real ones.
#
# So the hand channel misses the real defect AND fires on the good frame; no threshold
# separates them, because the detections themselves are wrong. Blocking on it would
# refuse legitimate uploads while still passing the thing it was added to catch.
# Feet behaved correctly in both directions on the same test (3 on the three-legged
# Anchor, 2 on its clean replacement), so feet block and hands are reported only.
#
# 2026-07-31, measured again and WEAKER than that: three fixtures made by
# compositing a real trouser-leg-and-shoe into a published frame — twice beside the
# character's own pair, once alone on open floor — were each reported as exactly two
# feet by every band that saw them, and all three passed. Raw points were 4 (the same
# real pair seen by two bands) and deduping was not the cause; the third shoe was
# never detected, at any tolerance. So the foot channel carries the same two-limb
# prior, and this gate is a backstop that has caught one real defect rather than a
# guarantee.
#
# Extra HANDS — and, on this evidence, extra FEET the model declines to see — are
# what the contact sheet is for. See publish_youtube.preflight_qc.
_BLOCK_ON = ("feet",)


def _to_frame_pts(pts: list, x0: float, y0: float, w: float, h: float) -> list:
    """Map tile-normalised 0-1000 points into FRAME-normalised 0-1000 points.

    (x0, y0, w, h) is the tile's rectangle in 0..1 frame units. Without this every
    band reports coordinates relative to itself, so the same limb seen in two
    overlapping bands looks like two different limbs.
    """
    out = []
    for p in pts:
        if not isinstance(p, dict):
            continue
        try:
            px, py = float(p.get("x", -1)), float(p.get("y", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= px <= 1000 and 0 <= py <= 1000):
            continue
        out.append({"x": (x0 + px / 1000.0 * w) * 1000.0,
                    "y": (y0 + py / 1000.0 * h) * 1000.0,
                    "desc": p.get("desc")})
    return out


def _dedupe_pts(pts: list, tol: int = 45) -> list:
    keep: list[dict] = []
    for q in pts:
        if isinstance(q, dict) and all(abs(q.get("x", 0) - k.get("x", 0)) +
                                       abs(q.get("y", 0) - k.get("y", 0)) > tol for k in keep):
            keep.append(q)
    return keep


def anatomy_ok(image: Path | str, cfg: dict) -> bool | None:
    """Vision QC for generated frames: does the character have normal anatomy?
    Returns True/False, or None when the check itself fails — callers must treat
    None as a pass, never as a defect.

    v4 (2026-07-30) — BAND-TILED detection on qwen3-vl:32b. Stage 1 bounding-boxes the
    character; stage 2 splits the padded box into horizontal bands, runs DETECTION-style
    hand/foot localization on each, maps every point back into frame coordinates, and
    dedupes ONCE globally. More than two hands or two feet fails the frame. The verdict
    always comes from OUR point counts, never a model 'anatomy_ok' opinion. Don't
    downsize the model: qwen3-vl:4b misses limbs.

    Why bands: v3's single crop passed both frames that shipped visible defects, and so
    did a tight crop, a wide crop and the full frame — every one returned exactly 2
    hands and 2 feet. Showing the model a whole character invites the 2-limb prior;
    showing it a band that contains only feet does not. Full measurements and the
    double-counting trap at _DETECT_BANDS.

    Cost is one bbox call plus sum(bands) detection calls — 6 by default, against v3's
    2. Tune with story.review.bands (e.g. [3] to halve it, [2,3,4] for more recall).
    """
    rv = cfg.get("story", {}).get("review", {})
    model = rv.get("model") or cfg.get("director", {}).get("model", "gemma4:31b")
    bands_list = [int(b) for b in (rv.get("bands") or _DETECT_BANDS)]
    pad = float(rv.get("bbox_pad", _BBOX_PAD))
    ov = float(rv.get("band_overlap", _BAND_OVERLAP))
    tol = int(rv.get("global_tol", _GLOBAL_TOL))
    try:
        from PIL import Image
        import io
        base = Image.open(image).convert("RGB")
        try:
            bb = _vqc_chat(model, _BBOX_PROMPT, Path(image).read_bytes(), rv).get("bbox")
            bx1, by1, bx2, by2 = [max(0.0, min(1.0, float(v) / 1000)) for v in bb]
            X1, Y1 = max(0.0, bx1 - pad), max(0.0, by1 - pad)
            X2, Y2 = min(1.0, bx2 + pad), min(1.0, by2 + pad)
        except (TypeError, ValueError, KeyError, IndexError):
            X1, Y1, X2, Y2 = 0.0, 0.0, 1.0, 1.0   # no usable bbox — band the whole frame
        if X2 - X1 < 0.05 or Y2 - Y1 < 0.05:
            X1, Y1, X2, Y2 = 0.0, 0.0, 1.0, 1.0

        hands_all: list = []
        feet_all: list = []
        calls = 0
        for bands in bands_list:
            for b in range(bands):
                ty1 = Y1 + (Y2 - Y1) * b / bands - (ov if b else 0.0)
                ty2 = Y1 + (Y2 - Y1) * (b + 1) / bands + (ov if b < bands - 1 else 0.0)
                ty1, ty2 = max(0.0, ty1), min(1.0, ty2)
                box = (int(X1 * base.width), int(ty1 * base.height),
                       int(X2 * base.width), int(ty2 * base.height))
                im = base.crop(box)
                if im.width < 64 or im.height < 64:
                    continue
                im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "PNG")
                try:
                    out = _vqc_chat(model, _DETECT_PROMPT, buf.getvalue(), rv)
                except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    # One flaky band must not void the frame: before this guard a
                    # single unparseable reply aborted anatomy_ok entirely (-> None
                    # -> counted as a PASS), discarding the bands that did answer.
                    print(f"  [review] band check failed ({type(exc).__name__}) — "
                          "skipping this band")
                    continue
                hands_all += _to_frame_pts(out.get("hands") or [], X1, ty1, X2 - X1, ty2 - ty1)
                feet_all += _to_frame_pts(out.get("feet") or [], X1, ty1, X2 - X1, ty2 - ty1)
                calls += 1
        if not calls:
            return None
        hands = len(_dedupe_pts(hands_all, tol))
        feet = len(_dedupe_pts(feet_all, tol))
        block_on = tuple(rv.get("block_on") or _BLOCK_ON)
        over = {"hands": hands > 2, "feet": feet > 2}
        if any(over.values()):
            fail = [k for k in block_on if over.get(k)]
            noted = [k for k, v in over.items() if v and k not in block_on]
            msg = f"  [review] {Path(image).name}: {hands} hand(s) / {feet} foot|feet"
            if noted:                            # say it, but do not act on it
                msg += f" — extra {'/'.join(noted)} NOT blocking (unreliable channel)"
            print(msg)
            if fail:
                return False
        return True
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"  [review] anatomy check failed ({type(exc).__name__}: {exc})")
        return None


def direct_briefing(scenes: list[dict], cfg: dict) -> dict:
    """Sequentially (concurrency 1) direct each scene. Returns {symbol: out|None}."""
    if not cfg.get("director", {}).get("enabled"):
        return {}
    model = cfg["director"].get("model", "gemma4:31b-cloud")
    print(f"  [director] {model} directing {len(scenes)} scene(s) …")
    result = {}
    for sc in scenes:
        out = direct_scene(sc, cfg)
        if out:
            print(f"  [director] {sc['symbol']}: “{out['title']}” · {len(out['narration'].split())}w"
                  f" · {len(out['elements'])} element(s)")
        result[sc["symbol"]] = out
    return result
