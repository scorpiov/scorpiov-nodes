"""
Scorpiov Wildcard Node for ComfyUI
----------------------------------------
A single node that:
  - Processes {a|b|c} inline wildcards
  - Loads __wildcard_file__ text files from the wildcards/ folder
    (searches ALL subfolders recursively — just use __filename__, no path needed)
  - Parses and loads <lora:name:weight> tags (searches all lora subfolders)
  - Encodes text to CONDITIONING
  - Supports Random or Serial selection modes
  - Serial mode remembers position and loops
  - Wildcard index is built at startup and refreshed on demand
"""

import os
import re
import random
import folder_paths
import comfy.sd
import comfy.utils
from server import PromptServer
from aiohttp import web


# ─────────────────────────────────────────────────────────────────────────────
#  Paths
#  The wildcards folder always lives inside the extension folder itself.
#  Users never need to configure this — just drop .txt files anywhere inside
#  ComfyUI/custom_nodes/scorpiov-nodes/wildcards/ and they are found.
# ─────────────────────────────────────────────────────────────────────────────

EXTENSION_DIR = os.path.dirname(os.path.abspath(__file__))
WILDCARDS_DIR = os.path.join(EXTENSION_DIR, "wildcards")


# ─────────────────────────────────────────────────────────────────────────────
#  Wildcard index
#
#  Built once at startup, rebuilt on refresh.
#  Maps lowercase filename stem -> full path, e.g.:
#    "color"      -> ".../wildcards/attributes/attrib/color.txt"
#    "hairstyles" -> ".../wildcards/hairstyles.txt"
#
#  Because it's a flat dict keyed by stem, __color__ works regardless
#  of which subfolder the file lives in.
# ─────────────────────────────────────────────────────────────────────────────

_wildcard_index: dict = {}   # stem (lowercase) -> full path


def _build_wildcard_index(folder=WILDCARDS_DIR):
    """
    Walk folder recursively, index every .txt file by its lowercase stem.
    If two files share a name, the shallowest one wins (alphabetical tiebreak).
    """
    index = {}
    if not os.path.isdir(folder):
        print(f"[Scorpiov Wildcard] Wildcards folder not found: {folder}")
        return index

    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for fname in sorted(files):
            if not fname.lower().endswith(".txt"):
                continue
            stem      = os.path.splitext(fname)[0].lower()
            full_path = os.path.join(root, fname)
            rel_path  = os.path.relpath(full_path, folder)
            if stem not in index:
                index[stem] = full_path
            else:
                existing_rel = os.path.relpath(index[stem], folder)
                print(f"[Scorpiov Wildcard] WARNING: duplicate name '{stem}': "
                      f"using '{existing_rel}', ignoring '{rel_path}'")
    return index


def _rebuild_index():
    """Rebuild the global wildcard index and log what was found."""
    global _wildcard_index
    _wildcard_index = _build_wildcard_index()
    count = len(_wildcard_index)
    print(f"[Scorpiov Wildcard] Wildcard index built — {count} file(s) found:")
    for stem in sorted(_wildcard_index.keys()):
        rel = os.path.relpath(_wildcard_index[stem], WILDCARDS_DIR)
        print(f"    __{stem}__  ->  wildcards/{rel}")


# Build the index immediately when ComfyUI loads this module
_rebuild_index()


# ─────────────────────────────────────────────────────────────────────────────
#  Serial state — tracks position per (node, wildcard) across runs
# ─────────────────────────────────────────────────────────────────────────────

_serial_state  = {}
_last_resolved = {}


def _get_serial_index(node_id, identifier, total, start_line):
    key = f"{node_id}::{identifier}"
    if key not in _serial_state:
        _serial_state[key] = max(0, min(start_line - 1, total - 1))
    idx = _serial_state[key]
    _serial_state[key] = (idx + 1) % total
    return idx


def _reset_serial_state(node_id):
    keys = [k for k in _serial_state if k.startswith(f"{node_id}::")]
    for k in keys:
        del _serial_state[k]
    print(f"[Scorpiov Wildcard] Serial state reset for node {node_id}")


# ─────────────────────────────────────────────────────────────────────────────
#  REST endpoint — Refresh button in the node UI
#  POST /scorpiov/wildcard/refresh
#  Rebuilds the wildcard index + resets serial state. No generation triggered.
# ─────────────────────────────────────────────────────────────────────────────

@PromptServer.instance.routes.post("/scorpiov/wildcard/refresh")
async def scorpiov_refresh_endpoint(request):
    try:
        data    = await request.json()
        node_id = str(data.get("node_id", ""))
        if node_id:
            _reset_serial_state(node_id)
        _rebuild_index()
        names = sorted(_wildcard_index.keys())
        return web.json_response({"status": "ok", "wildcards_found": names})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
#  Comment stripping — shared by wildcard files AND the node's own text box
#  so the rule only lives in one place (see dev reference §3.5 principle).
#
#    - line starting with '#'      -> whole line dropped
#    - '#' appearing after content -> text before '#' kept, rest dropped
#    - a line that is exactly '/*' -> starts a block comment
#    - a line that is exactly '*/' -> ends a block comment
#      (every line between the markers is dropped, however many there are)
# ─────────────────────────────────────────────────────────────────────────────

def _strip_comments_from_lines(lines):
    """
    Removes comments from a sequence of lines. Unlike the first version,
    '/*' and '*/' do NOT need to sit alone on their own line -- they are
    found anywhere in the text, the same way real C-style block comments
    work. This also lets a block comment start and end on the same line.

    Example, all of this is correctly treated as one comment even though
    the markers share lines with real text:
        /* this block
        is all a comment */
    """
    out = []
    in_block = False
    for raw in lines:
        line = raw.rstrip("\n")
        kept = []
        i = 0
        while i < len(line):
            if in_block:
                end_idx = line.find("*/", i)
                if end_idx == -1:
                    i = len(line)          # rest of this line is still comment
                else:
                    in_block = False
                    i = end_idx + 2        # resume normal scanning after */
                continue

            start_idx = line.find("/*", i)
            hash_idx  = line.find("#", i)
            candidates = [x for x in (start_idx, hash_idx) if x != -1]
            if not candidates:
                kept.append(line[i:])
                i = len(line)
                continue

            cut = min(candidates)
            kept.append(line[i:cut])

            if cut == hash_idx and (start_idx == -1 or hash_idx < start_idx):
                i = len(line)              # '#' comments run to end of line
            else:
                in_block = True
                i = cut + 2                # skip past the '/*' we just found

        cleaned = "".join(kept).strip()
        if cleaned:
            out.append(cleaned)

    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Wildcard file loading — uses the pre-built index
# ─────────────────────────────────────────────────────────────────────────────

def _load_wildcard(name):
    """
    Look up __name__ in the index and return its usable (non-comment,
    non-blank) lines. Case-insensitive. Returns [__name__] unchanged if not found.
    """
    stem = name.lower()
    path = _wildcard_index.get(stem)
    if path is None:
        print(f"[Scorpiov Wildcard] WARNING: __{name}__ not found in index. "
              f"Check wildcards/ folder or click Refresh.")
        return [f"__{name}__"]
    with open(path, "r", encoding="utf-8") as f:
        lines = _strip_comments_from_lines(f)
    if not lines:
        print(f"[Scorpiov Wildcard] WARNING: wildcard file is empty: {path}")
        return [f"__{name}__"]
    return lines


# ─────────────────────────────────────────────────────────────────────────────
#  Core wildcard processing
# ─────────────────────────────────────────────────────────────────────────────

def _pick(options, mode, node_id, identifier, start_line):
    if not options:
        return ""
    if mode == "random":
        return random.choice(options)
    idx = _get_serial_index(node_id, identifier, len(options), start_line)
    return options[idx]


def process_text(text, mode, start_line, node_id, seed):
    """
    0. Comments (# and /* */), typed directly in this text, are stripped first
    1. __filename__ -> looks up in pre-built index, picks a line
    2. {a|b|c}      -> picks one option (innermost groups first, supports nesting)
    """
    text = "\n".join(_strip_comments_from_lines(text.split("\n")))

    if mode == "random":
        random.seed(seed)

    inline_counter = [0]

    # Step 1: __file__ wildcards
    def replace_file_wildcard(m):
        name  = m.group(1)
        lines = _load_wildcard(name)
        return _pick(lines, mode, node_id, f"file::{name.lower()}", start_line)

    for _ in range(10):
        new_text = re.sub(r'__([a-zA-Z0-9_\-]+)__', replace_file_wildcard, text)
        if new_text == text:
            break
        text = new_text

    # Step 2: {a|b|c} inline groups
    def replace_inline_group(m):
        options = [o.strip() for o in m.group(1).split("|")]
        inline_counter[0] += 1
        return _pick(options, mode, node_id,
                     f"inline::{inline_counter[0]}", start_line)

    for _ in range(20):
        new_text = re.sub(r'\{([^{}]*)\}', replace_inline_group, text)
        if new_text == text:
            break
        text = new_text

    return text


# ─────────────────────────────────────────────────────────────────────────────
#  LoRA parsing and loading
# ─────────────────────────────────────────────────────────────────────────────

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
                    if stem not in index:
                        index[stem] = os.path.join(root, fname)
    return index


def _parse_loras(text):
    pattern = re.compile(r'<lora:([^:>\n]+):([0-9]*\.?[0-9]+)>')
    loras   = [(m.group(1).strip(), float(m.group(2)))
               for m in pattern.finditer(text)]
    clean   = pattern.sub('', text).strip()
    clean   = re.sub(r',\s*,', ',', clean)
    clean   = re.sub(r'\s{2,}', ' ', clean)
    return clean, loras


def _lora_lookup_key(name):
    """
    Returns the LoRA index lookup key for a name taken from a <lora:...> tag.
    Tags commonly include a subfolder prefix, e.g. <lora:Anima\\sshelen:0.8>
    or <lora:Anima/sshelen:0.8> -- but _build_lora_index() keys files by
    filename stem only (subfolder-agnostic). Strip any subfolder prefix
    (either slash style) before taking the stem, or a tag like that would
    silently fail to match and the LoRA would be skipped with no visible
    error other than a console warning.
    """
    basename = re.split(r'[\\/]+', name)[-1]
    return os.path.splitext(basename)[0].lower()


def _load_loras(model, clip, loras):
    if not loras:
        return model, clip
    print("[Scorpiov Wildcard] Building LoRA index...")
    lora_index = _build_lora_index()
    print(f"[Scorpiov Wildcard] {len(lora_index)} LoRA files indexed.")
    for name, weight in loras:
        stem = _lora_lookup_key(name)
        path = lora_index.get(stem)
        if path is None:
            print(f"[Scorpiov Wildcard] WARNING: LoRA not found: '{name}'")
            continue
        print(f"[Scorpiov Wildcard] Loading LoRA '{name}' @ {weight}")
        lora_sd = comfy.utils.load_torch_file(path, safe_load=True)
        model, clip = comfy.sd.load_lora_for_models(
            model, clip, lora_sd, weight, weight
        )
    return model, clip


# ─────────────────────────────────────────────────────────────────────────────
#  The Node
# ─────────────────────────────────────────────────────────────────────────────

class ScorpiovWildcardProcessor:
    """
    All-in-one wildcard processor.
    Drop .txt files anywhere inside the wildcards/ folder (any subfolder depth)
    and reference them with __filename__ — no path or configuration needed.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "clip":  ("CLIP",),
                "text":  ("STRING", {
                    "multiline": True,
                    "default":   "masterpiece, {girl|boy}, __hairstyles__",
                }),
                "mode": (["random", "serial"], {"default": "random"}),
                "seed": ("INT", {
                    "default": 0, "min": 0, "max": 0xffffffffffffffff,
                }),
                "serial_start_line": ("INT", {
                    "default": 1, "min": 1, "max": 9999,
                    "tooltip": "Serial mode: which line to start from (1 = first). "
                               "Applies to all wildcards in this prompt.",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES  = ("CONDITIONING", "MODEL", "CLIP", "STRING")
    RETURN_NAMES  = ("conditioning",  "model",  "clip",  "processed_text")
    FUNCTION      = "process"
    CATEGORY      = "Scorpiov/Prompt"
    OUTPUT_NODE   = True

    def process(self, model, clip, text, mode, seed,
                serial_start_line, unique_id="0"):

        node_id = str(unique_id)

        resolved = process_text(
            text=text, mode=mode, start_line=serial_start_line,
            node_id=node_id, seed=seed,
        )
        _last_resolved[node_id] = resolved
        print(f"[Scorpiov Wildcard] Resolved:\n{resolved}\n")

        clean_text, loras = _parse_loras(resolved)
        if loras:
            model, clip = _load_loras(model, clip, loras)

        tokens       = clip.tokenize(clean_text)
        conditioning = clip.encode_from_tokens_scheduled(tokens)

        return {
            "ui":     {"preview_text": [resolved]},
            "result": (conditioning, model, clip, resolved),
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")


# ─────────────────────────────────────────────────────────────────────────────
#  Scorpiov Wildcard Prompter
#
#  A text-only variant of the Wildcard Processor.
#  - No inputs whatsoever (no model, no clip)
#  - No LoRA loading (nothing to apply them to)
#  - Output is purely the resolved text string
#  - Same wildcard engine, same UI buttons and preview textarea
# ─────────────────────────────────────────────────────────────────────────────

class ScorpiovWildcardPrompter:
    """
    Text-only wildcard processor.
    Resolves __file__ wildcards and {a|b|c} inline groups in the prompt
    and outputs the resulting string — no model or clip required.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {
                    "multiline": True,
                    "default":   "masterpiece, {girl|boy}, __hairstyles__",
                }),
                "mode": (["random", "serial"], {"default": "random"}),
                "seed": ("INT", {
                    "default": 0, "min": 0, "max": 0xffffffffffffffff,
                }),
                "serial_start_line": ("INT", {
                    "default": 1, "min": 1, "max": 9999,
                    "tooltip": "Serial mode: which line to start from (1 = first). "
                               "Applies to all wildcards in this prompt.",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES  = ("STRING",)
    RETURN_NAMES  = ("processed_text",)
    FUNCTION      = "process"
    CATEGORY      = "Scorpiov/Prompt"
    OUTPUT_NODE   = True

    def process(self, text, mode, seed, serial_start_line, unique_id="0"):

        node_id = str(unique_id)

        resolved = process_text(
            text=text, mode=mode, start_line=serial_start_line,
            node_id=node_id, seed=seed,
        )
        _last_resolved[node_id] = resolved
        print(f"[Scorpiov Wildcard Prompter] Resolved:\n{resolved}\n")

        return {
            "ui":     {"preview_text": [resolved]},
            "result": (resolved,),
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovWildcardProcessor": ScorpiovWildcardProcessor,
    "ScorpiovWildcardPrompter":  ScorpiovWildcardPrompter,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovWildcardProcessor": "Scorpiov Wildcard Processor 🎲",
    "ScorpiovWildcardPrompter":  "Scorpiov Wildcard Prompter 📝",
}
