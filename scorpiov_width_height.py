"""
Scorpiov Width Height Node
--------------------------
Sets width and height for image generation.

Features:
  - Ratio dropdown loaded from ratios.txt (add new ratios without code changes)
  - Custom width/height text fields (only used when "Custom" is selected)
  - Upscale factor (e.g. 2.0 doubles both dimensions)
  - Latent channel depth selector: 4 for SD/SDXL, 16 for Flux/SD3/Qwen
  - Outputs: width (INT), height (INT), empty_latent (LATENT)

REFACTOR NOTE (Oct 14, 2026): _load_ratios is fully defensive (a bad
ratios.txt degrades to the Custom entry with a warning, never a crash);
upscaled dimensions are clamped to 8192 with a warning (8192 x 8.0 would
build an OOM-sized latent); the new latent_channels input is appended
LAST in the required dict so saved workflows keep their input order;
IS_CHANGED returns the plain values tuple (hash() of strings is salted
per process, so the old hash((...)) form was unreliable).
"""

import os
import torch
from server import PromptServer
from aiohttp import web

# ── Path to the ratios config file ──────────────────────────────────────────
RATIOS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratios.txt")

# ── In-memory cache so we only re-read when needed ──────────────────────────
_ratio_cache: list[tuple[str, int, int]] = []   # [(label, w, h), ...]


def _load_ratios() -> list[tuple[str, int, int]]:
    """
    Read ratios.txt and return a list of (label, width, height) tuples.
    Lines starting with # are comments. Format: Label | width | height
    Always prepends ("Custom", 0, 0) as the first entry.

    Defensive by design: ratios.txt is a hand-edited config file, so any
    read/parse failure (odd encoding, garbage content) degrades to just
    the "Custom" entry with a printed warning — it can never take down
    node registration.
    """
    ratios = [("Custom", 0, 0)]
    if not os.path.isfile(RATIOS_FILE):
        print(f"[Scorpiov WH] ratios.txt not found at {RATIOS_FILE}, using defaults.")
        return ratios

    try:
        with open(RATIOS_FILE, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except Exception as e:
        print(f"[Scorpiov WH] WARNING: could not read ratios.txt ({e}); "
              f"falling back to Custom only.")
        return ratios

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            print(f"[Scorpiov WH] Skipping bad ratios line: {line!r}")
            continue
        try:
            label = parts[0]
            w = int(parts[1])
            h = int(parts[2])
            ratios.append((label, w, h))
        except ValueError:
            print(f"[Scorpiov WH] Could not parse width/height in: {line!r}")

    return ratios


def _get_ratios() -> list[tuple[str, int, int]]:
    """Return cached ratios, loading from file if cache is empty."""
    global _ratio_cache
    if not _ratio_cache:
        _ratio_cache = _load_ratios()
    return _ratio_cache


def _ratio_labels() -> list[str]:
    return [r[0] for r in _get_ratios()]


def _ratio_map() -> dict[str, tuple[int, int]]:
    return {r[0]: (r[1], r[2]) for r in _get_ratios()}


# ── REST endpoint: refresh ratio list without restarting ComfyUI ─────────────
@PromptServer.instance.routes.post("/scorpiov/wh/refresh")
async def scorpiov_wh_refresh(request):
    global _ratio_cache
    _ratio_cache = _load_ratios()
    labels = [r[0] for r in _ratio_cache]
    print(f"[Scorpiov WH] Ratios refreshed: {labels}")
    return web.json_response({"status": "ok", "ratios": labels})


# ── The Node ─────────────────────────────────────────────────────────────────

class ScorpiovWidthHeight:
    """
    Scorpiov Width Height Node.
    Select a ratio preset or enter custom width/height.
    Applies an upscale factor, then outputs width, height, and an empty latent.
    """

    @classmethod
    def INPUT_TYPES(cls):
        labels = _ratio_labels()
        return {
            "required": {
                "ratio": (labels, {"default": labels[0]}),
                "width":  ("INT", {
                    "default": 1024, "min": 64, "max": 8192, "step": 8,
                    "tooltip": "Used only when ratio is set to Custom"
                }),
                "height": ("INT", {
                    "default": 1024, "min": 64, "max": 8192, "step": 8,
                    "tooltip": "Used only when ratio is set to Custom"
                }),
                "upscale_factor": ("FLOAT", {
                    "default": 1.0, "min": 0.1, "max": 8.0, "step": 0.1,
                    "tooltip": "Multiplies both width and height. e.g. 2.0 = double resolution"
                }),
                "batch_size": ("INT", {
                    "default": 1, "min": 1, "max": 64,
                    "tooltip": "Number of images in the empty latent batch"
                }),
                # Appended LAST on purpose: ComfyUI stores required inputs
                # positionally in saved workflows, so new inputs only ever
                # go at the end — old workflows keep wiring up correctly.
                "latent_channels": (["4 (SD / SDXL)", "16 (Flux / SD3 / Qwen)"], {
                    "default": "4 (SD / SDXL)",
                    "tooltip": "Latent channel depth — 4 for SD/SDXL, 16 for Flux/SD3/Qwen"
                }),
            }
        }

    RETURN_TYPES  = ("INT", "INT", "LATENT")
    RETURN_NAMES  = ("width", "height", "empty_latent")
    FUNCTION      = "process"
    CATEGORY      = "Scorpiov/Image"

    def process(self, ratio: str, width: int, height: int,
                upscale_factor: float, batch_size: int,
                latent_channels: str = "4 (SD / SDXL)"):

        rmap = _ratio_map()

        if ratio == "Custom":
            out_w = width
            out_h = height
        elif ratio in rmap:
            out_w, out_h = rmap[ratio]
        else:
            print(f"[Scorpiov WH] Unknown ratio '{ratio}', falling back to custom.")
            out_w = width
            out_h = height

        # Apply upscale factor, round to nearest multiple of 8 (required by SD)
        out_w = max(64, round(out_w * upscale_factor / 8) * 8)
        out_h = max(64, round(out_h * upscale_factor / 8) * 8)

        # The widgets cap inputs at 8192, but an 8192 x 8.0 upscale would
        # build a 65536-wide latent — clamp with a warning instead of an
        # OOM crash.
        if out_w > 8192 or out_h > 8192:
            print(f"[Scorpiov WH] WARNING: {out_w} x {out_h} exceeds the 8192 "
                  f"max, clamping.")
            out_w = min(out_w, 8192)
            out_h = min(out_h, 8192)

        channels = 16 if latent_channels.startswith("16") else 4

        # 16-channel models (Qwen-Image 2.1, Flux, SD3) pack their
        # latents 2x2, so pixel dimensions must land on a 16-pixel grid:
        # the sampler silently floors anything else (verified live,
        # Oct 14 2026: a 1000x832 request on Qwen 2.1 — and ComfyUI's
        # own core EmptyLatentImage — both come out 992x832, while the
        # node would still be reporting 1000 to anything wired to its
        # width output). Snap down to the grid here, with a warning, so
        # the sizes this node reports are the sizes you actually get.
        if channels == 16:
            snapped_w = max(64, (out_w // 16) * 16)
            snapped_h = max(64, (out_h // 16) * 16)
            if (snapped_w, snapped_h) != (out_w, out_h):
                print(f"[Scorpiov WH] NOTE: 16-channel models need "
                      f"dimensions on a 16-pixel grid: {out_w} x {out_h} "
                      f"-> {snapped_w} x {snapped_h}.")
                out_w, out_h = snapped_w, snapped_h

        print(f"[Scorpiov WH] ratio={ratio!r}  →  {out_w} x {out_h}  "
              f"(upscale={upscale_factor}x, batch={batch_size}, "
              f"latent_channels={channels})")

        # Build empty latent tensor  (latent space is 1/8 of pixel dimensions)
        latent = torch.zeros([batch_size, channels, out_h // 8, out_w // 8])
        # "downscale_ratio_spacial" is the hint core's EmptyLatentImage
        # returns and newer model paths (Qwen-Image 2.1 especially) rely
        # on: without it the sampler/VAE assume the tensor is already in
        # the model's packed /16 geometry and the image comes out at
        # DOUBLE the requested size (verified live, Oct 14 2026: an
        # 832x832 request decoded to 1664x1664; with the hint it is 832).
        empty_latent = {"samples": latent, "downscale_ratio_spacial": 8}

        return (out_w, out_h, empty_latent)

    @classmethod
    def IS_CHANGED(cls, ratio, width, height, upscale_factor, batch_size,
                   latent_channels="4 (SD / SDXL)"):
        # Return the plain values tuple. Python's built-in hash() of strings
        # is salted per process (PYTHONHASHSEED), so the old hash((...))
        # form compared unreliably across sessions — a tuple is stable.
        return (ratio, width, height, upscale_factor, batch_size, latent_channels)


# ── Registration ─────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovWidthHeight": ScorpiovWidthHeight,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovWidthHeight": "Scorpiov Width Height 📐",
}
