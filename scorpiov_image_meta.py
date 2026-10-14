"""
Scorpiov Image Meta Reader
--------------------------
Reads metadata embedded in a PNG image and extracts:
  - Checkpoint / model name
  - VAE name
  - LoRA names and weights
  - Positive prompt
  - Negative prompt
  - Raw metadata dump (full text)

Supports two formats:
  1. ComfyUI native  — JSON stored in the PNG "prompt" tEXt chunk
  2. A1111 / AUTOMATIC1111 — plain text stored in the PNG "parameters" tEXt chunk

The node receives the image as a file path string (from Load Image's
"filename" output, or typed manually). It reads the file directly from
disk so metadata is never lost (ComfyUI strips metadata when it converts
a PNG to a tensor).
"""

import os
import json
import re
import folder_paths
from PIL import Image
from server import PromptServer
from aiohttp import web

# Canonical LoRA-tag parsing + node-type sets live in scorpiov_common.py —
# every parser in this pack shares them so the copies can't drift apart
# again (they had: different case sensitivity, different clip-weight
# support, Meta handling UNETLoader while Save Image didn't).
try:
    from .scorpiov_common import (
        parse_lora_tags, lora_display_name,
        CHECKPOINT_TYPES, UNET_TYPES, LORA_LOADER_TYPES, VAE_LOADER_TYPES,
        KSAMPLER_TYPES, CLIP_ENCODE_TYPES, safe_float,
    )
except ImportError:  # loaded as a loose module, not as part of the package
    from scorpiov_common import (
        parse_lora_tags, lora_display_name,
        CHECKPOINT_TYPES, UNET_TYPES, LORA_LOADER_TYPES, VAE_LOADER_TYPES,
        KSAMPLER_TYPES, CLIP_ENCODE_TYPES, safe_float,
    )


def _extract_lora_tags(text: str) -> list:
    """
    LoRAs applied via <lora:name:weight> tags typed straight into prompt
    text (loaded by Scorpiov Lora Tag Loader) never show up as a LoraLoader
    node in the graph, so we scan resolved prompt text for tags directly.
    Returns [{"name": display name, "weight": model weight}, ...].
    """
    return [
        {"name": t["display_name"], "weight": t["model_weight"]}
        for t in parse_lora_tags(text or "")
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Helper: resolve a filename to a full path
#  ComfyUI Load Image nodes output just the filename (e.g. "photo.png").
#  The file lives in the ComfyUI input/ folder.
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_path(filename: str) -> str | None:
    """
    Try to find the image file on disk.
    Accepts:
      - An absolute path (already complete)
      - An annotated path ("[output] name.png" style, via folder_paths)
      - A bare filename  → looked up in ComfyUI's input, output, and temp
        folders (images people want metadata from usually live in output/)
    Returns the full path if found, or None.

    NOTE: folder_paths.get_input_directory() returns a SINGLE path string,
    not a list — an earlier version of this function iterated it directly
    and walked the string character by character, so bare-filename lookup
    could never match. It is treated as one folder below.
    """
    if not filename:
        return None

    if os.path.isabs(filename) and os.path.isfile(filename):
        return filename

    # Annotated paths ("subfolder/name.png", "[output] name.png", ...)
    if hasattr(folder_paths, "get_annotated_filepath"):
        try:
            candidate = folder_paths.get_annotated_filepath(filename)
            if candidate and os.path.isfile(candidate):
                return os.path.abspath(candidate)
        except Exception:
            pass

    # ComfyUI's input / output / temp folders — each getter returns one str.
    for getter_name in ("get_input_directory", "get_output_directory",
                        "get_temp_directory"):
        getter = getattr(folder_paths, getter_name, None)
        if getter is None:
            continue
        try:
            folder = getter()
        except Exception:
            continue
        if isinstance(folder, str) and folder:
            candidate = os.path.join(folder, filename)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

    # Also try the filename directly (relative path from CWD)
    if os.path.isfile(filename):
        return os.path.abspath(filename)

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  PNG metadata extraction
# ─────────────────────────────────────────────────────────────────────────────

def _read_png_meta(path: str) -> dict:
    """
    Open a PNG with Pillow and return its tEXt/iTXt metadata as a plain dict.
    Keys are chunk keywords (e.g. "parameters", "prompt", "workflow").

    The file is opened with a context manager (an unclosed handle locks
    the file on Windows). A full pixel decode is only forced if the cheap
    header read didn't surface any known metadata keys — PNG text chunks
    written after the image data are only guaranteed visible after load().
    """
    try:
        with Image.open(path) as img:
            meta = dict(img.info or {})
            # Pillow also exposes parsed text chunks via .text on PNGs —
            # merge anything info missed.
            text_chunks = getattr(img, "text", None)
            if isinstance(text_chunks, dict):
                for key, value in text_chunks.items():
                    meta.setdefault(key, value)
            if not any(k in meta for k in ("prompt", "parameters", "workflow")):
                img.load()
                meta = dict(img.info or {})
                if isinstance(text_chunks, dict):
                    for key, value in text_chunks.items():
                        meta.setdefault(key, value)
            return meta
    except Exception as e:
        print(f"[Scorpiov Meta] Could not open image: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
#  A1111 parser
#
#  Format:
#    <positive prompt text>
#    Negative prompt: <negative prompt text>
#    Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 123, Size: 512x512,
#    Model hash: abc123, Model: v1-5-pruned-emaonly, VAE hash: ..., VAE: vae.pt,
#    Lora hashes: "name: hash", ..., Version: ...
# ─────────────────────────────────────────────────────────────────────────────

def _parse_a1111(text: str) -> dict:
    result = {
        "format":          "A1111",
        "positive_prompt": "",
        "negative_prompt": "",
        "model":           "",
        "vae":             "",
        "loras":           [],
        "raw":             text,
    }

    # Split on "Negative prompt:" to separate positive from the rest
    neg_split = re.split(r'\nNegative prompt\s*:', text, maxsplit=1, flags=re.IGNORECASE)
    result["positive_prompt"] = neg_split[0].strip()

    remainder = ""
    if len(neg_split) > 1:
        # Split the negative+params section on the first line that looks like
        # "Steps: ..." to isolate the negative prompt text
        steps_split = re.split(r'\nSteps\s*:', neg_split[1], maxsplit=1, flags=re.IGNORECASE)
        result["negative_prompt"] = steps_split[0].strip()
        remainder = ("Steps:" + steps_split[1]) if len(steps_split) > 1 else ""
    else:
        # No negative prompt — the whole second half after the first newline
        # block is the params line
        lines = text.split("\n")
        # Find the first line that starts with "Steps:"
        param_start = next(
            (i for i, l in enumerate(lines) if re.match(r'Steps\s*:', l, re.IGNORECASE)),
            None
        )
        if param_start is not None:
            result["positive_prompt"] = "\n".join(lines[:param_start]).strip()
            remainder = "\n".join(lines[param_start:])

    # Extract key: value pairs from the params line(s)
    def _grab(pattern):
        m = re.search(pattern, remainder, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    result["model"] = _grab(r'(?<!\w)Model\s*:\s*([^,\n]+)')
    result["vae"]   = _grab(r'(?<!\w)VAE\s*:\s*([^,\n]+)')

    # LoRAs in A1111 appear inline in the prompt as <lora:name:weight>
    # (canonical parser from scorpiov_common — also handles the
    # <lora:name:weight:clip_weight> form and negative weights, which a
    # local copy of the regex in this function used to miss entirely)
    for tag in parse_lora_tags(text):
        result["loras"].append({"name": tag["name"], "weight": tag["model_weight"]})

    # Also check "Lora hashes" field for names not in prompt tags
    lora_hashes_raw = _grab(r'Lora hashes\s*:\s*"([^"]*)"')
    if lora_hashes_raw:
        for entry in lora_hashes_raw.split(","):
            name_part = entry.split(":")[0].strip()
            if name_part and not any(l["name"].lower() == name_part.lower()
                                     for l in result["loras"]):
                result["loras"].append({"name": name_part, "weight": None})

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  ComfyUI native parser
#
#  The "prompt" chunk is a JSON object whose keys are node IDs (strings).
#  Each value is: { "class_type": "...", "inputs": { ... } }
#
#  Node types we care about:
#    CheckpointLoaderSimple / CheckpointLoader  → ckpt_name
#    VAELoader                                  → vae_name
#    LoraLoader / LoraLoaderModelOnly           → lora_name, strength_model
#    CLIPTextEncode                             → text  (positive / negative)
#    KSampler / KSamplerAdvanced                → (links positive/negative)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_comfyui(json_text: str) -> dict:
    result = {
        "format":          "ComfyUI",
        "positive_prompt": "",
        "negative_prompt": "",
        "model":           "",
        "vae":             "",
        "loras":           [],
        "raw":             json_text,
    }

    try:
        graph = json.loads(json_text)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[Scorpiov Meta] Could not parse ComfyUI JSON: {e}")
        result["raw"] = json_text
        return result

    # A reader node must degrade, never throw: a "prompt" chunk that
    # decodes to something other than a node dict (list, bare string...)
    # is reported as unparseable instead of crashing on .items() below.
    if not isinstance(graph, dict):
        print("[Scorpiov Meta] ComfyUI prompt chunk was not a node graph object.")
        return result

    # ── Index all nodes by ID ─────────────────────────────────────────────
    nodes = {}   # id (str) -> node dict
    for node_id, node_data in graph.items():
        if isinstance(node_data, dict) and "class_type" in node_data:
            nodes[str(node_id)] = node_data

    # ── Helper: resolve an input that may be a [node_id, output_index] link ──
    def resolve(value):
        if isinstance(value, list) and len(value) == 2:
            ref_id, _output_idx = str(value[0]), value[1]
            return nodes.get(ref_id)
        return None

    # Node-type sets are the canonical ones from scorpiov_common (shared
    # with Save Image so the two files can't recognise different loaders).
    # Field values are guarded with isinstance(str): a widget converted to
    # an input arrives as a [node_id, idx] link, and os.path / float() on a
    # list used to crash the whole parse.

    # ── Checkpoint ────────────────────────────────────────────────────────
    for node in nodes.values():
        if node.get("class_type") in CHECKPOINT_TYPES:
            ckpt = node.get("inputs", {}).get("ckpt_name", "")
            if isinstance(ckpt, str) and ckpt:
                result["model"] = lora_display_name(ckpt)
                break

    # Some workflows (e.g. UNet-only / Flux-style setups) load the model via
    # UNETLoader instead of a checkpoint node. Check this separately since it
    # uses a different field name ("unet_name" vs "ckpt_name").
    if not result["model"]:
        for node in nodes.values():
            if node.get("class_type") in UNET_TYPES:
                unet = node.get("inputs", {}).get("unet_name", "")
                if isinstance(unet, str) and unet:
                    result["model"] = lora_display_name(unet)
                    break

    # ── VAE ───────────────────────────────────────────────────────────────
    for node in nodes.values():
        if node.get("class_type") in VAE_LOADER_TYPES:
            vae = node.get("inputs", {}).get("vae_name", "")
            if isinstance(vae, str) and vae:
                result["vae"] = lora_display_name(vae)
                break

    # ── LoRAs ─────────────────────────────────────────────────────────────
    for node in nodes.values():
        if node.get("class_type") in LORA_LOADER_TYPES:
            inputs = node.get("inputs", {})
            name   = inputs.get("lora_name", "")
            weight = inputs.get("strength_model", inputs.get("strength", 1.0))
            if isinstance(name, str) and name:
                result["loras"].append({
                    "name":   lora_display_name(name),
                    # safe_float returns the default for links/junk instead
                    # of raising TypeError the way float(list) did.
                    "weight": safe_float(weight, 1.0),
                })

    # ── Prompts ───────────────────────────────────────────────────────────
    # Strategy: find KSampler nodes and follow their positive/negative links
    # back to CLIPTextEncode nodes to get the actual text.
    # KSAMPLER_TYPES / CLIP_ENCODE_TYPES: canonical sets from scorpiov_common.

    MAX_HOPS = 8

    def resolve_value_text(value, depth=0):
        """
        A field's value can be a plain string, or a [node_id, output_index]
        link. If it's a link, follow it to the source node and ask that node
        for its text via resolve_node_text(). Keeps hopping until it bottoms
        out at real text or MAX_HOPS is hit.
        """
        if depth > MAX_HOPS or value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list) and len(value) == 2:
            source = nodes.get(str(value[0]))
            return resolve_node_text(source, depth + 1)
        return ""

    def resolve_node_text(node, depth=0):
        """
        Given a node, figure out "the text this node represents" — handling
        a few specific node types whose text doesn't live in a field simply
        called "text", plus a generic fallback for everything else.
        """
        if node is None or depth > MAX_HOPS:
            return ""
        ct = node.get("class_type", "")
        inputs = node.get("inputs", {})

        # WAS Node Suite "Text Concatenate" — text is split across
        # text_a / text_b / text_c / text_d (+ a delimiter), not one "text"
        # field. Resolve each piece (which may itself be wired) and join
        # them in order using the node's own delimiter.
        if ct == "Text Concatenate":
            parts = []
            for key in sorted(k for k in inputs if k.startswith("text_")):
                parts.append(resolve_value_text(inputs[key], depth + 1))
            delimiter = inputs.get("delimiter", ", ")
            if not isinstance(delimiter, str):
                delimiter = ", "
            return delimiter.join(p for p in parts if p)

        # Scorpiov Lora Tag Loader — its own "text" input is itself the
        # upstream source text (it strips <lora:...> tags on its way OUT,
        # but the wired-in "text" input is the original prompt). Follow it
        # rather than treating this node as a dead end.
        if ct == "ScorpiovLoraTagLoader":
            return resolve_value_text(inputs.get("text"), depth + 1)

        # Generic fallback: try every commonly-used field name for text,
        # following further links if the field itself is wired.
        for field in ("text", "value", "string", "prompt"):
            if field in inputs:
                resolved = resolve_value_text(inputs[field], depth + 1)
                if resolved:
                    return resolved

        return ""  # node type/shape we don't recognise — give up

    def get_text_from_node(n, _depth=0):
        if n is None or _depth > MAX_HOPS:
            return ""
        ct = n.get("class_type", "")
        if ct in CLIP_ENCODE_TYPES:
            raw = n.get("inputs", {}).get("text", "")
            return resolve_value_text(raw)
        # Some setups chain through conditioning nodes — follow one more level
        cond_input = n.get("inputs", {}).get("conditioning")
        if isinstance(cond_input, list):
            deeper = resolve(cond_input)
            if deeper is not None and deeper is not n:
                text = get_text_from_node(deeper, _depth + 1)
                if text:
                    return text
        # Everything else — including this pack's own Wildcard Processor,
        # which outputs CONDITIONING directly from a "text" input — goes
        # through the general resolver. The old sampler path only knew
        # CLIPTextEncode, so wildcard-driven workflows (this pack's main
        # workflow!) came back with empty prompts.
        return resolve_node_text(n)

    positive_texts = []
    negative_texts = []

    for node in nodes.values():
        if node.get("class_type") not in KSAMPLER_TYPES:
            continue
        inputs = node.get("inputs", {})

        pos_node = resolve(inputs.get("positive"))
        neg_node = resolve(inputs.get("negative"))

        pos_text = get_text_from_node(pos_node)
        neg_text = get_text_from_node(neg_node)

        if pos_text and pos_text not in positive_texts:
            positive_texts.append(pos_text)
        if neg_text and neg_text not in negative_texts:
            negative_texts.append(neg_text)

    # If no KSampler found, fall back: collect all CLIPTextEncode nodes
    if not positive_texts and not negative_texts:
        clip_texts = []
        for node in nodes.values():
            if node.get("class_type") in CLIP_ENCODE_TYPES:
                raw = node.get("inputs", {}).get("text", "")
                t = resolve_value_text(raw)
                if t:
                    clip_texts.append(t)
        # Heuristic: longer text = positive, shorter = negative
        clip_texts.sort(key=len, reverse=True)
        if clip_texts:
            positive_texts = [clip_texts[0]]
        if len(clip_texts) > 1:
            negative_texts = [clip_texts[1]]

    result["positive_prompt"] = "\n---\n".join(positive_texts)
    result["negative_prompt"] = "\n---\n".join(negative_texts)

    # Pick up LoRAs applied via <lora:name:weight> tags in prompt text
    # (Scorpiov Lora Tag Loader workflows) — these never appear as a
    # LoraLoader node, so they're invisible to the node-scan above.
    existing_names = {l["name"].lower() for l in result["loras"]}
    combined_text = "\n".join(positive_texts + negative_texts)
    for tag in _extract_lora_tags(combined_text):
        if tag["name"].lower() not in existing_names:
            result["loras"].append(tag)
            existing_names.add(tag["name"].lower())

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Main dispatch: detect format and parse
# ─────────────────────────────────────────────────────────────────────────────

def extract_metadata(path: str) -> dict:
    """
    Read a PNG file and return a parsed metadata dict with keys:
      format, positive_prompt, negative_prompt, model, vae, loras, raw
    """
    meta = _read_png_meta(path)

    empty = {
        "format":          "none",
        "positive_prompt": "",
        "negative_prompt": "",
        "model":           "",
        "vae":             "",
        "loras":           [],
        "raw":             "",
    }

    def _as_text(value):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

    # Gather BOTH formats when present instead of returning the first one
    # found. This matters most for images saved by Scorpiov Save Image:
    # they carry a ComfyUI "prompt" graph AND an A1111 "parameters" block,
    # and the two do NOT hold the same prompt text. The graph stores node
    # inputs as typed — for wildcard workflows that's the unresolved
    # template (with __file__ tokens and {a|b} groups still in it) — while
    # "parameters" stores the prompt that was actually executed. The old
    # graph-first dispatch therefore showed the template, not the prompt
    # the image was really made with. Rule now: "parameters" wins for
    # prompt text (and as first choice for model/vae/loras); the graph
    # fills any gaps and supplies the raw workflow dump.
    parsed_params = None
    if meta.get("parameters"):
        parsed_params = _parse_a1111(_as_text(meta["parameters"]))

    parsed_graph = None
    raw_prompt = None
    if meta.get("prompt"):
        raw_prompt = _as_text(meta["prompt"])
        if isinstance(raw_prompt, str):
            parsed_graph = _parse_comfyui(raw_prompt)

    if parsed_params is not None and parsed_graph is not None:
        merged = dict(parsed_graph)
        merged["format"] = "ComfyUI+A1111"
        for field in ("positive_prompt", "negative_prompt", "model", "vae"):
            if parsed_params.get(field):
                merged[field] = parsed_params[field]
        if parsed_params["loras"]:
            merged["loras"] = parsed_params["loras"]
        workflow_text = _as_text(meta["workflow"]) if meta.get("workflow") else None
        raw_parts = [f"=== parameters ===\n{_as_text(meta['parameters'])}",
                     f"=== prompt ===\n{raw_prompt}"]
        if workflow_text:
            raw_parts.append(f"=== workflow ===\n{workflow_text}")
        merged["raw"] = "\n\n".join(raw_parts)
        return merged

    if parsed_graph is not None:
        if meta.get("workflow"):
            parsed_graph["raw"] = (
                f"=== prompt ===\n{raw_prompt}\n\n"
                f"=== workflow ===\n{_as_text(meta['workflow'])}"
            )
        return parsed_graph

    if parsed_params is not None:
        return parsed_params

    # Nothing found
    print(f"[Scorpiov Meta] No recognised metadata found in: {path}")
    empty["raw"] = f"No metadata found.\n\nAll PNG chunks present: {list(meta.keys())}"
    return empty


# ─────────────────────────────────────────────────────────────────────────────
#  REST endpoint — called by the JS side to read metadata for display
#  POST /scorpiov/imagemeta/read   { "path": "filename.png" }
# ─────────────────────────────────────────────────────────────────────────────

@PromptServer.instance.routes.post("/scorpiov/imagemeta/read")
async def scorpiov_imagemeta_read(request):
    try:
        data     = await request.json()
        filename = data.get("path", "").strip()
        if not filename:
            return web.json_response({"status": "error", "message": "No path provided."}, status=400)

        full_path = _resolve_path(filename)
        if full_path is None:
            return web.json_response(
                {"status": "error", "message": f"File not found: {filename!r}"},
                status=404,
            )

        parsed = extract_metadata(full_path)

        # Format LoRAs
        lora_lines = []
        for l in parsed["loras"]:
            w = f"{l['weight']:.2f}" if l["weight"] is not None else "?"
            lora_lines.append(f"{l['name']} (weight: {w})")
        loras_str = "\n".join(lora_lines) if lora_lines else "(none)"
        parsed["loras_display"] = loras_str

        # Build the single combined display string for the textarea
        parsed["display_text"] = _format_ui_display(
            parsed["model"], parsed["vae"], loras_str,
            parsed["positive_prompt"], parsed["negative_prompt"], parsed["raw"],
        )

        return web.json_response({"status": "ok", "meta": parsed})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return web.json_response({"status": "error", "message": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
#  The Node
# ─────────────────────────────────────────────────────────────────────────────

class ScorpiovImageMeta:
    """
    Reads PNG metadata from an image file and outputs the extracted fields
    as strings that can be wired to other nodes.

    Connect the "filename" output of a Load Image node to the "image_path"
    input here, or type a filename manually.

    Outputs:
      model           – checkpoint / model name
      vae             – VAE name
      loras           – formatted list of LoRA names and weights
      positive_prompt – full positive prompt text
      negative_prompt – full negative prompt text
      raw_metadata    – complete raw metadata dump
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_path": ("STRING", {
                    "multiline": False,
                    "default":   "",
                    "tooltip":   "Filename from a Load Image node, or a full path to a PNG file.",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES  = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES  = ("model", "vae", "loras", "positive_prompt", "negative_prompt", "raw_metadata")
    FUNCTION      = "process"
    CATEGORY      = "Scorpiov/Image"
    OUTPUT_NODE   = True

    def process(self, image_path: str, unique_id="0"):

        image_path = image_path.strip()

        if not image_path:
            empty = ("", "", "", "", "", "No image path provided.")
            return {"ui": {"meta_display": [_format_ui_display(*empty)]}, "result": empty}

        full_path = _resolve_path(image_path)
        if full_path is None:
            msg = f"File not found: {image_path!r}"
            print(f"[Scorpiov Meta] {msg}")
            empty = ("", "", "", "", "", msg)
            return {"ui": {"meta_display": [_format_ui_display(*empty)]}, "result": empty}

        try:
            parsed = extract_metadata(full_path)
        except Exception as e:
            # A reader node should report, not crash the queue.
            msg = f"Could not read metadata from {image_path!r}: {e}"
            print(f"[Scorpiov Meta] {msg}")
            empty = ("", "", "", "", "", msg)
            return {"ui": {"meta_display": [_format_ui_display(*empty)]}, "result": empty}

        lora_lines = []
        for l in parsed["loras"]:
            w = f"{l['weight']:.2f}" if l["weight"] is not None else "?"
            lora_lines.append(f"{l['name']} (weight: {w})")
        loras_str = "\n".join(lora_lines) if lora_lines else "(none)"

        model    = parsed["model"]
        vae      = parsed["vae"]
        positive = parsed["positive_prompt"]
        negative = parsed["negative_prompt"]
        raw      = parsed["raw"]

        print(f"[Scorpiov Meta] Format   : {parsed['format']}")
        print(f"[Scorpiov Meta] Model    : {model}")
        print(f"[Scorpiov Meta] VAE      : {vae}")
        print(f"[Scorpiov Meta] LoRAs    : {loras_str}")
        print(f"[Scorpiov Meta] Positive : {positive[:80]}{'...' if len(positive) > 80 else ''}")
        print(f"[Scorpiov Meta] Negative : {negative[:80]}{'...' if len(negative) > 80 else ''}")

        display = _format_ui_display(model, vae, loras_str, positive, negative, raw)

        return {
            "ui":     {"meta_display": [display]},
            "result": (model, vae, loras_str, positive, negative, raw),
        }

    @classmethod
    def IS_CHANGED(cls, image_path, **kwargs):
        # Re-run when the FILE changes, not only when the path string does:
        # re-saving/exporting over the same filename is the normal way an
        # image gets new metadata, and returning the path alone made
        # ComfyUI serve the stale cached result. mtime+size is the cheap
        # fingerprint; an unresolvable path returns NaN (always re-run).
        if isinstance(image_path, str):
            full_path = _resolve_path(image_path.strip())
            if full_path:
                try:
                    st = os.stat(full_path)
                    return f"{st.st_mtime_ns}:{st.st_size}"
                except OSError:
                    pass
        return float("nan")


def _format_ui_display(model, vae, loras, positive, negative, raw):
    """
    Build a single formatted text block for the in-node textarea.
    Includes every piece of metadata — model, VAE, LoRAs, prompts, and
    the full raw dump so nothing (ADetailer, Hires, sampler settings…) is hidden.
    """
    sep = "─" * 60
    lines = []

    lines.append("📦 MODEL")
    lines.append(model or "(not found)")
    lines.append(sep)

    lines.append("🎨 VAE")
    lines.append(vae or "(not found)")
    lines.append(sep)

    lines.append("🔗 LoRAs")
    lines.append(loras if loras and loras != "(none)" else "(none)")
    lines.append(sep)

    lines.append("✅ POSITIVE PROMPT")
    lines.append(positive or "(empty)")
    lines.append(sep)

    lines.append("❌ NEGATIVE PROMPT")
    lines.append(negative or "(empty)")

    if raw:
        lines.append(sep)
        lines.append("📄 FULL RAW METADATA")
        lines.append(raw)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovImageMeta": ScorpiovImageMeta,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovImageMeta": "Scorpiov Image Meta Reader 🔍",
}
