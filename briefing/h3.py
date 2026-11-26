"""MiniMax H3 (ref2va) video engine for the flagship recap.

Drives the ISOLATED ComfyUI v0.30 worktree (the production ComfyUI on :8188 is
v0.28 and has no H3 nodes) over HTTP, exactly like briefing/comfy.py drives the
production server. One clip = one H3 generation covering 1-2 beats with a
timestamped internal cut, conditioned on the cast sprite (<Picture 1>, identity)
plus each beat's klein storyboard (<Picture 2>..., composition) — all proven
2026-08-04 (see the _h3_tests archive and the research-to-video-pipeline memory).

VRAM discipline: the H3 server CANNOT cohabit with the production ComfyUI's
models or the qwen3-vl reviewer. The caller frees production VRAM before
ensure_server() and MUST stop_server() before any anatomy_ok() call.

API gotcha (measured): the ref_images Autogrow input must be sent in the NESTED
dict form {"ref_images": {"ref_image_1": [...]}} — the flat form passes /prompt
validation and then throws unexpected-keyword inside execute().
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import generate as G

_SRV: subprocess.Popen | None = None


def _model_ref(g: dict, h: dict) -> list:
    """The MODEL link the sampler should consume, with EasyCache spliced in
    between UNETLoader and its consumers when `h3.easycache.enabled` is set.

    Both BasicScheduler and BasicGuider must read the SAME link: EasyCache
    returns a cloned patcher, and feeding the guider the cached model while the
    scheduler reads the raw one silently drops the acceleration."""
    ec = h.get("easycache") or {}
    if not ec.get("enabled"):
        return ["u", 0]
    g["ec"] = {"class_type": "EasyCache", "inputs": {
        "model": ["u", 0],
        "reuse_threshold": float(ec.get("reuse_threshold", 0.2)),
        "start_percent": float(ec.get("start_percent", 0.15)),
        "end_percent": float(ec.get("end_percent", 0.95)),
        "verbose": bool(ec.get("verbose", True))}}
    return ["ec", 0]


def hcfg(cfg: dict) -> dict:
    h = cfg.setdefault("h3", {})
    h.setdefault("dir", "/mnt/sandisk/ComfyUI-h3")
    h.setdefault("python", "/mnt/sandisk/ComfyUI-h3/.venv/bin/python")
    h.setdefault("host", "127.0.0.1")
    h.setdefault("port", 8189)
    # fl2va: each clip PIXEL-ANCHORS on its first beat's identity-locked klein
    # storyboard — strongest character fidelity, chosen for production after the
    # ref2va trial showed clothing/set drifting BETWEEN clips. ref2va: up to 9
    # <Picture i> refs (sprite identity + per-beat compositions) — softer
    # binding, kept for revisiting.
    h.setdefault("variant", "fl2va")
    h.setdefault("unet", f"minimax_h3_{h['variant']}_pruned_int8_convrot.safetensors")
    h.setdefault("clip", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
    h.setdefault("vae", "minimax_h3_video_vae_fp16.safetensors")
    h.setdefault("audio_vae", "minimax_h3_audio_vae_fp32.safetensors")
    # 960x544 + sage is the measured Wan-parity speed point; split_beats takes
    # the result to the 1280x704 canvas every HUD overlay is drawn for.
    h.setdefault("width", 960)
    h.setdefault("height", 544)
    h.setdefault("steps", 20)
    h.setdefault("sampler", "res_multistep")
    h.setdefault("scheduler", "simple")
    h.setdefault("ref_image_size", "match")   # "max" = better identity, much slower
    h.setdefault("retries", 1)                # QC re-rolls per clip
    h.setdefault("start_timeout_s", 240)
    h.setdefault("gen_timeout_s", 2400)
    # EasyCache — ComfyUI's OWN node (comfy_extras/nodes_easycache.py), not a
    # third-party patch. It skips a denoise step when the model's rate of change
    # is small, so it trades a little fidelity for wall-clock. Off by default:
    # every clip is a person-shaped crayon character and cache artifacts show up
    # first on faces and hands, so this is opt-in until an A/B says otherwise.
    ec = h.setdefault("easycache", {})
    ec.setdefault("enabled", False)
    ec.setdefault("reuse_threshold", 0.2)     # node defaults; higher = faster/looser
    ec.setdefault("start_percent", 0.15)
    ec.setdefault("end_percent", 0.95)
    ec.setdefault("verbose", True)            # logs steps skipped into h3_server.log
    h.setdefault("style_lock", (
        "2D-animated, a children's crayon drawing: rough waxy crayon strokes, loose "
        "wobbly hand-drawn outlines, warm cream paper background, sparse patches of "
        "color. This exact hand-drawn style, the same character design, the same "
        "clothing and the same room persist through every shot and every cut. Only "
        "the objects already visible in the reference pictures exist; no new "
        "furniture, rooms, posters or decorations appear."))
    return h


def _base(cfg: dict) -> str:
    h = hcfg(cfg)
    return f"http://{h['host']}:{int(h['port'])}"


def _up(cfg: dict) -> bool:
    try:
        urllib.request.urlopen(f"{_base(cfg)}/system_stats", timeout=3)
        return True
    except OSError:
        return False


def ensure_server(cfg: dict, log=print) -> bool:
    """Start (or attach to) the H3 worktree server. Caller must have freed
    production ComfyUI VRAM first (comfy.free_memory)."""
    global _SRV
    if _up(cfg):
        return True
    h = hcfg(cfg)
    d = Path(h["dir"])
    if not (d / "main.py").exists():
        log(f"  [h3] server dir missing: {d}")
        return False
    # --use-sage-attention only when the wheel actually imports under the
    # server's python: sage is a compiled extension linked against a specific
    # torch ABI, so a torch upgrade silently invalidates it — and a hardcoded
    # flag would then take the whole server down instead of costing ~11% speed.
    sage_ok = subprocess.run(
        [h["python"], "-c", "import sageattention"],
        capture_output=True).returncode == 0
    flags = ["--use-sage-attention"] if sage_ok else []
    log(f"  [h3] starting ComfyUI worktree on :{h['port']} "
        f"({'sage attention' if sage_ok else 'PLAIN sdpa — sageattention not importable'}) …")
    _SRV = subprocess.Popen(
        [h["python"], "main.py", "--listen", h["host"], "--port", str(h["port"]),
         "--disable-auto-launch"] + flags,
        cwd=d, stdout=open(d / "h3_server.log", "w"), stderr=subprocess.STDOUT)
    deadline = time.time() + float(h["start_timeout_s"])
    while time.time() < deadline:
        if _up(cfg):
            return True
        time.sleep(2)
    log("  [h3] server did not come up")
    return False


def stop_server(cfg: dict) -> None:
    """Kill the H3 server and wait for the VRAM to actually clear — the
    qwen3-vl reviewer loads next and a half-dead server starves it."""
    global _SRV
    h = hcfg(cfg)
    if _SRV and _SRV.poll() is None:
        _SRV.terminate()
    subprocess.run(["pkill", "-f", f"{h['dir']}/.venv"], check=False)
    _SRV = None
    for _ in range(15):
        if not _up(cfg):
            break
        time.sleep(1)
    time.sleep(3)                       # driver teardown lag


def _stage_frame(d: Path, src: Path, tag: str, size: tuple[int, int] | None = None) -> str:
    """Copy a keyframe into ComfyUI's input/ and return its name.

    `size` forces an exact resize to the H3 canvas. That matters for the LAST
    frame specifically: MiniMaxH3ImageToVideo stretches first_frame to the
    canvas ("disabled") but cover-CROPS last_frame ("center"). klein renders
    1280x704 (1.818) while the canvas is 960x544 (1.765), so an unresized pair
    would arrive framed ~3% apart and the model would render a slow phantom
    zoom that reads as camera drift. Pre-resizing makes the crop a no-op."""
    name = f"h3{tag}_{uuid.uuid4().hex[:8]}_{Path(src).name}"
    if size is None:
        shutil.copy(src, d / "input" / name)
    else:
        _resize_image(src, d / "input" / name, size)
    return name


def _resize_image(src: Path, dst: Path, size: tuple[int, int]) -> None:
    """Exact resize to `size` (PIL imported lazily so the graph builders stay
    importable — and patchable — in environments without Pillow)."""
    from PIL import Image
    Image.open(src).convert("RGB").resize(size, Image.LANCZOS).save(dst)


def i2v(cfg: dict, prompt: str, first_frame: Path, frames: int,
        seed: int, dest: Path, log=print, last_frame: Path | None = None) -> bool:
    """H3 keyframe generation.

    `last_frame=None` (default) is I2VA: the clip opens EXACTLY on
    `first_frame` (the first beat's klein storyboard) — identity and set are
    pixel-anchored, later shots hold them via the prompt's room restatement.

    Passing `last_frame` makes it a true FL2VA generation — the mode the
    `minimax_h3_fl2va_*` checkpoint is named for — pinning both endpoints so
    drift inside the clip is bounded and, when the caller chains clip N's last
    frame to clip N+1's first, drift ACROSS clips is eliminated too. The prompt
    must then carry the guide's two-picture instruction (director.h3_clip_prompt
    with last_picture=True), because the node hands both images to the text
    encoder in order: <Picture 1> = first, <Picture 2> = last. Silent video out."""
    h = hcfg(cfg)
    d = Path(h["dir"])
    (d / "input").mkdir(exist_ok=True)
    canvas = (int(h["width"]), int(h["height"]))
    name = _stage_frame(d, first_frame, "ff")
    lname = _stage_frame(d, last_frame, "lf", size=canvas) if last_frame else None
    prefix = f"h3_{uuid.uuid4().hex[:10]}"
    g = {
        "u": {"class_type": "UNETLoader", "inputs": {
              "unet_name": h.get("unet_fl2va",
                                 "minimax_h3_fl2va_pruned_int8_convrot.safetensors"),
              "weight_dtype": "default"}},
        "c": {"class_type": "CLIPLoader", "inputs": {
              "clip_name": h["clip"], "type": "minimax", "device": "default"}},
        "v": {"class_type": "VAELoader", "inputs": {"vae_name": h["vae"]}},
        "img": {"class_type": "LoadImage", "inputs": {"image": name}},
        "cond": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
                 "clip": ["c", 0], "vae": ["v", 0], "prompt": prompt,
                 "width": int(h["width"]), "height": int(h["height"]),
                 "length": int(frames), "first_frame": ["img", 0]}},
        "n": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        # (fl2va: the last-frame LoadImage + wiring is spliced in below)
        "ks": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": h["sampler"]}},
        "sch": {"class_type": "BasicScheduler", "inputs": {
                "model": ["u", 0], "scheduler": h["scheduler"],
                "steps": int(h["steps"]), "denoise": 1.0}},
        "gd": {"class_type": "BasicGuider", "inputs": {
               "model": ["u", 0], "conditioning": ["cond", 0]}},
        "s": {"class_type": "SamplerCustomAdvanced", "inputs": {
              "noise": ["n", 0], "guider": ["gd", 0], "sampler": ["ks", 0],
              "sigmas": ["sch", 0], "latent_image": ["cond", 1]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["s", 0], "vae": ["v", 0]}},
        "vid": {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "fps": 24}},
        "sav": {"class_type": "SaveVideo", "inputs": {
                "video": ["vid", 0], "filename_prefix": prefix,
                "format": "auto", "codec": "auto"}},
    }
    if lname:
        g["img2"] = {"class_type": "LoadImage", "inputs": {"image": lname}}
        g["cond"]["inputs"]["last_frame"] = ["img2", 0]
    m = _model_ref(g, h)
    g["sch"]["inputs"]["model"] = m
    g["gd"]["inputs"]["model"] = m
    return _queue_and_wait(cfg, g, prefix, dest, log)


def ref2va(cfg: dict, prompt: str, ref_paths: list[Path], frames: int,
           seed: int, dest: Path, log=print) -> bool:
    """One H3 generation: refs in order (<Picture 1> = identity sprite,
    <Picture 2>... = per-beat storyboards), silent video out (H3's native audio
    is discarded — narration is muxed by the compositor)."""
    h = hcfg(cfg)
    d = Path(h["dir"])
    (d / "input").mkdir(exist_ok=True)
    names = []
    for p in ref_paths:
        name = f"h3ref_{uuid.uuid4().hex[:8]}_{Path(p).name}"
        shutil.copy(p, d / "input" / name)
        names.append(name)
    prefix = f"h3_{uuid.uuid4().hex[:10]}"

    g = {
        "u": {"class_type": "UNETLoader", "inputs": {
              "unet_name": h.get("unet_ref2va",
                                 "minimax_h3_ref2va_pruned_int8_convrot.safetensors"),
              "weight_dtype": "default"}},
        "c": {"class_type": "CLIPLoader", "inputs": {
              "clip_name": h["clip"], "type": "minimax", "device": "default"}},
        "v": {"class_type": "VAELoader", "inputs": {"vae_name": h["vae"]}},
        "va": {"class_type": "VAELoader", "inputs": {"vae_name": h["audio_vae"]}},
        "cond": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                 "clip": ["c", 0], "vae": ["v", 0], "audio_vae": ["va", 0],
                 "prompt": prompt, "width": int(h["width"]), "height": int(h["height"]),
                 "length": int(frames), "ref_image_size": h["ref_image_size"],
                 # NESTED form — the flat form fails inside execute()
                 "ref_images": {f"ref_image_{i + 1}": [f"img{i}", 0]
                                for i in range(len(names))}}},
        "n": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "ks": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": h["sampler"]}},
        "sch": {"class_type": "BasicScheduler", "inputs": {
                "model": ["u", 0], "scheduler": h["scheduler"],
                "steps": int(h["steps"]), "denoise": 1.0}},
        "gd": {"class_type": "BasicGuider", "inputs": {
               "model": ["u", 0], "conditioning": ["cond", 0]}},
        "s": {"class_type": "SamplerCustomAdvanced", "inputs": {
              "noise": ["n", 0], "guider": ["gd", 0], "sampler": ["ks", 0],
              "sigmas": ["sch", 0], "latent_image": ["cond", 1]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["s", 0], "vae": ["v", 0]}},
        "vid": {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "fps": 24}},
        "sav": {"class_type": "SaveVideo", "inputs": {
                "video": ["vid", 0], "filename_prefix": prefix,
                "format": "auto", "codec": "auto"}},
    }
    for i, name in enumerate(names):
        g[f"img{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}

    m = _model_ref(g, h)
    g["sch"]["inputs"]["model"] = m
    g["gd"]["inputs"]["model"] = m
    return _queue_and_wait(cfg, g, prefix, dest, log)


def _queue_and_wait(cfg: dict, graph: dict, prefix: str, dest: Path, log=print) -> bool:
    """POST a graph, poll history to completion, collect the output file."""
    h = hcfg(cfg)
    try:
        req = urllib.request.Request(f"{_base(cfg)}/prompt",
                                     data=json.dumps({"prompt": graph}).encode(),
                                     headers={"Content-Type": "application/json"})
        pid = json.load(urllib.request.urlopen(req, timeout=30))["prompt_id"]
    except (OSError, ValueError, KeyError) as exc:
        log(f"  [h3] queue failed ({type(exc).__name__}: {exc})")
        return False
    deadline = time.time() + float(h["gen_timeout_s"])
    while time.time() < deadline:
        time.sleep(10)
        try:
            hist = json.load(urllib.request.urlopen(
                f"{_base(cfg)}/history/{pid}", timeout=30))
        except OSError:
            continue
        st = hist.get(pid, {}).get("status", {})
        if st.get("completed"):
            outs = sorted((Path(h["dir"]) / "output").glob(f"{prefix}*.mp4"))
            if not outs:
                outs = sorted((Path(h["dir"]) / "output" / "video").glob(f"{prefix}*.mp4"))
            if outs:
                shutil.move(str(outs[-1]), dest)
                return True
            log("  [h3] completed but no output file found")
            return False
        if st.get("status_str") == "error":
            msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
            log(f"  [h3] generation error: {json.dumps(msgs)[:300]}")
            return False
    log("  [h3] generation timed out")
    return False


def split_beats(cfg: dict, clip: Path, durations: list[float],
                raw_paths: list[Path]) -> bool:
    """Cut one H3 clip back into per-beat segments at the narration boundaries
    (where the prompt placed the internal cuts) and upscale each to the
    1280x704 HUD canvas — the shape the entire downstream compositor expects.
    The grid-snap means the clip runs slightly long; the final beat keeps the
    remainder and _beat_clip's -t/tpad handles the trim exactly as it does for
    Wan raws."""
    h = hcfg(cfg)
    t = 0.0
    for i, (dur, raw) in enumerate(zip(durations, raw_paths)):
        last = i == len(durations) - 1
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(clip)]
        if not last:
            cmd += ["-t", f"{dur:.3f}"]
        # Skip the upscale entirely when H3 already rendered at the compositing
        # canvas — lanczos cannot add detail, it only softens, and 2026-08-06
        # measured that softness as the recap's main quality gap against a raw
        # H3 clip. `scale` to the SAME size is not free either: it forces a full
        # swscale pass over every frame.
        if (int(h["width"]), int(h["height"])) != (1280, 704):
            cmd += ["-vf", "scale=1280:704:flags=lanczos"]
        # veryfast, not the x264 default of medium: this is an INTERMEDIATE that
        # _beat_clip re-encodes immediately. Measured on a real beat — 5.58s ->
        # 1.74s (3.2x) at the same crf and a marginally SMALLER file.
        cmd += ["-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "17",
                "-preset", G.INTERMEDIATE_PRESET, str(raw)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"  [h3] split failed for {raw.name} ({type(exc).__name__})")
            return False
        t += dur
    return True


def identity_ref(sym: str, tmp: Path) -> Path | None:
    """The cast sprite composited onto cream paper — <Picture 1> in every clip.
    Raw sprite alpha would decode as a black slab inside LoadImage."""
    from PIL import Image

    src = G.ROOT / "briefing" / "assets" / "characters" / f"{G._safe(sym)}.png"
    if not src.exists():
        return None
    dest = tmp / f"idref_{G._safe(sym)}.png"
    if not dest.exists():
        sp = Image.open(src).convert("RGBA")
        bg = Image.new("RGBA", sp.size, (247, 243, 232, 255))
        bg.alpha_composite(sp)
        bg.convert("RGB").save(dest)
    return dest
