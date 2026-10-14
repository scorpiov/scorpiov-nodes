"""
Scorpiov Lora Tag Loader for ComfyUI
----------------------------------------
Reads <lora:name:weight> or <lora:name:weight:clip_weight> tags out of a text
prompt, loads each named LoRA onto the given model/clip, strips the tags from
the text, and outputs the merged MODEL, CLIP, and the cleaned STRING (ready
to wire into a standard CLIPTextEncode node).

Tag formats supported:
  <lora:name:0.8>       -> model_weight = clip_weight = 0.8
  <lora:name:0.8:0.6>   -> model_weight = 0.8, clip_weight = 0.6

Also outputs a "loras_info" string formatted as "name (weight: 0.8)" per
line -- wire this straight into Scorpiov Save Image's "loras" input so
Civitai-compatible metadata (Lora hashes line + <lora:...> badge tags) gets
embedded in the saved PNG automatically.

Same LoRA search/index logic as scorpiov_wildcard.py: searches every folder
registered under ComfyUI's "loras" folder_paths entry, matched by filename
stem (case-insensitive), so subfolders "just work".

REFACTOR NOTE (Oct 14, 2026): the local <lora:...> regex, tag parser, and
lookup-key helper were deleted — they now come from scorpiov_common
(the one canonical LoRA-tag pattern for the whole pack). The LoRA index
is built lazily once per session instead of on every call, stem
collisions print a WARNING, and duplicate tags are de-duplicated with a
printed note instead of being loaded twice.
"""

import os
import folder_paths
import comfy.sd
import comfy.utils
from .scorpiov_hash_utils import get_autov2_hash

try:
    from .scorpiov_common import parse_lora_tags, strip_lora_tags, lora_lookup_key
except ImportError:  # standalone use / unit tests outside the package
    from scorpiov_common import parse_lora_tags, strip_lora_tags, lora_lookup_key


# ─────────────────────────────────────────────────────────────────────────────
#  LoRA index — built lazily once per session, shared by every call
#  (Tag parsing and lookup helpers now come from scorpiov_common so every
#  file that reads <lora:...> tags uses the same canonical pattern.)
# ─────────────────────────────────────────────────────────────────────────────

_lora_index = None  # lazily-built stem -> full path map


def _build_lora_index():
    index = {}
    for lora_dir in folder_paths.get_folder_paths("loras"):
        if not os.path.isdir(lora_dir):
            continue
        for root, dirs, files in os.walk(lora_dir):
            dirs.sort()
            for fname in sorted(files):
                if fname.lower().endswith((".safetensors", ".pt", ".ckpt")):
                    stem = os.path.splitext(fname)[0].lower()
                    full_path = os.path.join(root, fname)
                    if stem not in index:
                        index[stem] = full_path
                    elif os.path.abspath(index[stem]) != os.path.abspath(full_path):
                        # Two DIFFERENT files sharing one filename stem — the
                        # tag format can't tell them apart, so the first one
                        # wins; say so out loud instead of silently choosing.
                        print(f"[Scorpiov Lora Tag Loader] WARNING: duplicate "
                              f"LoRA stem '{stem}': keeping '{index[stem]}', "
                              f"ignoring '{full_path}'")
    return index


def _get_lora_index():
    """Lazily build the LoRA index once per session; reuse it every call."""
    global _lora_index
    if _lora_index is None:
        print("[Scorpiov Lora Tag Loader] Building LoRA index...")
        _lora_index = _build_lora_index()
        print(f"[Scorpiov Lora Tag Loader] {len(_lora_index)} LoRA files indexed.")
    return _lora_index


def _resolve_loras(tags):
    """
    Given a list of tag dicts from scorpiov_common.parse_lora_tags
    ({name, display_name, model_weight, clip_weight}), look up each
    LoRA's file path and AutoV2 hash once. Returns a list of dicts:
    {name, model_weight, clip_weight, path, hash}.
    A lora that can't be found on disk gets path=None, hash="".
    """
    if not tags:
        return []
    lora_index = _get_lora_index()

    resolved = []
    for tag in tags:
        name = tag["name"]
        stem = lora_lookup_key(name)
        path = lora_index.get(stem)
        if path is None:
            print(f"[Scorpiov Lora Tag Loader] WARNING: LoRA not found: '{name}'")
            resolved.append({
                "name": name, "model_weight": tag["model_weight"],
                "clip_weight": tag["clip_weight"], "path": None, "hash": "",
            })
            continue
        file_hash = get_autov2_hash(path)
        resolved.append({
            "name": name, "model_weight": tag["model_weight"],
            "clip_weight": tag["clip_weight"], "path": path, "hash": file_hash,
        })
    return resolved


def _load_loras(model, clip, resolved):
    for entry in resolved:
        if entry["path"] is None:
            continue
        print(f"[Scorpiov Lora Tag Loader] Loading LoRA '{entry['name']}' "
              f"@ model={entry['model_weight']}, clip={entry['clip_weight']} "
              f"(hash: {entry['hash'] or 'unknown'})")
        lora_sd = comfy.utils.load_torch_file(entry["path"], safe_load=True)
        model, clip = comfy.sd.load_lora_for_models(
            model, clip, lora_sd, entry["model_weight"], entry["clip_weight"]
        )
    return model, clip


# ─────────────────────────────────────────────────────────────────────────────
#  The Node
# ─────────────────────────────────────────────────────────────────────────────

class ScorpiovLoraTagLoader:
    """
    Extracts <lora:name:weight[:clip_weight]> tags from text, loads them onto
    model/clip, and outputs the merged model/clip plus the tag-stripped text.
    Wire the stripped text into a CLIPTextEncode node afterward.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "clip":  ("CLIP",),
                "text":  ("STRING", {
                    "multiline":   True,
                    "forceInput":  True,
                    "tooltip":     "Text containing <lora:name:weight> tags, "
                                   "e.g. from Wildcard Prompter.",
                }),
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "STRING", "STRING")
    RETURN_NAMES = ("model",  "clip",  "text",   "loras_info")
    FUNCTION     = "load"
    CATEGORY     = "Scorpiov/Loaders"
    OUTPUT_NODE  = True

    def load(self, model, clip, text):
        clean_text = strip_lora_tags(text)

        # Dedupe identical tags (same LoRA + same model/clip weights):
        # loading the same LoRA twice would double-apply it, so keep one
        # and say so rather than hiding the skip.
        seen = set()
        tags = []
        for tag in parse_lora_tags(text):
            key = (lora_lookup_key(tag["name"]),
                   tag["model_weight"], tag["clip_weight"])
            if key in seen:
                print(f"[Scorpiov Lora Tag Loader] Note: ignoring duplicate tag "
                      f"<lora:{tag['name']}:{tag['model_weight']}:"
                      f"{tag['clip_weight']}>")
                continue
            seen.add(key)
            tags.append(tag)

        resolved = _resolve_loras(tags)
        if resolved:
            model, clip = _load_loras(model, clip, resolved)
        else:
            print("[Scorpiov Lora Tag Loader] No <lora:...> tags found in text.")

        # Format for Scorpiov Save Image's "loras" field, one per line:
        #   "name (weight: 0.8) [hash: 6ce0161689]"
        # This is the exact format _parse_loras() in scorpiov_save_image.py
        # expects, so it can build the real Civitai "Lora hashes:" metadata
        # line (not just a placeholder) and re-embed the <lora:...> tag in
        # the saved prompt.
        info_lines = []
        for entry in resolved:
            line = f"{entry['name']} (weight: {entry['model_weight']})"
            if entry["hash"]:
                line += f" [hash: {entry['hash']}]"
            info_lines.append(line)
        loras_info = "\n".join(info_lines)

        return {
            "ui":     {"preview_text": [clean_text]},
            "result": (model, clip, clean_text, loras_info),
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovLoraTagLoader": ScorpiovLoraTagLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovLoraTagLoader": "Scorpiov Lora Tag Loader 🏷️",
}
