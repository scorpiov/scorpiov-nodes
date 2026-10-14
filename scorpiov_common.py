"""
Scorpiov Common — shared parsing helpers for the Scorpiov node pack.

WHY THIS MODULE EXISTS
----------------------
Before this file, the LoRA-tag regex and the ComfyUI node-type lists were
copy-pasted into scorpiov_image_meta.py, scorpiov_wildcard.py,
scorpiov_save_image.py and scorpiov_lora_tag_loader.py — and the copies had
already drifted apart (one case-insensitive, one not; one supporting a
clip weight, one not; Meta handling UNETLoader while Save Image didn't).
Every parser in the pack now imports the canonical versions from here, so
a fix lands everywhere at once.

Nothing in this module imports ComfyUI — it is pure Python on purpose, so
the parsers can be unit-tested outside a running ComfyUI.
"""

import os
import re


# ─────────────────────────────────────────────────────────────────────────────
#  LoRA tags:  <lora:name:weight>  or  <lora:name:weight:clip_weight>
#  Weights may be negative (valid in ComfyUI). Matching is case-insensitive
#  (<LoRA:...> counts). This is the ONE canonical pattern for the pack.
# ─────────────────────────────────────────────────────────────────────────────

LORA_TAG_PATTERN = re.compile(
    r'<lora:([^:>\n]+):(-?[0-9]*\.?[0-9]+)(?::(-?[0-9]*\.?[0-9]+))?>',
    re.IGNORECASE,
)


def lora_lookup_key(name: str) -> str:
    """
    Index lookup key for a LoRA name taken from a tag: strip any subfolder
    prefix (either slash style) and the extension, lowercase the stem.
    e.g. "Anima\\sshelen.safetensors" -> "sshelen"
    """
    basename = re.split(r'[\\/]+', str(name))[-1]
    return os.path.splitext(basename)[0].lower()


def lora_display_name(name: str) -> str:
    """Human-facing LoRA name: basename without extension, case preserved."""
    basename = re.split(r'[\\/]+', str(name))[-1]
    return os.path.splitext(basename)[0]


def parse_lora_tags(text: str) -> list:
    """
    Find every <lora:...> tag in text. Returns a list of dicts:
      {"name": as-written name, "display_name": stem for display,
       "model_weight": float, "clip_weight": float}
    clip_weight defaults to model_weight when the tag has no third part.
    """
    tags = []
    for m in LORA_TAG_PATTERN.finditer(text or ""):
        name = m.group(1).strip()
        model_weight = float(m.group(2))
        clip_weight = float(m.group(3)) if m.group(3) is not None else model_weight
        tags.append({
            "name": name,
            "display_name": lora_display_name(name),
            "model_weight": model_weight,
            "clip_weight": clip_weight,
        })
    return tags


def strip_lora_tags(text: str) -> str:
    """
    Remove <lora:...> tags from text and tidy the punctuation/spacing
    their removal leaves behind (double commas, double spaces, and
    leading/trailing commas on each line).
    """
    clean = LORA_TAG_PATTERN.sub('', text or "")
    clean = re.sub(r',\s*,', ',', clean)
    clean = re.sub(r'[ \t]{2,}', ' ', clean)
    lines = [ln.strip().strip(',').strip() for ln in clean.split('\n')]
    return '\n'.join(lines).strip()


# ─────────────────────────────────────────────────────────────────────────────
#  Canonical ComfyUI node-type sets (union of every alias the pack's
#  parsers have ever recognised — keep additions here, nowhere else)
# ─────────────────────────────────────────────────────────────────────────────

CHECKPOINT_TYPES = {
    "CheckpointLoaderSimple", "CheckpointLoader",
    "unCLIPCheckpointLoader", "Checkpoint Loader (Simple)",
    "CheckpointLoaderSimpleWithNoiseSelect",
}

UNET_TYPES = {"UNETLoader"}

VAE_LOADER_TYPES = {"VAELoader"}

LORA_LOADER_TYPES = {
    "LoraLoader", "LoraLoaderModelOnly", "Lora Loader", "LoRALoader",
}

KSAMPLER_TYPES = {
    "KSampler", "KSamplerAdvanced", "KSamplerSelect",
    "SamplerCustom", "SamplerCustomAdvanced", "KSampler (Efficient)",
}

CLIP_ENCODE_TYPES = {
    "CLIPTextEncode", "CLIPTextEncodeSDXL", "CLIPTextEncodeSDXLRefiner",
    "smZ CLIPTextEncode", "BNK_CLIPTextEncode",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Small shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def basename_noext(path) -> str:
    """'folder/model_v2.safetensors' -> 'model_v2' (either slash style)."""
    base = re.split(r'[\\/]+', str(path))[-1]
    return os.path.splitext(base)[0]


def safe_float(value, default=None):
    """
    Coerce a workflow-graph value to float if it honestly is one.
    Returns default for links ([node_id, idx] lists), bools, junk, None —
    the cases that used to crash parsers with TypeError.
    """
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default
