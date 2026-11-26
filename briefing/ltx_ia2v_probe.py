#!/usr/bin/env python3
"""LTX-2.3 image+audio -> video: the mode H3 does not have.

H3 cannot be driven by our audio. Confirmed at three levels: the model card
("does not support pure audio-to-video mode"), the vendor API docs ("the
reference audio MUST SAY A DIFFERENT SENTENCE than the one you want in the video.
It is a timbre anchor, nothing else"), and the DiT code, where `ref_audio` is a
conditioning segment rather than a target. So H3's mouth always syncs to audio H3
invented, never to ours -- which is exactly why MiniMaxH3AudioDrive had to exist.

LTX-2.3 (Lightricks, Apache-2.0, 22B) does it natively, and ComfyUI already ships
the graph as `video_ltx2_3_ia2v.json`. The mechanism, read out of that template:

    LoadAudio -> LTXVAudioVAEEncode -> SetLatentNoiseMask(SolidMask value=0)
              -> LTXVConcatAVLatent -> SamplerCustomAdvanced

A ZERO noise mask on the audio stream means "do not denoise this" -- our audio
latent is held fixed while the video is generated around it. `samplers.py:1295`
unbinds the nested per-stream mask and packs it; 638-642 blends every step. That
is real pinning, in supported code, and it is what my hand-rolled frame-anchor
node was reinventing badly.

The image enters through LTXVImgToVideoInplace, which is the pixel anchor. So one
graph gives both things we chased separately all day: IDENTITY from our klein
keyframe, MOUTH from our Qwen3-TTS track.

    python -m briefing.ltx_ia2v_probe --image <kf.png> --audio <line.wav> --prompt "..."

Production runs it as a subprocess from host_briefing.render_host_clip (the
weekday post-market recap), one call per clip.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path

from . import h3 as H3

ROOT = Path(__file__).resolve().parent.parent
CKPT = "ltx-2.3-22b-dev-fp8.safetensors"
TE = "gemma_3_12B_it_fp4_mixed.safetensors"
DISTILL = "ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors"
IDLORA = "ltx-2.3-id-lora-talkvid-3k.safetensors"
# OmniNFT RL (Kijai/LTX2.3_comfy, from zghhui/OmniNFT): an RL fine-tune of LTX's
# joint AV generation, trained with modality-wise negative-aware credit assignment.
# Published numbers: JavisBench DeSync 0.569 -> 0.269, gains in AV-Quality /
# AV-Consistency / AV-Synchrony — i.e. exactly our weakest axis (lip-sync, and the
# stochastic dead-mouth the QC gate exists for). Kijai's card: strength 2.0 to
# match the original alpha 64 / rank 32. OPT-IN until a paired A/B on our own
# pinned-audio graph proves it — every published number is for LTX generating its
# own audio, and ours is pinned.
OMNILORA = "LTX-2.3-OmniNFT-RL-Lora_bf16.safetensors"
UPSCALER = "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"

# The template's own first-stage schedule (8 steps). The distilled LoRA is what
# makes so few steps viable.
SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
# stage 2 refines from 0.85 -- short, because the structure is already there
SIGMAS_STAGE2 = "0.85, 0.7250, 0.4219, 0.0"
# THE NEGATIVE PROMPT IS LIVE AGAIN — but only when NAG is on (see --nag-scale).
#
# The old value was inherited from ComfyUI's template and had never done anything,
# because at cfg 1.0 the unconditional pass is skipped entirely. LTX2_NAG applies the
# negative inside the attention layers instead, so it works with guidance switched
# off. That makes this the FIRST place in the whole pipeline where naming something
# unwanted is safe: in the positive prompt a banned noun is a summoned noun (the
# subtitle ban went 1-of-2 beats to 2-of-2), and that is a property of positive
# conditioning, not of the words.
#
# PATHOLOGY-ONLY, by measurement. The first draft banned smiling/grinning next to
# the real defect and got exactly what it asked for -- lips shut in every frame at
# one seed. An open smiling mouth is often CORRECT while speaking; what is wrong is
# a HELD one, so only stillness and the measured defect classes are named:
#   smiling/grinning     v4 rendered a fixed grin instead of speech, reproducibly,
#                        at three seeds
#   lip sync             the same clip's mouth was shut on its loudest moments
#   camera motion        the ID LoRA walks the framing in 1.20-1.30 over 27 s
#   text                 LTX burns in its own subtitles, stochastically
#   identity             mean 0.52-0.58 against an earlier locked plate, with
#                        visible morphing
# Deliberately NOT copied from the community avatar workflow: "eyes looking at
# camera" (the presenter must look at camera) and its set-specific items (a cap
# slogan, a microphone, doors and shelves) which belong to that author's scene,
# not ours.
NEG = ("silent, not speaking, still lips, motionless mouth, a single expression held "
       "for the entire shot, frozen face, mismatched lip sync, off-sync audio, "
       "camera movement, push-in, zoom, dolly, drifting framing, tilted camera, "
       "subtitles, captions, burned-in text, watermark, lettering, "
       "microphone, headset microphone, lavalier mic, earpiece, "
       "light stand, c-stand, tripod, boom arm, magic arm, articulated clamp arm, "
       "studio grip equipment, softbox, "
       "extra limbs, extra hands, extra fingers, deformed hands, "
       "cartoon, 3D CGI look, video game, plastic skin")
# "microphone" was deliberately NOT copied from the community avatar workflow —
# it looked like that author's set dressing. Measured 2026-08-11: in NATIVE
# audio mode (LTX generating the speech) the "presenter performing" prior drew a
# tan headset-mic capsule ON THE PRESENTER'S CHEEK, growing in from frame ~40
# while the keyframe-anchored opening stayed clean. It was in their NEG because
# ref-voice mode NEEDS it there.
#
# THE GRIP LINE, added 2026-08-19, is the same prior reaching one step further.
# On the SOXL 中文 clip a black articulated magic-arm rig — knobs, yellow brand
# lettering, a boom head — grew out of the bare wall beside the host from ~5.5s
# to ~9s and he put his hand on it. The mic nouns above were suppressed, so it
# produced the nearest thing this list had not thought to name. "Presenter
# against a seamless wall" co-occurs with studio grip gear throughout the
# training data; NEG has to cover the CLASS, not one member of it.
#
# This is the cheaper half of the fix and it is second for a reason: NEG can
# only push away what someone has already thought to name, so the load-bearing
# change is not summoning an outstretched hand in the first place
# (ltx_story.strip_held_gesture, applied in host_briefing).


def build(img: str, aud: str, prompt: str, width: int, height: int, length: int,
          seed: int, prefix: str, fps: int, seconds: float,
          id_lora: float, distill: float, img_strength: float,
          img_compression: int = 0, nag_scale: float = 0.0,
          guides: tuple = (), guide_strength: float = 0.7,
          omni_lora: float = 0.0) -> dict:
    g = {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "te": {"class_type": "LTXAVTextEncoderLoader", "inputs": {
               "text_encoder": TE, "ckpt_name": CKPT, "device": "default"}},
        "avae": {"class_type": "LTXVAudioVAELoader", "inputs": {"ckpt_name": CKPT}},
        "lora": {"class_type": "LoraLoaderModelOnly", "inputs": {
                 "model": ["ckpt", 0], "lora_name": DISTILL, "strength_model": distill}},
        "img": {"class_type": "LoadImage", "inputs": {"image": img}},
        "aud": {"class_type": "LoadAudio", "inputs": {"audio": aud}},
        # keep only the span the clip actually needs
        "trim": {"class_type": "TrimAudioDuration", "inputs": {
                 "audio": ["aud", 0], "start_index": 0.0, "duration": float(seconds)}},
        "aenc": {"class_type": "LTXVAudioVAEEncode", "inputs": {
                 "audio": ["trim", 0], "audio_vae": ["avae", 0]}},
        # An empty audio latent: the input of the removed reference-voice mode, where
        # LTX generated the speech. Nothing links to it any more; it stays so the
        # emitted graph is the one every production render was made with.
        "aempty": {"class_type": "LTXVEmptyLatentAudio", "inputs": {
                   "frames_number": int(length), "frame_rate": float(fps),
                   "batch_size": 1, "audio_vae": ["avae", 0]}},
        # value 0 == "do not denoise" -> OUR audio survives sampling untouched.
        # The official graph sizes this mask to the VIDEO dimensions rather than a
        # fixed square, so match it.
        "zero": {"class_type": "SolidMask", "inputs": {
                 "value": 0.0, "width": width, "height": height}},
        "amask": {"class_type": "SetLatentNoiseMask", "inputs": {
                  "samples": ["aenc", 0], "mask": ["zero", 0]}},
        "vlat": {"class_type": "EmptyLTXVLatentVideo", "inputs": {
                 "width": width, "height": height, "length": length, "batch_size": 1}},
        # the pixel anchor: our klein keyframe written into the video latent
        # STRENGTH IS THE LIP-SYNC KNOB, not just an identity one. The official
        # first stage uses 0.7 and only re-applies at 1.0 on the upscale pass. At
        # 1.0 here the keyframe is written into the latent hard enough to freeze
        # the face: our first run measured gape95 0.069 (almost no mouth motion)
        # and a lip-sync margin of -0.020 despite the audio itself surviving at
        # +0.871. A frozen mouth cannot track any audio.
        "i2v": {"class_type": "LTXVImgToVideoInplace", "inputs": {
                "vae": ["ckpt", 2], "image": ["img", 0], "latent": ["vlat", 0],
                "strength": img_strength, "bypass": False}},
        "cat": {"class_type": "LTXVConcatAVLatent", "inputs": {
                "video_latent": ["i2v", 0],
                # the pinned wav (ia2v)
                "audio_latent": ["amask", 0]}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["te", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["te", 0]}},
        "lcond": {"class_type": "LTXVConditioning", "inputs": {
                  "positive": ["pos", 0], "negative": ["neg", 0], "frame_rate": float(fps)}},
        "gd": {"class_type": "CFGGuider", "inputs": {
               "model": ["idl", 0], "positive": ["lcond", 0], "negative": ["lcond", 1], "cfg": 1.0}},
        "n": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "ks": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        # Stage-1 schedule: the official template's hand-written 9 sigmas.
        "sig": {"class_type": "ManualSigmas", "inputs": {"sigmas": SIGMAS}},
        "s": {"class_type": "SamplerCustomAdvanced", "inputs": {
              "noise": ["n", 0], "guider": ["gd", 0], "sampler": ["ks", 0],
              "sigmas": ["sig", 0], "latent_image": ["cat", 0]}},
        "sep": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["s", 0]}},
        "vdec": {"class_type": "VAEDecode", "inputs": {"samples": ["dec_src", 0], "vae": ["ckpt", 2]}},
        "adec": {"class_type": "LTXVAudioVAEDecode", "inputs": {
                 "samples": ["adec_src", 1], "audio_vae": ["avae", 0]}},
        "vid": {"class_type": "CreateVideo", "inputs": {
                "images": ["vdec", 0], "fps": float(fps), "audio": ["adec", 0]}},
        "sav": {"class_type": "SaveVideo", "inputs": {
                "video": ["vid", 0], "filename_prefix": prefix,
                "format": "auto", "codec": "auto"}},
    }
    # STAGE 2, traced from video_ltx2_3_ia2v.json's links (not its node list).
    #
    #   stage 1  ImgToVideoInplace @0.7 -> sample on the 9-sigma schedule
    #   split    video latent | audio latent  (the audio is now DENOISED and ours)
    #   upscale  LTXVLatentUpsampler x2 on the VIDEO latent only
    #   stage 2  ImgToVideoInplace @1.0 on the upscaled latent, CropGuides for the
    #            conditioning, re-concat with the SAME stage-1 audio, sample again
    #            on the short 4-sigma schedule
    #
    # This is what the strength sweep implies the design is for: 0.7 buys mouth
    # motion, then 1.0 buys identity back on a latent that already carries the
    # synced mouth. Running stage 1 alone forced a choice between the two
    # (identity 0.688 @1.0 with margin -0.020, vs identity 0.571 @0.7 with
    # margin +0.200).
    # LTXVPreprocess — IN THE OFFICIAL TEMPLATE, ABSENT FROM OURS UNTIL NOW.
    #
    # `video_ltx2_3_ia2v.json` runs the conditioning image through
    # LTXVPreprocess(img_compression=18) and feeds the RESULT to both
    # ImgToVideoInplace nodes. The node re-encodes the frame as H.264 at that CRF
    # and decodes it back (nodes_lt.py:700), i.e. it deliberately degrades the
    # image. The reason is distribution: the model was trained on frames pulled out
    # of compressed video, so a pristine PNG keyframe is out of distribution — and
    # a first frame the model cannot reproduce is a first frame it drifts away from.
    #
    # Kept OFF by default so this does not silently change every render. Pass
    # --img-compression 18 to match the template exactly.
    if img_compression:
        g["pre"] = {"class_type": "LTXVPreprocess", "inputs": {
                    "image": ["img", 0], "img_compression": int(img_compression)}}
        g["i2v"]["inputs"]["image"] = ["pre", 0]

    # Resolve the model link BEFORE stage 2 copies it. Doing this after left gd2
    # pointing at ["idl", 0], a node that is never created when the ID LoRA is
    # off -- ComfyUI rejected the graph with KeyError 'idl'.
    src = ["lora", 0]
    if id_lora > 0:
        g["idl"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                    "model": src, "lora_name": IDLORA, "strength_model": id_lora}}
        g["gd"]["inputs"]["model"] = ["idl", 0]
    else:
        g["gd"]["inputs"]["model"] = src

    if omni_lora > 0:
        g["omni"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                     "model": g["gd"]["inputs"]["model"], "lora_name": OMNILORA,
                     "strength_model": float(omni_lora)}}
        g["gd"]["inputs"]["model"] = ["omni", 0]

    # NORMALISED ATTENTION GUIDANCE. The one thing the community avatar workflow has
    # that we never did, and the reason its author could write "smiling, laughing,
    # mismatched lip sync" into a negative and have it mean something.
    #
    # NAG pushes the negative concept out inside the attention layers and renormalises,
    # so it does not need the unconditional pass that cfg 1.0 skips. Defaults are the
    # node's own and the avatar workflow's: scale 11, alpha 0.25, tau 2.5.
    #
    # It takes the NEGATIVE conditioning (LTXVConditioning output 1) — the same text
    # CFGGuider is handed and silently ignores. Applied to the resolved model BEFORE
    # the two-stage block copies that link, so both samplers get it.
    if nag_scale > 0:
        g["nag"] = {"class_type": "LTX2_NAG", "inputs": {
                    "model": g["gd"]["inputs"]["model"],
                    "nag_scale": float(nag_scale), "nag_alpha": 0.25,
                    "nag_tau": 2.5, "nag_cond_video": ["lcond", 1],
                    "inplace": False}}
        g["gd"]["inputs"]["model"] = ["nag", 0]


    # ---- MULTI-POINT IDENTITY GUIDES (see module docstring for the mechanism) ----
    if guides:
        src_img = ["pre", 0] if img_compression else ["img", 0]
        pos_link, neg_link, lat_link = ["lcond", 0], ["lcond", 1], ["vlat", 0]
        for n, fidx in enumerate(guides):
            key = f"guide{n}"
            g[key] = {"class_type": "LTXVAddGuide", "inputs": {
                "positive": pos_link, "negative": neg_link, "vae": ["ckpt", 2],
                "latent": lat_link, "image": src_img,
                # must be a multiple of 8 for 9+ frame video; -1 is the last frame
                # FRAME 0 KEEPS img_strength. The guides replace ImgToVideoInplace,
                # which was img_strength's only consumer in stage 1 (stage 2's anchor
                # is hardcoded at 1.0) -- so without this the flag is silently dead
                # whenever guides are on. It was: three renders at 0.70/0.55/0.40
                # came back with identical metrics to three decimals, because they
                # were the same video. Frame 0 is the pixel anchor and the later
                # guides are the identity re-assertions, so they get separate knobs.
                "frame_idx": int(fidx),
                "strength": float(img_strength if fidx == 0 else guide_strength)}}
            pos_link, neg_link, lat_link = [key, 0], [key, 1], [key, 2]
        # the guides replace ImgToVideoInplace in stage 1, exactly as flf2v does
        g["cat"]["inputs"]["video_latent"] = lat_link
        g.pop("i2v", None)
        g["gd"]["inputs"]["positive"] = pos_link
        g["gd"]["inputs"]["negative"] = neg_link
        if nag_scale > 0:
            g["nag"]["inputs"]["nag_cond_video"] = neg_link
        # strip the appended guide frames off the sampled latent before anything
        # downstream sees them, or they decode as extra frames on the end
        g["degd"] = {"class_type": "LTXVCropGuides", "inputs": {
            "positive": pos_link, "negative": neg_link, "latent": ["sep", 0]}}

    g["upmodel"] = {"class_type": "LatentUpscaleModelLoader",
                    "inputs": {"model_name": UPSCALER}}
    g["up"] = {"class_type": "LTXVLatentUpsampler", "inputs": {
               "samples": ["degd", 2] if guides else ["sep", 0],
               "upscale_model": ["upmodel", 0],
               "vae": ["ckpt", 2]}}
    # the template feeds the PREPROCESSED image to both stages, so match it
    g["i2v2"] = {"class_type": "LTXVImgToVideoInplace", "inputs": {
                 "vae": ["ckpt", 2],
                 "image": ["pre", 0] if img_compression else ["img", 0],
                 "latent": ["up", 0],
                 "strength": 1.0, "bypass": False}},
    g["i2v2"] = g["i2v2"][0] if isinstance(g["i2v2"], tuple) else g["i2v2"]
    g["crop"] = {"class_type": "LTXVCropGuides", "inputs": {
                 "positive": ["lcond", 0],
                 "negative": ["lcond", 1],
                 "latent": ["sep", 0]}}
    g["cat2"] = {"class_type": "LTXVConcatAVLatent", "inputs": {
                 "video_latent": ["i2v2", 0], "audio_latent": ["sep", 1]}}
    g["gd2"] = {"class_type": "CFGGuider", "inputs": {
                "model": g["gd"]["inputs"]["model"],
                "positive": ["crop", 0], "negative": ["crop", 1], "cfg": 1.0}}
    g["n2"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed) + 1}}
    g["sig2"] = {"class_type": "ManualSigmas",
                 "inputs": {"sigmas": SIGMAS_STAGE2}}
    g["s2"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
               "noise": ["n2", 0], "guider": ["gd2", 0], "sampler": ["ks", 0],
               "sigmas": ["sig2", 0], "latent_image": ["cat2", 0]}}
    g["sep2"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["s2", 0]}}
    g["dec_src"] = g["sep2"]
    g["vdec"]["inputs"]["samples"] = ["sep2", 0]
    g["adec"]["inputs"]["samples"] = ["sep2", 1]
    g.pop("dec_src", None)

    return g


def main() -> None:
    ap = argparse.ArgumentParser(description="LTX-2.3 image+audio -> video")
    ap.add_argument("--image", required=True, help="presenter keyframe (identity anchor)")
    ap.add_argument("--audio", required=True, help="OUR Qwen3-TTS wav (drives the mouth)")
    ap.add_argument("--prompt", required=True,
                    help="the composed performance prompt (host_briefing passes "
                         "ltx_official.compose's output)")
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--height", type=int, default=448)
    ap.add_argument("--seconds", type=float, default=6.6)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--id-lora", type=float, default=0.0,
                    help="0 = official ia2v graph; >0 adds the talking-head ID LoRA")
    ap.add_argument("--img-strength", type=float, default=0.7,
                    help="official first stage uses 0.7; 1.0 freezes the face")
    ap.add_argument("--nag-scale", type=float, default=0.0,
                    help="LTX2_NAG strength. 0 = off (the negative prompt is dead "
                         "weight, as it has always been at cfg 1.0). 11 is the node "
                         "default and what the community avatar workflow uses. This "
                         "is the only channel on this graph where naming an unwanted "
                         "thing suppresses it instead of summoning it")
    ap.add_argument("--omni-lora", type=float, default=0.0,
                    help="OmniNFT RL LoRA strength. 0 = off. Kijai recommends 2.0 "
                         "(alpha 64 / rank 32). RL-tuned for AV synchrony — "
                         "candidate fix for our lip-sync axis; unproven on the "
                         "pinned-audio graph until the paired A/B")
    ap.add_argument("--guides", default="",
                    help="comma-separated frame indices to re-anchor the keyframe "
                         "at, e.g. '0,192,384'. Each must be a multiple of 8; -1 "
                         "means the last frame. THIS is the identity knob: "
                         "ImgToVideoInplace anchors frame 0 and nothing else, which "
                         "is why img_strength never moved identity")
    ap.add_argument("--guide-strength", type=float, default=0.7)
    ap.add_argument("--img-compression", type=int, default=0,
                    help="LTXVPreprocess CRF on the conditioning image. The official "
                         "ia2v template uses 18 and we have always used none — a "
                         "pristine PNG is out of distribution for a model trained on "
                         "compressed video frames. 0 = off (our historical behaviour)")
    ap.add_argument("--out", default=str(ROOT / "briefing" / ".probe" / "ltx_ia2v"))
    args = ap.parse_args()

    cfg = json.loads((ROOT / "briefing" / "config.json").read_text())
    cfg.pop("_comment", None)
    for k in list(cfg):
        if isinstance(cfg[k], dict):
            cfg[k].pop("_comment", None)
    h = H3.hcfg(cfg)
    h["port"] = 8189
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    if not H3.ensure_server(cfg):
        sys.exit("server unreachable")

    d = Path(h["dir"]); (d / "input").mkdir(exist_ok=True)

    def stage(src, kind):
        n = f"ltx_{kind}_{uuid.uuid4().hex[:8]}{Path(src).suffix}"
        shutil.copy(src, d / "input" / n)
        return n

    fps = 24
    # LTX wants (frames - 1) divisible by 8, plus 1
    frames = int(round(args.seconds * fps))
    # Round UP to the (n-1)%8==0 grid, never down. Rounding down TRUNCATES THE
    # LINE'S LAST WORDS: a 11.60 s line snapped to 11.38 s, cutting 0.22 s, and the
    # missing tail showed up as a slumped audio correlation (0.434 for that beat
    # vs 0.718-0.771 for the beats whose length happened to land on the grid).
    # A shot that runs slightly long is harmless; one that clips the line is not.
    frames = ((frames - 1 + 7) // 8) * 8 + 1
    # The official graph samples stage 1 at HALF the target and lets the x2
    # upscaler carry it to full: EmptyLTXVLatentVideo is fed width/2, height/2
    # via ComfyMathExpression "a/2".
    w1, h1 = ((args.width // 2) // 32) * 32, ((args.height // 2) // 32) * 32

    prefix = f"ltx_{uuid.uuid4().hex[:8]}"
    dest = out / "ltx_ia2v.mp4"
    print(f"  [ltx] stage1 {w1}x{h1} -> x2 -> {w1*2}x{h1*2}, {frames} frames @ {fps}fps "
          f"({frames/fps:.2f}s), img_strength={args.img_strength}, "
          f"id_lora={args.id_lora}, TWO-STAGE")
    g = build(stage(args.image, "img"), stage(args.audio, "aud"), args.prompt,
              w1, h1, frames, args.seed, prefix, fps,
              args.seconds, args.id_lora, 0.5, args.img_strength,
              img_compression=args.img_compression,
              nag_scale=args.nag_scale,
              guides=tuple(int(x) for x in args.guides.split(',') if x.strip()),
              guide_strength=args.guide_strength, omni_lora=args.omni_lora)
    (out / "graph.json").write_text(json.dumps(g, indent=1))
    if not H3._queue_and_wait(cfg, g, prefix, dest):
        sys.exit("  [ltx] render FAILED")
    print(f"  [ltx] video -> {dest}")


if __name__ == "__main__":
    main()
