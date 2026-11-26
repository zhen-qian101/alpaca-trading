"""ComfyUI image backend — a drop-in for the diffusers text-to-image pipeline.

The whole render pipeline generates images by calling
``pipe(prompt=..., num_inference_steps=..., guidance_scale=..., height=..., width=...).images[0]``
(see generate.generate_images, story.make_sprite / _first_frame). ``ComfyPipe``
implements that EXACT interface but runs the diffusion on a headless **ComfyUI**
server over its HTTP API instead of an in-process ``diffusers`` pipeline. Wins:

  * the model stays RESIDENT in VRAM across the many image gens in one render
    (no per-call cold load) — a real throughput win for the slideshow tier;
  * the render process holds NO CUDA context (all GPU work is in the ComfyUI
    server), so the dual-context slowdown that plagued the Wan path can't happen;
  * one API-driven backend that the Wan/interpolation graphs can later join.

hyperframes stays the compositor and edge-tts stays the audio — ComfyUI only
produces the SD 3.5 stills. Everything degrades gracefully: if the server or the
model files aren't available, ``ComfyPipe.create`` returns None and the caller
falls back to the in-process diffusers pipeline (story.load_pipe).

Model layout (ComfyUI/models/, SD 3.5 Large fp8 split — see briefing/comfy_setup):
    diffusion_models/sd3.5_large_fp8_scaled.safetensors
    text_encoders/{clip_g,clip_l,t5xxl_fp8_e4m3fn_scaled}.safetensors
    vae/sd3.5_vae.safetensors      (reused from the diffusers cache)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace


def _cfg(cfg: dict) -> dict:
    return cfg.get("comfyui", {}) if isinstance(cfg, dict) else {}


def comfy_python() -> str:
    """The comfy conda env's python: the env that has cv2 and the ComfyUI
    stack, which the render's own env deliberately lacks. Steps that need cv2
    (the stylist's plates, the lip-sync gate) run in it as a subprocess.

    $COMFY_PYTHON names it; the default is the conda env "comfy" under
    ~/anaconda3. agent/run_stylist.sh resolves the same default in bash."""
    return (os.environ.get("COMFY_PYTHON")
            or str(Path.home() / "anaconda3" / "envs" / "comfy" / "bin" / "python"))


def _base(c: dict) -> str:
    return f"http://{c.get('host', '127.0.0.1')}:{int(c.get('port', 8188))}"


def _get(url: str, timeout: float = 10):
    return urllib.request.urlopen(urllib.request.Request(url), timeout=timeout)


def _post(url: str, payload: dict, timeout: float = 30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _server_up(c: dict) -> bool:
    try:
        _get(_base(c) + "/system_stats", timeout=4).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def _models_present(c: dict) -> bool:
    d = Path(c.get("dir", "")) / "models"
    need = [d / "diffusion_models" / c.get("unet", "sd3.5_large_fp8_scaled.safetensors"),
            d / "text_encoders" / c.get("clip_g", "clip_g.safetensors"),
            d / "text_encoders" / c.get("clip_l", "clip_l.safetensors"),
            d / "text_encoders" / c.get("t5", "t5xxl_fp8_e4m3fn_scaled.safetensors"),
            d / "vae" / c.get("vae", "sd3.5_vae.safetensors")]
    return all(p.exists() for p in need)


def ensure_server(cfg: dict, verbose: bool = True) -> bool:
    """Ensure a headless ComfyUI server is up; start it if not. Returns True when
    reachable. The server is left RUNNING (persistent) for subsequent renders."""
    c = _cfg(cfg)
    if _server_up(c):
        return True
    comfy_dir = Path(c.get("dir", "/mnt/sandisk/ComfyUI"))
    py = os.path.expanduser(c.get("python") or comfy_python())
    if not comfy_dir.exists() or not Path(py).exists() or not _models_present(c):
        if verbose:
            print("  [comfy] server/model not available — using diffusers instead")
        return False
    log = comfy_dir / "comfy_server.log"
    if verbose:
        print(f"  [comfy] starting headless server on {_base(c)} …")
    subprocess.Popen(
        [py, str(comfy_dir / "main.py"), "--listen", c.get("host", "127.0.0.1"),
         "--port", str(int(c.get("port", 8188))), "--disable-auto-launch"],
        cwd=str(comfy_dir), stdout=open(log, "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    deadline = time.time() + float(c.get("start_timeout_s", 180))
    while time.time() < deadline:
        if _server_up(c):
            if verbose:
                print("  [comfy] server ready")
            return True
        time.sleep(2)
    if verbose:
        print("  [comfy] server did not come up in time — using diffusers instead")
    return False


def free_memory(cfg: dict) -> None:
    """Ask ComfyUI to unload models + free VRAM (call before a Wan phase needs it)."""
    c = _cfg(cfg)
    try:
        _post(_base(c) + "/free", {"unload_models": True, "free_memory": True}, timeout=30).read()
    except (urllib.error.URLError, OSError):
        pass


def _sd35_workflow(c: dict, prompt: str, negative: str, width: int, height: int,
                   steps: int, cfg_scale: float, seed: int) -> dict:
    """Build the ComfyUI API-format graph for SD 3.5 Large (fp8 split): load unet +
    triple CLIP + vae, SD3 model-sampling shift, sample, decode, save."""
    return {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": c.get("unet", "sd3.5_large_fp8_scaled.safetensors"),
                            "weight_dtype": c.get("weight_dtype", "default")}},
        "clip": {"class_type": "TripleCLIPLoader",
                 "inputs": {"clip_name1": c.get("clip_g", "clip_g.safetensors"),
                            "clip_name2": c.get("clip_l", "clip_l.safetensors"),
                            "clip_name3": c.get("t5", "t5xxl_fp8_e4m3fn_scaled.safetensors")}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": c.get("vae", "sd3.5_vae.safetensors")}},
        "shift": {"class_type": "ModelSamplingSD3",
                  "inputs": {"model": ["unet", 0], "shift": float(c.get("shift", 3.0))}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": negative}},
        "latent": {"class_type": "EmptySD3LatentImage",
                   "inputs": {"width": int(width), "height": int(height), "batch_size": 1}},
        "ksampler": {"class_type": "KSampler",
                     "inputs": {"model": ["shift", 0], "positive": ["pos", 0], "negative": ["neg", 0],
                                "latent_image": ["latent", 0], "seed": int(seed), "steps": int(steps),
                                "cfg": float(cfg_scale), "sampler_name": c.get("sampler", "euler"),
                                "scheduler": c.get("scheduler", "sgm_uniform"), "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["ksampler", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "briefing"}},
    }


class ComfyPipe:
    """diffusers-pipeline-shaped shim backed by a ComfyUI server."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.c = _cfg(cfg)
        self.client_id = uuid.uuid4().hex
        self._seed = 0

    # ---- construction / teardown -----------------------------------------
    @classmethod
    def create(cls, cfg: dict) -> "ComfyPipe | None":
        if not ensure_server(cfg):
            return None
        self = cls(cfg)
        try:                                       # one warm-up gen so the model is resident + the graph is valid
            self(prompt="a flat minimalist cartoon circle, plain background, no text",
                 num_inference_steps=int(self.c.get("warmup_steps", 6)),
                 guidance_scale=float(cfg.get("image_guidance", 4.0)), height=512, width=512)
        except Exception as exc:  # noqa: BLE001
            print(f"  [comfy] warm-up failed ({type(exc).__name__}: {exc}); using diffusers instead")
            return None
        print("  [comfy] SD 3.5 backend ready (model resident in ComfyUI)")
        return self

    def set_progress_bar_config(self, **_kw):      # diffusers-API no-op (load_pipe calls it)
        return self

    def to(self, *_a, **_k):                        # diffusers-API no-op
        return self

    def free(self):
        free_memory(self.cfg)

    # ---- generation (the diffusers pipeline call signature) ---------------
    def __call__(self, prompt: str, num_inference_steps: int = 28, guidance_scale: float = 4.0,
                 height: int = 1024, width: int = 1024, **_kw) -> SimpleNamespace:
        c = self.c
        # SD3 latent must be /16; clamp odd sizes.
        width, height = (int(width) // 16) * 16 or 512, (int(height) // 16) * 16 or 512
        self._seed = (self._seed * 1103515245 + 12345) & 0x7FFFFFFF or 1   # deterministic, varied per call
        wf = _sd35_workflow(c, prompt, c.get("negative", "text, watermark, blurry, low quality, deformed"),
                            width, height, int(num_inference_steps or c.get("steps", 28)),
                            float(guidance_scale if guidance_scale else c.get("cfg", 4.0)), self._seed)
        img = self._run(wf, timeout=float(c.get("gen_timeout_s", 180)))
        return SimpleNamespace(images=[img])

    def _run(self, workflow: dict, timeout: float):
        from PIL import Image
        base = _base(self.c)
        resp = json.load(_post(base + "/prompt", {"prompt": workflow, "client_id": self.client_id}))
        pid = resp["prompt_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.6)
            try:
                hist = json.load(_get(f"{base}/history/{pid}", timeout=10))
            except (urllib.error.URLError, OSError):
                continue
            if pid not in hist:
                continue
            outs = hist[pid].get("outputs", {})
            imgs = (outs.get("save", {}) or {}).get("images", [])
            if imgs:
                meta = imgs[0]
                q = urllib.parse.urlencode({"filename": meta["filename"],
                                            "subfolder": meta.get("subfolder", ""),
                                            "type": meta.get("type", "output")})
                data = _get(f"{base}/view?{q}", timeout=30).read()
                return Image.open(io.BytesIO(data)).convert("RGB")
            status = hist[pid].get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"comfy graph error: {json.dumps(status)[:300]}")
        raise TimeoutError(f"comfy generation timed out after {timeout:.0f}s")


# ==========================================================================
# FLUX.2 KLEIN 9B storyboard frames (the recap video's first-frame engine).
# Each screenplay beat gets its own frame: the FIXED cast character (adopted
# sprite passed as a ReferenceLatent + a strict identity lock in the prompt)
# posed IN the crayon scene doing that beat's action — no compositing, no
# character drift. Distilled klein: 4 steps, cfg 1, ~3-5 s per frame.
# ==========================================================================
def _kcfg(cfg: dict) -> dict:
    return _cfg(cfg).get("klein", {})


def klein_models_present(cfg: dict) -> bool:
    c, k = _cfg(cfg), _kcfg(cfg)
    d = Path(c.get("dir", "/mnt/sandisk/ComfyUI")) / "models"
    need = [d / "diffusion_models" / k.get("unet", "flux-2-klein-9b-fp8.safetensors"),
            d / "text_encoders" / k.get("clip", "qwen_3_8b_fp8mixed.safetensors"),
            d / "vae" / k.get("vae", "flux2-vae.safetensors")]
    return all(p.exists() for p in need)


def _klein_edit_workflow(k: dict, ref_name: str, prompt: str, width: int, height: int,
                         seed: int, megapixels: float = 1.0) -> dict:
    unet = k.get("unet", "flux-2-klein-9b-fp8.safetensors")
    clip = k.get("clip", "qwen_3_8b_fp8mixed.safetensors")
    vae = k.get("vae", "flux2-vae.safetensors")
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "zero": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
        "ref_img": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "ref_scaled": {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": ["ref_img", 0], "upscale_method": "lanczos",
            "megapixels": float(megapixels), "resolution_steps": 1}},
        "ref_lat": {"class_type": "VAEEncode", "inputs": {"pixels": ["ref_scaled", 0], "vae": ["vae", 0]}},
        "pos_ref": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["pos", 0], "latent": ["ref_lat", 0]}},
        "neg_ref": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["zero", 0], "latent": ["ref_lat", 0]}},
        "lat": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": int(width), "height": int(height), "batch_size": 1}},
        "sched": {"class_type": "Flux2Scheduler", "inputs": {
            "steps": int(k.get("steps", 4)), "width": int(width), "height": int(height)}},
        "samp": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": k.get("sampler", "euler")}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "guide": {"class_type": "CFGGuider", "inputs": {
            "model": ["unet", 0], "positive": ["pos_ref", 0], "negative": ["neg_ref", 0],
            "cfg": float(k.get("cfg", 1.0))}},
        "run": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guide", 0], "sampler": ["samp", 0],
            "sigmas": ["sched", 0], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["run", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["dec", 0], "filename_prefix": "storyboard"}},
    }


def klein_storyboard(cfg: dict, ref_image: Path, prompt: str, dest: Path,
                     width: int = 1280, height: int = 704, seed: int = 0) -> bool:
    """One storyboard frame: reference image (any RGBA sprite — composited onto
    white here) + prompt -> PNG at dest. Returns False on any failure."""
    c, k = _cfg(cfg), _kcfg(cfg)
    try:
        from PIL import Image
        name = f"sb_ref_{uuid.uuid4().hex[:10]}.png"
        inp = Path(c.get("dir", "/mnt/sandisk/ComfyUI")) / "input"
        inp.mkdir(exist_ok=True)
        im = Image.open(ref_image).convert("RGBA")
        canvas = Image.new("RGBA", im.size, (255, 255, 255, 255))
        canvas.alpha_composite(im)
        canvas.convert("RGB").save(inp / name)

        wf = _klein_edit_workflow(k, name, prompt, width, height, seed)
        base = _base(c)
        pid = json.load(_post(base + "/prompt", {"prompt": wf, "client_id": "briefing-sb"}))["prompt_id"]
        deadline = time.time() + float(k.get("gen_timeout_s", 240))
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                hist = json.load(_get(f"{base}/history/{pid}", timeout=10))
            except (urllib.error.URLError, OSError):
                continue
            if pid not in hist:
                continue
            imgs = (hist[pid].get("outputs", {}).get("save", {}) or {}).get("images", [])
            if imgs:
                q = urllib.parse.urlencode({"filename": imgs[0]["filename"],
                                            "subfolder": imgs[0].get("subfolder", ""),
                                            "type": imgs[0].get("type", "output")})
                dest.write_bytes(_get(f"{base}/view?{q}", timeout=60).read())
                return dest.stat().st_size > 0
            if hist[pid].get("status", {}).get("status_str") == "error":
                print(f"  [klein-sb] graph error: {json.dumps(hist[pid]['status'])[:200]}")
                return False
        print("  [klein-sb] storyboard timed out")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  [klein-sb] {type(exc).__name__}: {exc}")
        return False


def klein_storyboard_multi(cfg: dict, ref_images: list[Path], prompt: str, dest: Path,
                           width: int = 1280, height: int = 704, seed: int = 0,
                           megapixels: float | list[float] = 1.0) -> bool:
    """klein_storyboard with SEVERAL reference images instead of one.

    Flux.2 takes a chain of ReferenceLatents, so more than one view of the same
    subject can condition a generation. For the host that matters: one front
    plate leaves the model guessing at the presenter's face in three-quarter, and
    guessing is where identity drifts. Feeding the front plus the three-quarter
    plate gives it the geometry instead.

    Falls back to nothing special for a single image — the graph is identical to
    klein_storyboard's at length 1.

    ``megapixels`` sets the size each reference is normalised to before encoding,
    per reference if a list is given. It is worth spending unevenly: reference
    tokens cost H*W/256, so a face-only plate at 2.0 MP buys real facial detail,
    while pushing a half-body plate past its native 1.09 MP only upscales.
    """
    c, k = _cfg(cfg), _kcfg(cfg)
    if not ref_images:
        return False
    try:
        from PIL import Image
        inp = Path(c.get("dir", "/mnt/sandisk/ComfyUI")) / "input"
        inp.mkdir(exist_ok=True)
        names = []
        for p in ref_images:
            name = f"sb_ref_{uuid.uuid4().hex[:10]}.png"
            im = Image.open(p).convert("RGBA")
            canvas = Image.new("RGBA", im.size, (255, 255, 255, 255))
            canvas.alpha_composite(im)
            canvas.convert("RGB").save(inp / name)
            names.append(name)

        mps = ([float(m) for m in megapixels] if isinstance(megapixels, (list, tuple))
               else [float(megapixels)] * len(names))
        mps += [mps[-1]] * (len(names) - len(mps))

        wf = _klein_edit_workflow(k, names[0], prompt, width, height, seed, mps[0])
        # chain the extra references onto both conditioning branches
        pos_tail, neg_tail = "pos_ref", "neg_ref"
        for i, name in enumerate(names[1:], start=2):
            wf[f"ref_img{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            wf[f"ref_scaled{i}"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
                "image": [f"ref_img{i}", 0], "upscale_method": "lanczos",
                "megapixels": mps[i - 1], "resolution_steps": 1}}
            wf[f"ref_lat{i}"] = {"class_type": "VAEEncode", "inputs": {
                "pixels": [f"ref_scaled{i}", 0], "vae": ["vae", 0]}}
            wf[f"pos_ref{i}"] = {"class_type": "ReferenceLatent", "inputs": {
                "conditioning": [pos_tail, 0], "latent": [f"ref_lat{i}", 0]}}
            wf[f"neg_ref{i}"] = {"class_type": "ReferenceLatent", "inputs": {
                "conditioning": [neg_tail, 0], "latent": [f"ref_lat{i}", 0]}}
            pos_tail, neg_tail = f"pos_ref{i}", f"neg_ref{i}"
        wf["guide"]["inputs"]["positive"] = [pos_tail, 0]
        wf["guide"]["inputs"]["negative"] = [neg_tail, 0]

        base = _base(c)
        pid = json.load(_post(base + "/prompt", {"prompt": wf, "client_id": "briefing-sbm"}))["prompt_id"]
        deadline = time.time() + float(k.get("gen_timeout_s", 240))
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                hist = json.load(_get(f"{base}/history/{pid}", timeout=10))
            except (urllib.error.URLError, OSError):
                continue
            if pid not in hist:
                continue
            imgs = (hist[pid].get("outputs", {}).get("save", {}) or {}).get("images", [])
            if imgs:
                q = urllib.parse.urlencode({"filename": imgs[0]["filename"],
                                            "subfolder": imgs[0].get("subfolder", ""),
                                            "type": imgs[0].get("type", "output")})
                dest.write_bytes(_get(f"{base}/view?{q}", timeout=60).read())
                return dest.stat().st_size > 0
            if hist[pid].get("status", {}).get("status_str") == "error":
                print(f"  [klein-sbm] graph error: {json.dumps(hist[pid]['status'])[:200]}")
                return False
        print("  [klein-sbm] storyboard timed out")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  [klein-sbm] {type(exc).__name__}: {exc}")
        return False


def klein_icon(cfg: dict, prompt: str, dest: Path, size: int = 768, seed: int = 0,
               width: int | None = None, height: int | None = None) -> bool:
    """One crayon prop/number icon, plain text-to-image (no reference latent) so
    the icons share the storyboards' hand-drawn world instead of SD 3.5's look.
    White paper background on purpose — the caller rembg-cuts it out. Returns
    False on any failure (caller falls back to the SD 3.5 icon path).
    width/height override the square default (used for the 16:9 cover art)."""
    c, k = _cfg(cfg), _kcfg(cfg)
    try:
        wf = _klein_edit_workflow(k, "", prompt, width or size, height or size, seed)
        # strip the reference-image chain: pure t2i for a standalone object.
        for node in ("ref_img", "ref_scaled", "ref_lat", "pos_ref", "neg_ref"):
            del wf[node]
        wf["guide"]["inputs"]["positive"] = ["pos", 0]
        wf["guide"]["inputs"]["negative"] = ["zero", 0]
        wf["save"]["inputs"]["filename_prefix"] = "icon"
        base = _base(c)
        pid = json.load(_post(base + "/prompt", {"prompt": wf, "client_id": "briefing-icon"}))["prompt_id"]
        deadline = time.time() + float(k.get("gen_timeout_s", 240))
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                hist = json.load(_get(f"{base}/history/{pid}", timeout=10))
            except (urllib.error.URLError, OSError):
                continue
            if pid not in hist:
                continue
            imgs = (hist[pid].get("outputs", {}).get("save", {}) or {}).get("images", [])
            if imgs:
                q = urllib.parse.urlencode({"filename": imgs[0]["filename"],
                                            "subfolder": imgs[0].get("subfolder", ""),
                                            "type": imgs[0].get("type", "output")})
                dest.write_bytes(_get(f"{base}/view?{q}", timeout=60).read())
                return dest.stat().st_size > 0
            if hist[pid].get("status", {}).get("status_str") == "error":
                print(f"  [klein-icon] graph error: {json.dumps(hist[pid]['status'])[:200]}")
                return False
        print("  [klein-icon] icon timed out")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  [klein-icon] {type(exc).__name__}: {exc}")
        return False
