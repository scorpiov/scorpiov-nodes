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


def _resolve_numeric(value, prompt, depth=0):
    """
    A KSampler field (steps/cfg/seed) can be a literal value, or a
    [node_id, output_index] link to a small "primitive" node (an Int,
    Float, or Seed-generator type node) supplying that value instead of
    it being typed directly into the widget. Rather than hardcoding every
    third-party node's class name and field name, follow the link one hop
    and return the first value on that node that actually parses as a
    number -- mirrors the link-following scorpiov_image_meta.py already
    does for text, generalized here for numbers. Returns None if nothing
    resolvable is found (caller falls back to its own default).
    """
    if depth > 3 or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    if isinstance(value, list) and len(value) == 2:
        node = prompt.get(str(value[0]))
        if not isinstance(node, dict):
            return None
        for v in node.get("inputs", {}).values():
            resolved = _resolve_numeric(v, prompt, depth + 1)
            if resolved is not None:
                return resolved
    return None


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
            # Any of these may be a literal value OR a [node_id, output_index]
            # link if the widget was converted to an input (e.g. fed from a
            # separate Int/Float/Seed node) rather than typed directly.
            # _resolve_numeric follows that link rather than crashing on it.
            steps_val = _resolve_numeric(inputs.get("steps", 0), prompt)
            if steps_val is not None:
                result["steps"] = int(steps_val)

            cfg_val = _resolve_numeric(inputs.get("cfg", 0.0), prompt)
            if cfg_val is not None:
                result["cfg"] = float(cfg_val)

            seed_val = _resolve_numeric(
                inputs.get("seed", inputs.get("noise_seed", -1)), prompt
            )
            if seed_val is not None:
                result["seed"] = int(seed_val)
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

def _build_parameters(positive, negative, model, vae, loras,
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

    if model and model.strip():
        params.append(f"Model: {model.strip()}")

    if vae and vae.strip():
        params.append(f"VAE: {vae.strip()}")

    # LoRAs — parse "name (weight: 0.75)" lines and emit as tags
    lora_entries = _parse_loras(loras)
    if lora_entries:
        # Embed <lora:name:weight> in the positive prompt line (Civitai reads these)
        # and also add a Lora hashes line on the params line
        lora_tags     = ", ".join(f"<lora:{e['name']}:{e['weight']}>" for e in lora_entries)
        lora_hash_str = ", ".join(f"{e['name']}: 0000000000" for e in lora_entries)
        # Inject tags into positive line (line 0) if not already there
        if lora_tags and "<lora:" not in lines[0]:
            lines[0] = (lines[0].rstrip(", ") + ", " + lora_tags).lstrip(", ")
        params.append(f'Lora hashes: "{lora_hash_str}"')

    params.append("Version: v1.9.0")

    if params:
        lines.append(", ".join(params))

    return "\n".join(lines)


def _parse_loras(loras_str: str) -> list:
    """Parse 'name (weight: 0.75)' lines into list of dicts."""
    result = []
    if not loras_str or not loras_str.strip() or loras_str.strip() == "(none)":
        return result
    for line in loras_str.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^(.+?)\s*\(weight:\s*([0-9.]+)\)', line)
        if m:
            result.append({"name": m.group(1).strip(), "weight": m.group(2)})
            continue
        parts = line.split(":")
        if len(parts) == 2:
            result.append({"name": parts[0].strip(), "weight": parts[1].strip()})
            continue
        result.append({"name": line, "weight": "1"})
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
                    "multiline": True,
                    "default":   "",
                    "tooltip":   "Auto-filled if left blank. Or wire from Image Meta Reader → loras.",
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

        print(f"[Scorpiov Save] Auto-detected from workflow:")
        print(f"  Model    : {eff_model}   VAE: {eff_vae}")
        print(f"  Sampler  : {eff_sampler} / {eff_sched}  steps={eff_steps}  cfg={eff_cfg}  seed={eff_seed}")

        results = []

        for i, image_tensor in enumerate(images):

            # ── Tensor → PIL ──────────────────────────────────────────────
            # NOTE: Qwen-Image-2.1's VAE decodes to RGBA (4 channels, native
            # transparency), not RGB. Hardcoding mode="RGB" makes Pillow
            # silently reinterpret the 4-byte-per-pixel buffer as 3 bytes per
            # pixel, which is exactly the static/moiré garbage this node used
            # to produce. Detect the channel count and use the matching mode.
            arr      = (image_tensor.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
            channels = arr.shape[-1] if arr.ndim == 3 else 1
            if channels >= 4:
                # RGBA — keep the alpha channel so transparency survives in the PNG
                pil_img = Image.fromarray(arr[:, :, :4], mode="RGBA")
            elif channels == 3:
                pil_img = Image.fromarray(arr, mode="RGB")
            elif channels == 1:
                pil_img = Image.fromarray(arr[:, :, 0], mode="L")
            else:
                # Unexpected layout — best effort on first 3 channels
                pil_img = Image.fromarray(arr[:, :, :3], mode="RGB")
            W, H     = pil_img.size

            # ── Build metadata ─────────────────────────────────────────────
            pnginfo = PngInfo()

            if save_metadata:
                params_text = _build_parameters(
                    positive  = positive_prompt,
                    negative  = negative_prompt,
                    model     = eff_model,
                    vae       = eff_vae,
                    loras     = loras or "",
                    steps     = eff_steps,
                    cfg       = eff_cfg,
                    sampler   = eff_sampler,
                    scheduler = eff_sched,
                    seed      = eff_seed,
                    width     = W,
                    height    = H,
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
