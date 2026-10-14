"""
Scorpiov Image Loader
---------------------
A custom image loader node for ComfyUI that outputs three things:

  image      (IMAGE)   – the standard ComfyUI image tensor, ready to connect
                         to any node that accepts IMAGE (VAE Encode, Preview, etc.)
  filename   (STRING)  – bare filename, e.g. "my_photo.png"
  file_path  (STRING)  – full absolute path on disk, e.g.
                         "/home/user/ComfyUI/input/my_photo.png"

The file-picker widget behaves identically to ComfyUI's built-in Load Image
node — you get the thumbnail preview, the upload button, and the folder browser.

Supports: PNG, JPG/JPEG, WEBP, BMP, GIF (first frame), TIFF.
Modes with real alpha (RGBA, LA, palette images carrying a 'transparency'
chunk) are flattened onto white; every other mode converts to RGB.
If you need the mask output, use ComfyUI's built-in Load Image node instead.

REFACTOR NOTE (Oct 14, 2026): the widget list is scanned recursively so
subfolders show up as "sub/image.png"; images load through a context
manager (no leaked file handles on Windows); IS_CHANGED hashes the file
in 8 MB chunks instead of reading it whole into RAM.
"""

import os
import hashlib

import numpy as np
import torch
import folder_paths
from PIL import Image, ImageOps


# ─────────────────────────────────────────────────────────────────────────────
#  Helper: resolve a filename → absolute path
#  ComfyUI stores uploaded files in its input/ folder.
#  The widget value is just the bare filename (or a subfolder/filename).
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_input_path(filename: str) -> str:
    """
    Given a bare filename (as returned by the image-upload widget),
    return the full absolute path inside ComfyUI's input directory.
    Raises FileNotFoundError if not found.
    """
    # folder_paths.get_annotated_filepath handles subfolders like
    # "subfolder/image.png" and the [input]/[output]/[temp] prefix syntax
    # that ComfyUI uses internally.
    if hasattr(folder_paths, "get_annotated_filepath"):
        candidate = folder_paths.get_annotated_filepath(filename)
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)

    # Fallback: search the input directory directly
    input_dir = folder_paths.get_input_directory()
    candidate = os.path.join(input_dir, filename)
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)

    # Last resort: absolute or CWD-relative path provided directly
    if os.path.isfile(filename):
        return os.path.abspath(filename)

    raise FileNotFoundError(
        f"[Scorpiov Image Loader] Cannot find image file: {filename!r}\n"
        f"  Searched input directory: {input_dir}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Helper: PIL Image → ComfyUI IMAGE tensor
#
#  ComfyUI IMAGE tensors are float32, shape [batch, H, W, C], values 0‥1.
#  Alpha channels are flattened onto white before conversion.
# ─────────────────────────────────────────────────────────────────────────────

def _pil_to_tensor(pil_img: Image.Image) -> torch.Tensor:
    """Convert a PIL Image to a ComfyUI-compatible IMAGE tensor."""

    # Anything with real alpha (RGBA, LA, palette-plus-transparency chunk)
    # is flattened onto a white background first so a semi-transparent PNG
    # looks in the tensor the way it does in an image viewer; everything
    # else converts straight to RGB. (Converting to RGBA first keeps the
    # flattening path identical for all alpha-bearing modes.)
    if (pil_img.mode in ("RGBA", "LA", "PA")
            or (pil_img.mode == "P" and "transparency" in pil_img.info)):
        rgba = pil_img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[3])   # alpha channel as mask
        pil_img = background
    elif pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")

    # Apply EXIF orientation so the tensor matches what the user sees
    pil_img = ImageOps.exif_transpose(pil_img)

    arr = np.array(pil_img, dtype=np.float32) / 255.0   # H × W × C, 0‥1
    tensor = torch.from_numpy(arr).unsqueeze(0)          # 1 × H × W × C
    return tensor


# ─────────────────────────────────────────────────────────────────────────────
#  The Node
# ─────────────────────────────────────────────────────────────────────────────

class ScorpiovImageLoader:
    """
    Scorpiov Image Loader.
    Loads an image and outputs the tensor, the bare filename, and the full
    file path — all three wirable to other nodes.
    """

    @classmethod
    def INPUT_TYPES(cls):
        # Scan the input directory ourselves — "input" is a special path,
        # not a named collection, so get_filename_list("input") raises KeyError.
        input_dir = folder_paths.get_input_directory()
        image_extensions = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif"}
        files = []
        if os.path.isdir(input_dir):
            # Recursive scan so subfolders are pickable too. Widget values
            # use forward-slash relative paths ("sub/image.png"), which
            # _resolve_input_path handles via get_annotated_filepath.
            for root, dirs, filenames in os.walk(input_dir):
                dirs.sort()
                for f in sorted(filenames):
                    if os.path.splitext(f)[1].lower() in image_extensions:
                        rel = os.path.relpath(os.path.join(root, f), input_dir)
                        files.append(rel.replace(os.sep, "/"))
        files = sorted(files) or [""]   # widget needs at least one entry

        return {
            "required": {
                # image_upload: True activates the thumbnail preview, upload
                # button, and folder browser in the ComfyUI frontend.
                "image": (files, {"image_upload": True}),
            }
        }

    # ── Outputs ──────────────────────────────────────────────────────────────
    RETURN_TYPES  = ("IMAGE",  "STRING",   "STRING")
    RETURN_NAMES  = ("image",  "filename", "file_path")
    FUNCTION      = "load_image"
    CATEGORY      = "Scorpiov/Image"

    def load_image(self, image: str):
        """
        image: the value from the file-picker widget — a bare filename
               (e.g. "photo.png") relative to ComfyUI's input/ folder.
        """

        # ── Resolve to a full path ────────────────────────────────────────
        try:
            full_path = _resolve_input_path(image)
        except FileNotFoundError as e:
            print(str(e))
            # Return a 1×1 black image so downstream nodes don't crash,
            # along with empty strings for the path outputs.
            dummy = torch.zeros(1, 1, 1, 3, dtype=torch.float32)
            return (dummy, image, "")

        # ── Load and convert ──────────────────────────────────────────────
        # Context manager: Pillow closes the file handle on exit, so on
        # Windows the file can be moved/renamed/deleted while ComfyUI is
        # still running. (The tensor is built from a numpy copy, so it
        # stays valid after the handle closes.)
        with Image.open(full_path) as pil_img:
            tensor = _pil_to_tensor(pil_img)

        # ── Build output strings ──────────────────────────────────────────
        filename  = os.path.basename(full_path)      # e.g. "photo.png"
        file_path = full_path                         # e.g. "/…/input/photo.png"

        print(f"[Scorpiov Image Loader] Loaded: {full_path}")
        print(f"  filename  : {filename}")
        print(f"  file_path : {file_path}")
        print(f"  tensor    : {list(tensor.shape)}  dtype={tensor.dtype}")

        return (tensor, filename, file_path)

    @classmethod
    def IS_CHANGED(cls, image: str):
        """
        Tell ComfyUI to re-run this node when the file contents change,
        not just when the widget value (filename) changes.
        We hash the file so edits to the same filename are detected.
        """
        try:
            full_path = _resolve_input_path(image)
            # Chunked hashing: a multi-GB TIFF should never be read whole
            # into RAM just to check whether the file changed.
            digest = hashlib.md5()
            with open(full_path, "rb") as f:
                for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except Exception:
            return float("nan")   # always re-run if we can't hash

    @classmethod
    def VALIDATE_INPUTS(cls, image: str):
        """
        ComfyUI calls this before running — lets us show a clean error
        in the UI instead of a red stack-trace if the file is missing.
        """
        if not folder_paths.exists_annotated_filepath(image):
            return f"Image file not found: {image!r}"
        return True


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovImageLoader": ScorpiovImageLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovImageLoader": "Scorpiov Image Loader 🖼️",
}
