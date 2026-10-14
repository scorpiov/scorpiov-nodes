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

# Canonical node-type sets live in scorpiov_common.py, shared with the
# Image Meta Reader — the two files had already drifted (Meta recognised
# UNETLoader, this file didn't, so Flux/Qwen images saved with a blank
# Model field). Real AutoV2 hashes come from scorpiov_hash_utils (the
# Lora Tag Loader already used it; this file used to fabricate them).
try:
    from .scorpiov_common import (
        CHECKPOINT_TYPES, UNET_TYPES, VAE_LOADER_TYPES,
        LORA_LOADER_TYPES, KSAMPLER_TYPES, basename_noext,
    )
    from .scorpiov_hash_utils import get_autov2_hash
except ImportError:  # loaded as a loose module, not as part of the package
    from scorpiov_common import (
        CHECKPOINT_TYPES, UNET_TYPES, VAE_LOADER_TYPES,
        LORA_LOADER_TYPES, KSAMPLER_TYPES, basename_noext,
    )
    try:
        from scorpiov_hash_utils import get_autov2_hash
    except ImportError:
        get_autov2_hash = None


# ─────────────────────────────────────────────────────────────────────────────
#  Workflow auto-reader
#  Parses the ComfyUI `prompt` dict (the full workflow graph) to extract
#  sampler settings, checkpoint name, and VAE without the user having to
#  wire anything.
# ─────────────────────────────────────────────────────────────────────────────


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


def _linked_node(prompt: dict, value):
    """If value is a [node_id, output_index] link, return the target node dict."""
    if isinstance(value, list) and len(value) == 2:
        node = prompt.get(str(value[0]))
        if isinstance(node, dict):
            return node
    return None


def _read_workflow(prompt: dict) -> dict:
    """
    Walk the workflow graph and extract:
      steps, cfg, sampler_name, scheduler, seed, model_name, vae_name,
      model_raw / model_folder (the model filename as configured, kept so
      save_images can locate the file and compute its real AutoV2 hash),
      and loras (a "name (weight: w)" line per LoraLoader-family node, in
      the same format the Meta Reader / Lora Tag Loader produce).

    Returns a dict — any value not found is an empty string / 0.

    Changes from the original version:
      - Fields are filled PER-FIELD from any sampler, not all-or-nothing
        from the first KSampler the dict order happens to yield (which
        recorded the wrong sampler in hires/refiner/multi-sampler graphs
        and never consulted later samplers for fields the first lacked).
      - UNETLoader workflows (Flux / Qwen) now yield a model name, like
        the Meta Reader already did.
      - SamplerCustom-family chains are partially followed: sampler name
        via a linked KSamplerSelect, steps/scheduler via a linked
        BasicScheduler, seed via a linked noise node.
      - LoRAs are actually extracted — the loras field's tooltip always
        promised "Auto-filled if left blank"; now it is.
    """
    result = {
        "steps":        0,
        "cfg":          0.0,
        "sampler_name": "",
        "scheduler":    "",
        "seed":         -1,
        "model_name":   "",
        "model_raw":    "",
        "model_folder": "checkpoints",
        "vae_name":     "",
        "loras":        "",
    }

    if not prompt or not isinstance(prompt, dict):
        return result

    lora_lines = []

    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        ct     = node.get("class_type", "")
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue

        # ── Samplers → steps, cfg, sampler, scheduler, seed (per-field) ──
        if ct in KSAMPLER_TYPES:
            # Any of these may be a literal value OR a [node_id, output_index]
            # link if the widget was converted to an input (e.g. fed from a
            # separate Int/Float/Seed node) rather than typed directly.
            # _resolve_numeric follows that link rather than crashing on it.
            if not result["steps"]:
                steps_val = _resolve_numeric(inputs.get("steps", 0), prompt)
                if steps_val is not None:
                    result["steps"] = int(steps_val)
            if not result["cfg"]:
                cfg_val = _resolve_numeric(inputs.get("cfg", 0.0), prompt)
                if cfg_val is not None:
                    result["cfg"] = float(cfg_val)
            if result["seed"] < 0:
                seed_val = _resolve_numeric(
                    inputs.get("seed", inputs.get("noise_seed", -1)), prompt
                )
                if seed_val is None and "noise" in inputs:
                    noise_node = _linked_node(prompt, inputs.get("noise"))
                    if noise_node is not None:
                        seed_val = _resolve_numeric(
                            noise_node.get("inputs", {}).get("noise_seed", -1),
                            prompt,
                        )
                if seed_val is not None:
                    result["seed"] = int(seed_val)
            if not result["sampler_name"]:
                sn = inputs.get("sampler_name", "")
                if isinstance(sn, str) and sn:
                    result["sampler_name"] = sn
                else:
                    # SamplerCustom-style: sampler arrives as a link to a
                    # KSamplerSelect-style node holding the name.
                    sampler_node = _linked_node(prompt, inputs.get("sampler"))
                    if sampler_node is not None:
                        sn2 = sampler_node.get("inputs", {}).get("sampler_name", "")
                        if isinstance(sn2, str) and sn2:
                            result["sampler_name"] = sn2
            if not result["scheduler"]:
                sc = inputs.get("scheduler", "")
                if isinstance(sc, str) and sc:
                    result["scheduler"] = sc
                else:
                    # SamplerCustom-style: scheduler lives on the sigmas
                    # node (usually BasicScheduler) feeding this sampler.
                    sigmas_node = _linked_node(prompt, inputs.get("sigmas"))
                    if sigmas_node is not None:
                        sc2 = sigmas_node.get("inputs", {}).get("scheduler", "")
                        if isinstance(sc2, str) and sc2:
                            result["scheduler"] = sc2
                        if not result["steps"]:
                            st2 = _resolve_numeric(
                                sigmas_node.get("inputs", {}).get("steps", 0),
                                prompt,
                            )
                            if st2 is not None:
                                result["steps"] = int(st2)

        # ── BasicScheduler (standalone, SamplerCustom chains) ────────────
        if ct == "BasicScheduler":
            if not result["scheduler"]:
                sc = inputs.get("scheduler", "")
                if isinstance(sc, str) and sc:
                    result["scheduler"] = sc
            if not result["steps"]:
                st = _resolve_numeric(inputs.get("steps", 0), prompt)
                if st is not None:
                    result["steps"] = int(st)

        # ── Checkpoint / UNET → model_name (+ raw name for hashing) ──────
        if ct in CHECKPOINT_TYPES and not result["model_name"]:
            ckpt = inputs.get("ckpt_name", "")
            if isinstance(ckpt, str) and ckpt:
                result["model_name"]  = basename_noext(ckpt)
                result["model_raw"]   = ckpt
                result["model_folder"] = "checkpoints"

        if ct in UNET_TYPES and not result["model_name"]:
            unet = inputs.get("unet_name", "")
            if isinstance(unet, str) and unet:
                result["model_name"]   = basename_noext(unet)
                result["model_raw"]    = unet
                result["model_folder"] = "diffusion_models"

        # ── VAELoader → vae_name ──────────────────────────────────────────
        if ct in VAE_LOADER_TYPES and not result["vae_name"]:
            vae = inputs.get("vae_name", "")
            if isinstance(vae, str) and vae:
                result["vae_name"] = basename_noext(vae)

        # ── LoraLoader family → loras lines ──────────────────────────────
        if ct in LORA_LOADER_TYPES:
            lname = inputs.get("lora_name", "")
            if isinstance(lname, str) and lname:
                w_val = _resolve_numeric(
                    inputs.get("strength_model", inputs.get("strength", 1.0)),
                    prompt,
                )
                weight = float(w_val) if w_val is not None else 1.0
                line = f"{basename_noext(lname)} (weight: {weight:g})"
                if line not in lora_lines:
                    lora_lines.append(line)

    result["loras"] = "\n".join(lora_lines)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  A1111 "parameters" block builder
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_number(value) -> str:
    """Format a float the way A1111 does: 7 -> '7', 4.75 -> '4.75' (the old
    code rounded CFG to 1 decimal, so the metadata quietly lied)."""
    s = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _build_parameters(positive, negative, model, vae, loras,
                       steps, cfg, sampler, scheduler, seed, width, height,
                       model_hash="") -> str:
    """
    Build the exact A1111 parameters string that Civitai, A1111, and our
    own Meta Reader all parse correctly.

    Honesty rules (changed): hashes are only written when they are REAL
    (model_hash computed from the actual checkpoint file; LoRA hashes
    taken from the "[hash: ...]" the Lora Tag Loader supplies). The old
    version wrote 0000000000 for every LoRA and a hardcoded
    "Version: v1.9.0" — Civitai trusts these fields, so fabricated values
    are worse than omitting them.
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
        params.append(f"CFG scale: {_fmt_number(cfg)}")

    if seed is not None and seed >= 0:
        params.append(f"Seed: {seed}")

    if width and height:
        params.append(f"Size: {width}x{height}")

    if model_hash:
        params.append(f"Model hash: {model_hash}")

    if model and model.strip():
        params.append(f"Model: {model.strip()}")

    if vae and vae.strip():
        params.append(f"VAE: {vae.strip()}")

    # LoRAs — parse "name (weight: 0.75)" lines and emit as tags
    lora_entries = _parse_loras(loras)
    if lora_entries:
        # Embed <lora:name:weight> in the positive prompt line (Civitai reads these)
        lora_tags = ", ".join(f"<lora:{e['name']}:{e['weight']}>" for e in lora_entries)
        # Inject tags into positive line (line 0) if not already there
        if lora_tags and "<lora:" not in lines[0]:
            lines[0] = (lines[0].rstrip(", ") + ", " + lora_tags).lstrip(", ")
        # ...and add a Lora hashes line ONLY for entries whose hash we
        # actually know (supplied by the Lora Tag Loader). Entries without
        # a known hash are omitted rather than faked.
        known_hashes = [e for e in lora_entries if e.get("hash")]
        if known_hashes:
            lora_hash_str = ", ".join(f"{e['name']}: {e['hash']}" for e in known_hashes)
            params.append(f'Lora hashes: "{lora_hash_str}"')

    if params:
        lines.append(", ".join(params))

    return "\n".join(lines)


def _parse_loras(loras_str: str) -> list:
    """
    Parse loras lines into [{"name", "weight", "hash"}].
    Accepted line forms:
      name (weight: 0.75)                — Meta Reader / auto-detected
      name (weight: 0.75) [hash: ab12cd34ef]  — Lora Tag Loader (real hash)
      name:0.75                          — bare fallback
      name                               — weight defaults to 1
    """
    result = []
    if not loras_str or not loras_str.strip() or loras_str.strip() == "(none)":
        return result
    for raw_line in loras_str.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        hash_m = re.search(r'\[hash:\s*([0-9a-fA-F]+)\]', line)
        file_hash = hash_m.group(1) if hash_m else ""
        core = re.sub(r'\[hash:[^\]]*\]', '', line).strip()
        m = re.match(r'^(.+?)\s*\(weight:\s*(-?[0-9.]+)\)', core)
        if m:
            result.append({"name": m.group(1).strip(), "weight": m.group(2),
                           "hash": file_hash})
            continue
        if ":" in core:
            # Split on the LAST colon so names containing colons survive
            # (the old split(":") required exactly one colon in the line).
            name, weight = core.rsplit(":", 1)
            result.append({"name": name.strip(), "weight": weight.strip(),
                           "hash": file_hash})
            continue
        result.append({"name": core, "weight": "1", "hash": file_hash})
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


# Windows reserved device names — a base name of CON/NUL/COM1/... cannot
# be created as a file on Windows no matter the extension.
_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _sanitise_segment(seg: str) -> str:
    # The character set now includes ':' and control characters, which the
    # original missed — on Windows ':' in a filename means a drive-relative
    # path / alternate data stream, so "draft:v2" used to fail (or worse).
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', '_', seg).strip(". ")
    if not cleaned:
        return "_"
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    # Cap segment length so date+prefix combinations stay far away from
    # Windows' path-length limit once the output folder is prepended.
    return cleaned[:120]


def _prepare_save_location(output_dir: str, prefix: str) -> tuple:
    """Resolve date tokens + sanitise the prefix into (save_dir, base_name,
    folder_parts), creating the folder. Split out of _get_save_path so a
    batch can prepare once and count sequentially (see save_images)."""
    prefix   = _resolve_date_tokens(prefix)
    prefix   = prefix.replace("\\", "/")
    segments = [s for s in prefix.split("/") if s.strip()]
    if not segments:
        segments = ["Scorpiov"]

    base_name    = _sanitise_segment(segments[-1])
    folder_parts = [_sanitise_segment(s) for s in segments[:-1]]
    save_dir     = os.path.join(output_dir, *folder_parts) if folder_parts else output_dir
    os.makedirs(save_dir, exist_ok=True)
    return save_dir, base_name, folder_parts


def _scan_counter(save_dir: str, base_name: str, extension: str = "png") -> int:
    """Next free counter for base_name in save_dir (scans the folder once)."""
    pattern = re.compile(
        r'^' + re.escape(base_name) + r'_(\d+)_\.' + re.escape(extension) + r'$',
        re.IGNORECASE,
    )
    existing = []
    for fname in os.listdir(save_dir):
        m = pattern.match(fname)
        if m:
            existing.append(int(m.group(1)))
    return (max(existing) + 1) if existing else 1


def _get_save_path(output_dir: str, prefix: str, extension: str = "png") -> tuple:
    save_dir, base_name, folder_parts = _prepare_save_location(output_dir, prefix)
    counter      = _scan_counter(save_dir, base_name, extension)
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
                    "tooltip":   "Supports %date:yyyy-MM-dd%, %seed%, %model%, %width%, %height%, "
                                 "and subfolders via /. Batch images share the prefix and get "
                                 "sequential counters (no more _2/_3 prefix variants).",
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
        # LoRAs: wired value wins; if left blank, use the LoraLoader nodes
        # found in the graph (the tooltip's "auto-filled" promise).
        eff_loras   = (loras or "").strip() or auto["loras"]

        # Real AutoV2 model hash, computed from the actual model file the
        # graph names (cached on disk by scorpiov_hash_utils, so this is
        # only slow the first time a given file's content is seen). Only
        # the auto-detected model can be hashed — a hand-typed override
        # name has no known file, so its hash is honestly omitted.
        eff_model_hash = ""
        if save_metadata and not model_name.strip() and auto["model_raw"] \
                and get_autov2_hash is not None:
            try:
                model_path = folder_paths.get_full_path(
                    auto["model_folder"], auto["model_raw"])
            except Exception:
                model_path = None
            if model_path:
                try:
                    eff_model_hash = get_autov2_hash(model_path) or ""
                except Exception as e:
                    print(f"[Scorpiov Save] Model hash failed: {e}")

        print(f"[Scorpiov Save] Auto-detected from workflow:")
        print(f"  Model    : {eff_model}   VAE: {eff_vae}")
        print(f"  Sampler  : {eff_sampler} / {eff_sched}  steps={eff_steps}  cfg={eff_cfg}  seed={eff_seed}")

        results = []
        # Per-prefix save locations, prepared (folder scanned) ONCE per
        # distinct resolved prefix in this call, with the counter then
        # incremented locally per image. The old code re-scanned the whole
        # output folder for every image in the batch and restarted the
        # counter for each per-image prefix variant.
        _locations = {}

        def _location_for(resolved_prefix):
            loc = _locations.get(resolved_prefix)
            if loc is None:
                save_dir, base_name, folder_parts = _prepare_save_location(
                    output_dir, resolved_prefix)
                loc = {
                    "save_dir": save_dir, "base_name": base_name,
                    "folder_parts": folder_parts,
                    "next": _scan_counter(save_dir, base_name),
                }
                _locations[resolved_prefix] = loc
            return loc

        for i, image_tensor in enumerate(images):

            # ── Tensor → PIL ──────────────────────────────────────────────
            # NOTE: Qwen-Image-2.1's VAE decodes to RGBA (4 channels, native
            # transparency), not RGB. Hardcoding mode="RGB" makes Pillow
            # silently reinterpret the 4-byte-per-pixel buffer as 3 bytes per
            # pixel, which is exactly the static/moiré garbage this node used
            # to produce. Detect the channel count and use the matching mode.
            arr      = (image_tensor.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
            if arr.ndim == 2:
                arr = arr[:, :, None]     # single-plane HxW → HxWx1
            channels = arr.shape[-1] if arr.ndim == 3 else 1
            if channels >= 4:
                # RGBA — keep the alpha channel so transparency survives in the PNG
                pil_img = Image.fromarray(arr[:, :, :4], mode="RGBA")
            elif channels == 3:
                pil_img = Image.fromarray(arr, mode="RGB")
            elif channels == 2:
                # Luminance+alpha — used to fall into the RGB branch and
                # raise on the shape/mode mismatch.
                pil_img = Image.fromarray(arr, mode="LA")
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
                    loras     = eff_loras,
                    steps     = eff_steps,
                    cfg       = eff_cfg,
                    sampler   = eff_sampler,
                    scheduler = eff_sched,
                    seed      = eff_seed,
                    width     = W,
                    height    = H,
                    model_hash = eff_model_hash,
                )
                pnginfo.add_text("parameters", params_text)

                # A chunk that fails to serialise must not cost the image
                # itself — save without that chunk and say so.
                added_keys = {"parameters"}
                if prompt is not None:
                    try:
                        pnginfo.add_text("prompt", json.dumps(prompt))
                        added_keys.add("prompt")
                    except (TypeError, ValueError) as e:
                        print(f"[Scorpiov Save] Skipping prompt chunk: {e}")

                if extra_pnginfo is not None:
                    for key, value in extra_pnginfo.items():
                        if key in added_keys:
                            continue   # never write a duplicate PNG chunk
                        try:
                            pnginfo.add_text(key, json.dumps(value))
                            added_keys.add(key)
                        except (TypeError, ValueError) as e:
                            print(f"[Scorpiov Save] Skipping {key!r} chunk: {e}")

            # ── Save ───────────────────────────────────────────────────────
            # Filename tokens beyond %date:...% — resolved per image now
            # that W/H are known: %seed%, %model%, %width%, %height%.
            prefix = filename_prefix
            prefix = prefix.replace("%seed%", str(eff_seed))
            prefix = prefix.replace("%model%", eff_model or "")
            prefix = prefix.replace("%width%", str(W))
            prefix = prefix.replace("%height%", str(H))

            loc            = _location_for(prefix)
            counter        = loc["next"]
            loc["next"]    = counter + 1
            plain_filename = f"{loc['base_name']}_{counter:05d}_.png"
            full_path      = os.path.join(loc["save_dir"], plain_filename)
            subfolder      = "/".join(loc["folder_parts"])
            display_name   = "/".join(loc["folder_parts"] + [plain_filename]) \
                             if loc["folder_parts"] else plain_filename

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
