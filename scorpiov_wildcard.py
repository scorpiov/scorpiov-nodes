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

# Canonical LoRA-tag parsing lives in scorpiov_common.py, shared with the
# Lora Tag Loader / Image Meta / Save Image so the copies can't drift
# (they had: this file's pattern was case-sensitive and had no clip-weight
# support, so <LoRA:...> tags were neither loaded nor stripped).
try:
    from .scorpiov_common import (
        parse_lora_tags, strip_lora_tags, lora_lookup_key,
    )
except ImportError:  # loaded as a loose module, not as part of the package
    from scorpiov_common import (
        parse_lora_tags, strip_lora_tags, lora_lookup_key,
    )


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

# Parsed-line cache for wildcard files: stem -> (mtime, size, [lines]).
# _load_wildcard used to re-open, re-read and re-strip the file on EVERY
# reference; a stat() per load is the cheap invalidation check instead.
_wildcard_file_cache: dict = {}


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
            # The __token__ syntax can only reference stems made of
            # [a-z0-9_-] — a file whose name needs other characters (spaces,
            # dots, accents) is indexed but can never be expanded. Say so
            # loudly at build time instead of failing silently at run time.
            if not re.fullmatch(r'[a-z0-9_\-]+', stem):
                print(f"[Scorpiov Wildcard] WARNING: '{rel_path}' can never be "
                      f"referenced as __{stem}__ (name contains characters "
                      f"outside a-z, 0-9, _ and -). Rename the file to use it.")
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
    _wildcard_file_cache.clear()
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
    """
    State is stored as (next_index, start_line_at_last_call).

    Two fixes over the original version:
      - The stored index is taken modulo the CURRENT total on read. If a
        wildcard file was edited shorter since the state was stored, the
        old code indexed past the end of the list and crashed the run.
      - start_line is honoured when it CHANGES, not only on first use —
        but a merely-present start_line must NOT reset the position on
        every call: the Batch Prompt Runner submits a whole batch upfront
        with serial_start_line pinned at 1 and relies on auto-increment
        carrying it through the batch (see js/scorpiov_batch_runner.js).
        Change detection gives both behaviours: turn the widget and the
        sequence restarts; leave it alone and the sequence continues.
    """
    key = f"{node_id}::{identifier}"
    stored = _serial_state.get(key)
    if stored is None or stored[1] != start_line:
        idx = max(0, min(start_line - 1, total - 1))
    else:
        idx = stored[0] % total
    _serial_state[key] = ((idx + 1) % total, start_line)
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
        # Body is optional — a POST with no/empty JSON body must still
        # refresh (request.json() raises on an empty body, which used to
        # turn a trivial refresh into a 500).
        try:
            data = await request.json()
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        node_id = str(data.get("node_id", ""))
        if node_id:
            _reset_serial_state(node_id)
        _rebuild_index()
        _invalidate_lora_index()
        names = sorted(_wildcard_index.keys())
        return web.json_response({"status": "ok", "wildcards_found": names})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
#  REST endpoint — used by the Batch Prompt Runner to find out how many
#  usable lines a wildcard file has, so it knows how many queue jobs to
#  submit per checkpoint. Reuses the same index + loader as everything
#  else, so "how many lines" always matches what serial mode will
#  actually walk through (comments/blank lines excluded identically).
#  GET /scorpiov/wildcard/line_count?name=<wildcard name>
# ─────────────────────────────────────────────────────────────────────────────

@PromptServer.instance.routes.get("/scorpiov/wildcard/line_count")
async def scorpiov_line_count_endpoint(request):
    name = request.query.get("name", "")
    if not name:
        return web.json_response({"status": "error", "message": "missing 'name' query param"}, status=400)
    stem = name.lower()
    found = stem in _wildcard_index
    try:
        line_count = len(_load_wildcard(name)) if found else 0
    except Exception as e:
        # The file vanished or became unreadable between index build and
        # this request. Answer in the shape the Batch Prompt Runner
        # already understands (found: false) instead of an HTML 500 page
        # its .json() call would choke on.
        print(f"[Scorpiov Wildcard] line_count failed for {name!r}: {e}")
        return web.json_response({
            "status": "error", "message": str(e),
            "name": name, "found": False, "line_count": 0,
        })
    return web.json_response({
        "status": "ok",
        "name": name,
        "found": found,
        "line_count": line_count,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Comment stripping — shared by wildcard files AND the node's own text box
#  so the rule only lives in one place (see dev reference §3.5 principle).
#  validate_wildcards.py carries an intentional copy of this function;
#  keep the two in sync (its header says so too).
#
#  The rules (this comment, the implementation, and the validator all
#  describe the same behaviour — they had drifted apart once already):
#    - '#' anywhere outside a block comment starts a comment that runs
#      to the end of the line; text before it is kept.
#    - '\#' is an escape: it produces a literal '#' and does NOT start a
#      comment (for hex colours like \#FF5500, issue numbers, etc.).
#    - '/*' ... '*/' are C-style block comments, recognised anywhere in
#      the text; a block may start and end on the same line or span lines.
# ─────────────────────────────────────────────────────────────────────────────

def _strip_comments_from_lines(lines):
    """
    Removes comments from a sequence of lines, per the rules in the
    section header above. Single left-to-right scan per line, with the
    block-comment state carried across lines.
    """
    out = []
    in_block = False
    for raw in lines:
        line = raw.rstrip("\n")
        kept = []
        i = 0
        n = len(line)
        while i < n:
            if in_block:
                end_idx = line.find("*/", i)
                if end_idx == -1:
                    i = n                  # rest of this line is still comment
                else:
                    in_block = False
                    i = end_idx + 2        # resume normal scanning after */
                continue

            ch = line[i]
            if ch == "\\" and i + 1 < n and line[i + 1] == "#":
                kept.append("#")           # \# escape -> literal '#'
                i += 2
                continue
            if ch == "#":
                i = n                      # '#' comments run to end of line
                continue
            if ch == "/" and i + 1 < n and line[i + 1] == "*":
                in_block = True
                i += 2                     # skip past the '/*' we just found
                continue
            kept.append(ch)
            i += 1

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

    Parsed lines are cached per file (keyed by mtime+size, checked with a
    single stat per call) — previously the file was re-opened and
    re-stripped on every single reference in a prompt. Files are read as
    utf-8-sig with replacement on bad bytes: a BOM saved by a Windows
    editor no longer corrupts the first line, and one stray non-UTF8 byte
    no longer crashes resolution.
    """
    stem = name.lower()
    path = _wildcard_index.get(stem)
    if path is None:
        print(f"[Scorpiov Wildcard] WARNING: __{name}__ not found in index. "
              f"Check wildcards/ folder or click Refresh.")
        return [f"__{name}__"]
    try:
        st = os.stat(path)
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None:
        cached = _wildcard_file_cache.get(stem)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = _strip_comments_from_lines(f)
    if not lines:
        print(f"[Scorpiov Wildcard] WARNING: wildcard file is empty: {path}")
        return [f"__{name}__"]
    if cache_key is not None:
        _wildcard_file_cache[stem] = (cache_key, lines)
    return lines


# ─────────────────────────────────────────────────────────────────────────────
#  Core wildcard processing
# ─────────────────────────────────────────────────────────────────────────────

def _pick(options, mode, node_id, identifier, start_line, rng=None):
    if not options:
        return ""
    if mode == "random":
        # A private Random instance, never the module-global one: seeding
        # the global RNG (the old random.seed(seed)) silently reset the
        # sequence for every other node/library in the ComfyUI process.
        return (rng or random).choice(options)
    idx = _get_serial_index(node_id, identifier, len(options), start_line)
    return options[idx]


def process_text(text, mode, start_line, node_id, seed):
    """
    Resolves wildcards in this order, interleaved until stable:

      1. Strip comments (# and /* */) from the typed text first.
      2. Alternate between two sub-steps until no more changes occur:
           a. Resolve the innermost {a|b|c} group — pick ONE option.
           b. Expand any __file__ wildcard tokens that are now exposed.

    Interleaving is the key fix: previously, ALL __file__ tokens were
    expanded before ANY {} group was resolved. That meant
    {__anima01__|__anima02__|...} expanded every file upfront, then split
    on | — but the expanded file content often contains | characters itself
    (from nested {a|b} groups or LoRA weights), causing the outer group
    split to break across the wrong boundaries and bleed content from
    multiple wildcard files into one prompt.

    With interleaving, the {} group picks its winning option FIRST, then
    only that winner's __file__ reference is expanded — so content from
    the unchosen options never enters the prompt.
    """
    text = "\n".join(_strip_comments_from_lines(text.split("\n")))

    # Reproducibility note: with a fixed seed, random mode resolves the
    # same way every run — that's ComfyUI's seed convention, and the
    # frontend's per-queue "randomize" control on the seed widget is what
    # varies it. What changed here is only WHERE the randomness lives:
    # a private Random(seed) instance instead of seeding the global RNG.
    rng = random.Random(seed) if mode == "random" else None

    inline_counter = [0]

    def replace_file_wildcard(m):
        name  = m.group(1)
        lines = _load_wildcard(name)
        return _pick(lines, mode, node_id, f"file::{name.lower()}", start_line, rng)

    def replace_inline_group(m):
        options = [o.strip() for o in m.group(1).split("|")]
        inline_counter[0] += 1
        return _pick(options, mode, node_id,
                     f"inline::{inline_counter[0]}", start_line, rng)

    # Interleave {} resolution and __file__ expansion until stable.
    # Each outer loop pass does ONE of each sub-step.
    # We cap at 20 outer passes (400 total ops) to prevent infinite loops
    # from pathological self-referencing wildcard files — and cap the
    # total text size as well: mutually-referencing files can grow the
    # string exponentially WITHIN the pass cap, so the cap alone didn't
    # prevent a memory blow-up. Past the ceiling we fail loudly instead.
    MAX_RESOLVED_CHARS = 500_000
    for _ in range(20):
        if len(text) > MAX_RESOLVED_CHARS:
            raise RuntimeError(
                f"[Scorpiov Wildcard] Resolved prompt exceeded "
                f"{MAX_RESOLVED_CHARS} characters — wildcard files are "
                f"almost certainly referencing each other in a loop. "
                f"Check the __file__ references in this prompt."
            )
        changed = False

        # a. Resolve one layer of innermost {} groups
        new_text = re.sub(r'\{([^{}]*)\}', replace_inline_group, text)
        if new_text != text:
            text    = new_text
            changed = True

        # b. Expand any __file__ tokens now visible (one pass)
        new_text = re.sub(r'__([a-zA-Z0-9_\-]+)__', replace_file_wildcard, text)
        if new_text != text:
            text    = new_text
            changed = True

        if not changed:
            break

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
                    full = os.path.join(root, fname)
                    if stem not in index:
                        index[stem] = full
                    elif index[stem] != full:
                        # Same filename stem in two subfolders: the index
                        # is stem-only (subfolder-agnostic by design), so
                        # one of them silently wins. Previously that choice
                        # was invisible — the user just got the wrong LoRA.
                        # Keep first-found, but say so where they can see.
                        print(f"[Scorpiov Wildcard] WARNING: LoRA name "
                              f"collision '{stem}': using "
                              f"'{index[stem]}', ignoring '{full}'. Tags "
                              f"cannot distinguish same-named LoRAs in "
                              f"different subfolders.")
    return index


# The index is built ONCE and reused — it used to be rebuilt with a full
# os.walk of every LoRA folder on every single generation that contained
# a tag. Refresh Wildcards (the node's button / endpoint) invalidates it.
_lora_index_cache = {"index": None}


def _get_lora_index():
    if _lora_index_cache["index"] is None:
        print("[Scorpiov Wildcard] Building LoRA index...")
        _lora_index_cache["index"] = _build_lora_index()
        print(f"[Scorpiov Wildcard] {len(_lora_index_cache['index'])} LoRA files indexed.")
    return _lora_index_cache["index"]


def _invalidate_lora_index():
    _lora_index_cache["index"] = None


def _parse_loras(text):
    """
    Returns (clean_text, loras) where loras is a list of
    (name, model_weight, clip_weight) tuples — via the canonical parser
    in scorpiov_common, so <LoRA:...> (any case), negative weights, and
    the three-part <lora:name:w:clip_w> form all work, and the tags are
    stripped from the text that goes to the encoder either way.
    """
    tags = parse_lora_tags(text)
    loras = [(t["name"], t["model_weight"], t["clip_weight"]) for t in tags]
    return strip_lora_tags(text), loras


def _lora_lookup_key(name):
    """
    Kept for backwards compatibility — the implementation is the shared
    one in scorpiov_common (strips any subfolder prefix, either slash
    style, plus the extension, and lowercases the stem).
    """
    return lora_lookup_key(name)


def _load_loras(model, clip, loras):
    if not loras:
        return model, clip
    lora_index = _get_lora_index()
    seen = set()
    for name, model_weight, clip_weight in loras:
        stem = lora_lookup_key(name)
        dedupe_key = (stem, model_weight, clip_weight)
        if dedupe_key in seen:
            # The same tag expanded twice (a common wildcard accident)
            # used to apply the LoRA twice, silently doubling its effect.
            print(f"[Scorpiov Wildcard] Duplicate LoRA tag skipped: '{name}'")
            continue
        seen.add(dedupe_key)
        path = lora_index.get(stem)
        if path is None:
            print(f"[Scorpiov Wildcard] WARNING: LoRA not found: '{name}'")
            continue
        if clip_weight != model_weight:
            print(f"[Scorpiov Wildcard] Loading LoRA '{name}' @ model={model_weight}, clip={clip_weight}")
        else:
            print(f"[Scorpiov Wildcard] Loading LoRA '{name}' @ {model_weight}")
        lora_sd = comfy.utils.load_torch_file(path, safe_load=True)
        model, clip = comfy.sd.load_lora_for_models(
            model, clip, lora_sd, model_weight, clip_weight
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
                    # Show the frontend's randomize/fixed control: a fixed
                    # seed reproduces the same picks (by design), randomize
                    # is how "random" varies per queue.
                    "control_after_generate": True,
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

    # OUTPUT_NODE=True on both wildcard classes is deliberate, not an
    # oversight: the preview + the Batch Prompt Runner rely on these nodes
    # executing on every queue. IS_CHANGED=NaN is the matching choice —
    # random and serial modes are meaningless if ComfyUI caches the result.
    # The cost (downstream nodes also re-run each queue) is accepted and
    # documented here so nobody "optimises" it away blindly.
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
                    "control_after_generate": True,
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
