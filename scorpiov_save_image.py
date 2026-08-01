"""
Scorpiov Save Image
-------------------
Saves images with full A1111-compatible metadata so they are correctly
parsed by Civitai, Automatic1111, and the Scorpiov Image Meta Reader.

Auto-detection
--------------
Steps, CFG, sampler, scheduler, seed, model name, and VAE are read
automatically from the workflow graph (the hidden `prompt` input that
ComfyUI injects into every output node). You only NEED to wire in:

  • positive_prompt  — from Scorpiov Wildcard Prompter
  • negative_prompt  — from Scorpiov Wildcard Prompter (negative)
  • images           — from VAE Decode

Everything else is optional. If you type a value manually in a field,
that value overrides the auto-detected one.

PNG chunks written
------------------
  "parameters"  — A1111 format (Civitai, A1111, our Meta Reader)
  "prompt"      — ComfyUI workflow JSON (auto-injected)
  "workflow"    — ComfyUI workflow extra (auto-injected)
"""

import os
import json
import re
import datetime

import numpy as np
import folder_paths
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from .scorpiov_hash_utils import get_autov2_hash


# ─────────────────────────────────────────────────────────────────────────────
#  Workflow auto-reader
#  Parses the ComfyUI `prompt` dict (the full workflow graph) to extract
#  sampler settings, checkpoint name, and VAE without the user having to
#  wire anything.
# ─────────────────────────────────────────────────────────────────────────────

# Node class names we recognise for each role
_KSAMPLER_TYPES     = {"KSampler", "KSamplerAdvanced", "KSamplerSelect",
                       "SamplerCustom", "KSampler (Efficient)"}
_CHECKPOINT_TYPES   = {"CheckpointLoaderSimple", "CheckpointLoader",
                       "unCLIPCheckpointLoader", "CheckpointLoaderSimpleWithNoiseSelect"}
_VAE_TYPES          = {"VAELoader"}


def _read_workflow(prompt: dict) -> dict:
    """
    Walk the workflow graph and extract:
      steps, cfg, sampler_name, scheduler, seed, model_name, vae_name

    Returns a dict — any value not found is an empty string / 0.
    """
    result = {
        "steps":        0,
        "cfg":          0.0,
        "sampler_name": "",
        "scheduler":    "",
        "seed":         -1,
        "model_name":   "",
        "model_name_raw": "",   # raw ckpt_name as stored in the workflow, needed to locate the file for hashing
        "vae_name":     "",
    }

    if not prompt or not isinstance(prompt, dict):
        return result

    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        ct     = node.get("class_type", "")
        inputs = node.get("inputs", {})

        # ── KSampler → steps, cfg, sampler, scheduler, seed ──────────────
        if ct in _KSAMPLER_TYPES and not result["steps"]:
            result["steps"]        = int(inputs.get("steps", 0))
            result["cfg"]          = float(inputs.get("cfg", 0.0))
            result["seed"]         = int(inputs.get("seed",
                                         inputs.get("noise_seed", -1)))
            # sampler_name may be a string or a link — use string only
            sn = inputs.get("sampler_name", "")
            if isinstance(sn, str):
                result["sampler_name"] = sn
            sc = inputs.get("scheduler", "")
            if isinstance(sc, str):
                result["scheduler"] = sc

        # ── Checkpoint → model_name ───────────────────────────────────────
        if ct in _CHECKPOINT_TYPES and not result["model_name"]:
            ckpt = inputs.get("ckpt_name", "")
            if isinstance(ckpt, str) and ckpt:
                # Strip path and extension → bare model name
                result["model_name"] = os.path.splitext(
                    os.path.basename(ckpt)
                )[0]
                result["model_name_raw"] = ckpt

        # ── VAELoader → vae_name ──────────────────────────────────────────
        if ct in _VAE_TYPES and not result["vae_name"]:
            vae = inputs.get("vae_name", "")
            if isinstance(vae, str) and vae:
                result["vae_name"] = os.path.splitext(
                    os.path.basename(vae)
                )[0]

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  A1111 "parameters" block builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_parameters(positive, negative, model, model_hash, vae, loras,
                       steps, cfg, sampler, scheduler, seed, width, height) -> str:
    """
    Build the exact A1111 parameters string that Civitai, A1111, and our
    own Meta Reader all parse correctly.
    """
    lines = []

    # Line 1 — positive prompt
    lines.append(positive.strip() if positive.strip() else "")

    # Line 2 — negative prompt
    lines.append(f"Negative prompt: {negative.strip()}")

    # Line 3 — all settings on one comma-separated line
    params = []

    if steps:
        params.append(f"Steps: {steps}")

    if sampler and sampler.strip():
        params.append(f"Sampler: {sampler.strip()}")

    if scheduler and scheduler.strip():
        params.append(f"Schedule type: {scheduler.strip()}")

    if cfg:
        params.append(f"CFG scale: {cfg:.1f}")

    if seed is not None and seed >= 0:
        params.append(f"Seed: {seed}")

    if width and height:
        params.append(f"Size: {width}x{height}")

    if model_hash and model_hash.strip():
        params.append(f"Model hash: {model_hash.strip()}")

    if model and model.strip():
        params.append(f"Model: {model.strip()}")

    if vae and vae.strip():
        params.append(f"VAE: {vae.strip()}")

    # LoRAs — parse "name (weight: 0.75) [hash: abc1234567]" lines
    lora_entries = _parse_loras(loras)
    if lora_entries:
        # Embed <lora:name:weight> in the positive prompt line (Civitai reads these)
        # and also add a Lora hashes line on the params line
        lora_tags     = ", ".join(f"<lora:{e['name']}:{e['weight']}>" for e in lora_entries)
        lora_hash_str = ", ".join(
            f"{e['name']}: {e['hash'] if e['hash'] else '0000000000'}"
            for e in lora_entries
        )
        # Inject tags into positive line (line 0) if not already there
        if lora_tags and "<lora:" not in lines[0]:
            lines[0] = (lines[0].rstrip(", ") + ", " + lora_tags).lstrip(", ")
        params.append(f'Lora hashes: "{lora_hash_str}"')

    params.append("Version: v1.9.0")

    if params:
        lines.append(", ".join(params))

    return "\n".join(lines)


def _parse_loras(loras_str: str) -> list:
    """
    Parse lines from the "loras" field into a list of dicts.
    Supported line formats (all optional pieces except name):
      "name (weight: 0.75) [hash: abc1234567]"   <- from Lora Tag Loader
      "name (weight: 0.75)"                      <- no hash available
      "name: weight"                              <- legacy manual format
      "name"                                       <- weight defaults to 1
    """
    result = []
    if not loras_str or not loras_str.strip() or loras_str.strip() == "(none)":
        return result
    for line in loras_str.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(
            r'^(.+?)\s*\(weight:\s*([0-9.]+)\)(?:\s*\[hash:\s*([0-9a-fA-F]*)\])?',
            line,
        )
        if m:
            result.append({
                "name":   m.group(1).strip(),
                "weight": m.group(2),
                "hash":   (m.group(3) or "").strip(),
            })
            continue
        parts = line.split(":")
        if len(parts) == 2:
            result.append({"name": parts[0].strip(), "weight": parts[1].strip(), "hash": ""})
            continue
        result.append({"name": line, "weight": "1", "hash": ""})
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  File path helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_date_tokens(text: str) -> str:
    def _replace(m):
        fmt = m.group(1)
        now = datetime.datetime.now()
        fmt = fmt.replace("yyyy", now.strftime("%Y"))
        fmt = fmt.replace("yy",   now.strftime("%y"))
        fmt = fmt.replace("MM",   now.strftime("%m"))
        fmt = fmt.replace("dd",   now.strftime("%d"))
        fmt = fmt.replace("HH",   now.strftime("%H"))
        fmt = fmt.replace("mm",   now.strftime("%M"))
        fmt = fmt.replace("ss",   now.strftime("%S"))
        return fmt
    return re.sub(r'%date:([^%]+)%', _replace, text, flags=re.IGNORECASE)


def _sanitise_segment(seg: str) -> str:
    cleaned = re.sub(r'[\\*?"<>|]', '_', seg).strip(". ")
    return cleaned if cleaned else "_"


def _get_save_path(output_dir: str, prefix: str, extension: str = "png") -> tuple:
    prefix   = _resolve_date_tokens(prefix)
    prefix   = prefix.replace("\\", "/")
    segments = [s for s in prefix.split("/") if s.strip()]
    if not segments:
        segments = ["Scorpiov"]

    base_name    = _sanitise_segment(segments[-1])
    folder_parts = [_sanitise_segment(s) for s in segments[:-1]]
    save_dir     = os.path.join(output_dir, *folder_parts) if folder_parts else output_dir
    os.makedirs(save_dir, exist_ok=True)

    pattern = re.compile(
        r'^' + re.escape(base_name) + r'_(\d+)_\.' + re.escape(extension) + r'$',
        re.IGNORECASE,
    )
    existing = []
    for fname in os.listdir(save_dir):
        m = pattern.match(fname)
        if m:
            existing.append(int(m.group(1)))

    counter      = (max(existing) + 1) if existing else 1
    filename     = f"{base_name}_{counter:05d}_.{extension}"
    subfolder    = "/".join(folder_parts)  # "" if no subfolder — this is what ComfyUI's preview expects
    display_name = "/".join(folder_parts + [filename]) if folder_parts else filename
    return os.path.join(save_dir, filename), display_name, filename, subfolder


# ─────────────────────────────────────────────────────────────────────────────
#  The Node
# ─────────────────────────────────────────────────────────────────────────────

class ScorpiovSaveImage:
    """
    Saves images with full metadata.

    REQUIRED wires:
      images          ← VAE Decode
      positive_prompt ← Scorpiov Wildcard Prompter (positive)
      negative_prompt ← Scorpiov Wildcard Prompter (negative)

    EVERYTHING ELSE is read automatically from the workflow.
    You can override any auto-detected value by typing in the field —
    leave a field at its default (blank / 0) to use the auto value.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),

                "filename_prefix": ("STRING", {
                    "default":   "%date:yyyy-MM-dd%/Scorpiov",
                    "multiline": False,
                    "tooltip":   "Supports %date:yyyy-MM-dd% and subfolders via /",
                }),

                # ── Must be wired — these cannot be auto-detected ─────────
                "positive_prompt": ("STRING", {
                    "multiline":  True,
                    "forceInput": True,
                    "tooltip":    "Wire from Scorpiov Wildcard Prompter → processed_text",
                }),
                "negative_prompt": ("STRING", {
                    "multiline":  True,
                    "forceInput": True,
                    "tooltip":    "Wire from your negative Wildcard Prompter → processed_text",
                }),
            },
            "optional": {
                # ── All optional — auto-detected from workflow if left blank / 0 ──
                "loras": ("STRING", {
                    "multiline":  True,
                    "forceInput": True,
                    "default":    "",
                    "tooltip":    "Wire from Lora Tag Loader \u2192 loras_info, or Image Meta Reader \u2192 loras. Leave unconnected to skip.",
                }),
                "model_name": ("STRING", {
                    "multiline": False,
                    "default":   "",
                    "tooltip":   "Auto-detected from CheckpointLoader in your workflow. Override by typing here.",
                }),
                "vae_name": ("STRING", {
                    "multiline": False,
                    "default":   "",
                    "tooltip":   "Auto-detected from VAELoader in your workflow. Override by typing here.",
                }),
                "steps": ("INT", {
                    "default": 0, "min": 0, "max": 200,
                    "tooltip": "Auto-detected from KSampler. Override by typing a non-zero value.",
                }),
                "cfg": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 30.0, "step": 0.5,
                    "tooltip": "Auto-detected from KSampler. Override by typing a non-zero value.",
                }),
                "sampler_name": ("STRING", {
                    "multiline": False,
                    "default":   "",
                    "tooltip":   "Auto-detected from KSampler. Override by typing here.",
                }),
                "scheduler": ("STRING", {
                    "multiline": False,
                    "default":   "",
                    "tooltip":   "Auto-detected from KSampler. Override by typing here.",
                }),
                "seed": ("INT", {
                    "default": -1, "min": -1, "max": 0xffffffffffffffff,
                    "tooltip": "Auto-detected from KSampler. -1 = use auto-detected value.",
                }),
                "save_metadata": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Uncheck to save a clean PNG with no embedded metadata.",
                }),
                # NOTE: model_hash is intentionally the LAST optional field.
                # ComfyUI stores widget values as a flat positional array per
                # node ("widgets_values"), matched to this list purely by
                # order. Inserting a new field anywhere but the end shifts
                # every field after it, silently corrupting values in any
                # workflow saved before the change (e.g. seed receiving the
                # control_after_generate value, cfg receiving an empty
                # string). Any future new optional field must also be
                # appended here, never inserted earlier in this dict.
                "model_hash": ("STRING", {
                    "multiline": False,
                    "default":   "",
                    "tooltip":   "Auto-detected (SHA256 AutoV2) from the checkpoint file the first time it's used, then cached. Override by typing here.",
                }),
            },
            "hidden": {
                "prompt":        "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    FUNCTION     = "save_images"
    OUTPUT_NODE  = True
    CATEGORY     = "Scorpiov/Image"

    def save_images(
        self,
        images,
        filename_prefix,
        positive_prompt,
        negative_prompt,
        loras         = "",
        model_name    = "",
        model_hash    = "",
        vae_name      = "",
        steps         = 0,
        cfg           = 0.0,
        sampler_name  = "",
        scheduler     = "",
        seed          = -1,
        save_metadata = True,
        prompt        = None,
        extra_pnginfo = None,
    ):
        output_dir = folder_paths.get_output_directory()

        # ── Auto-detect settings from the workflow graph ──────────────────
        # Read everything we can from the KSampler / CheckpointLoader / VAELoader
        # nodes in the workflow. User-supplied values override auto-detected ones.
        auto = _read_workflow(prompt)

        eff_steps   = steps       if steps       else auto["steps"]
        eff_cfg     = cfg         if cfg         else auto["cfg"]
        eff_sampler = sampler_name.strip() or auto["sampler_name"]
        eff_sched   = scheduler.strip()    or auto["scheduler"]
        eff_seed    = seed        if seed >= 0   else auto["seed"]
        eff_model   = model_name.strip()   or auto["model_name"]
        eff_vae     = vae_name.strip()     or auto["vae_name"]

        # ── Model hash: manual override, else hash the checkpoint file ─────
        # (cached — only the very first save after a checkpoint changes pays
        # the hashing cost; every save after that reuses the cached value)
        eff_model_hash = model_hash.strip()
        if not eff_model_hash and auto["model_name_raw"]:
            ckpt_path = folder_paths.get_full_path("checkpoints", auto["model_name_raw"])
            if ckpt_path:
                eff_model_hash = get_autov2_hash(ckpt_path)

        print(f"[Scorpiov Save] Auto-detected from workflow:")
        print(f"  Model    : {eff_model} (hash: {eff_model_hash or 'unknown'})   VAE: {eff_vae}")
        print(f"  Sampler  : {eff_sampler} / {eff_sched}  steps={eff_steps}  cfg={eff_cfg}  seed={eff_seed}")

        results = []

        for i, image_tensor in enumerate(images):

            # ── Tensor → PIL ──────────────────────────────────────────────
            arr     = (image_tensor.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
            pil_img = Image.fromarray(arr, mode="RGB")
            W, H    = pil_img.size

            # ── Build metadata ─────────────────────────────────────────────
            pnginfo = PngInfo()

            if save_metadata:
                params_text = _build_parameters(
                    positive   = positive_prompt,
                    negative   = negative_prompt,
                    model      = eff_model,
                    model_hash = eff_model_hash,
                    vae        = eff_vae,
                    loras      = loras or "",
                    steps      = eff_steps,
                    cfg        = eff_cfg,
                    sampler    = eff_sampler,
                    scheduler  = eff_sched,
                    seed       = eff_seed,
                    width      = W,
                    height     = H,
                )
                pnginfo.add_text("parameters", params_text)

                if prompt is not None:
                    pnginfo.add_text("prompt", json.dumps(prompt))

                if extra_pnginfo is not None:
                    for key, value in extra_pnginfo.items():
                        pnginfo.add_text(key, json.dumps(value))

            # ── Save ───────────────────────────────────────────────────────
            prefix    = filename_prefix if len(images) == 1 else f"{filename_prefix}_{i+1}"
            full_path, display_name, plain_filename, subfolder = _get_save_path(output_dir, prefix)

            pil_img.save(full_path, format="PNG", pnginfo=pnginfo, compress_level=4)

            print(f"[Scorpiov Save] Saved: {display_name}  ({W}x{H})")
            print(f"  Positive : {positive_prompt[:80]}{'...' if len(positive_prompt)>80 else ''}")
            print(f"  Negative : {negative_prompt[:60]}{'...' if len(negative_prompt)>60 else ''}")

            results.append({
                "filename":  plain_filename,
                "subfolder": subfolder,
                "type":      "output",
            })

        return {"ui": {"images": results}}


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovSaveImage": ScorpiovSaveImage,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovSaveImage": "Scorpiov Save Image 💾",
}
