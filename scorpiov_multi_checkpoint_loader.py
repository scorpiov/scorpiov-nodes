import json

import comfy.sd
import folder_paths

try:
    from .scorpiov_common import basename_noext
except ImportError:  # standalone use / unit tests outside the package
    from scorpiov_common import basename_noext


class ScorpiovMultiCheckpointLoader:
    """
    Scorpiov Multi-Checkpoint Loader

    Holds a list of checkpoints, configured client-side (JS builds the slot
    UI) and mirrored into the hidden `scorpiov_checkpoint_config` widget as
    a JSON array: [{"ckpt_name": "...", "tag": "..."}, ...].

    At execution time, loads whichever slot `checkpoint_index` points to.
    Intended to be driven by the Scorpiov Batch Prompt Runner, which sets
    `checkpoint_index` before each queue submission so a single graph can
    cycle through multiple checkpoints across separate runs.

    Outputs model_tag alongside MODEL/CLIP/VAE so Save Image (or anything
    else downstream) can use the short tag in a filename without having to
    re-derive it from the checkpoint filename itself.

    REFACTOR NOTE (Oct 14, 2026): a non-dict slot entry now raises the
    same clear ValueError style as the other guards (a hand-edited config
    used to die on AttributeError), and the default tag is the bare
    checkpoint stem via scorpiov_common.basename_noext — no subfolder
    path, no extension.
    """

    CATEGORY = "Scorpiov/Loaders"
    RETURN_TYPES = ("MODEL", "CLIP", "VAE", "STRING")
    RETURN_NAMES = ("MODEL", "CLIP", "VAE", "model_tag")
    FUNCTION = "load_checkpoint"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # Driven by the Batch Runner between queue submissions.
                # Left visible (not hidden) so it can also be set manually
                # while testing this node standalone.
                "checkpoint_index": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1}),
                # Hidden JS-mirror of the slot list. Never hand-edit this;
                # it's rebuilt from the on-node UI. See scorpiov_multi_checkpoint_loader.js
                "scorpiov_checkpoint_config": ("STRING", {"default": "[]", "multiline": False}),
            }
        }

    def load_checkpoint(self, checkpoint_index, scorpiov_checkpoint_config):
        try:
            slots = json.loads(scorpiov_checkpoint_config)
            if not isinstance(slots, list):
                slots = []
        except (json.JSONDecodeError, TypeError):
            slots = []

        if not slots:
            raise ValueError(
                "Scorpiov Multi-Checkpoint Loader: no checkpoints configured. "
                "Use 'Add Checkpoint' on the node to add at least one slot."
            )

        # Clamp rather than hard-fail on an out-of-range index, so a Batch
        # Runner that miscounts slots (e.g. off-by-one across a long batch)
        # degrades to the last valid slot instead of aborting mid-batch.
        index = max(0, min(checkpoint_index, len(slots) - 1))
        slot = slots[index]
        if not isinstance(slot, dict):
            # A hand-edited config (or a JS-side serialization bug) can put
            # a non-object in the slot list — fail with a clear message
            # pointing at the broken slot instead of an AttributeError.
            raise ValueError(
                "Scorpiov Multi-Checkpoint Loader: slot "
                f"{index} is malformed (expected an object with "
                f"'ckpt_name'/'tag', got {type(slot).__name__}). "
                "Re-add the checkpoint on the node to repair the config."
            )

        ckpt_name = slot.get("ckpt_name", "")
        # Default tag is the bare checkpoint stem ("model_v2") — never the
        # raw ckpt_name, which may carry a subfolder path and an extension.
        tag = slot.get("tag") or basename_noext(ckpt_name)

        if not ckpt_name:
            raise ValueError(
                f"Scorpiov Multi-Checkpoint Loader: slot {index} has no checkpoint selected."
            )

        ckpt_path = folder_paths.get_full_path("checkpoints", ckpt_name)
        if ckpt_path is None:
            raise ValueError(
                f"Scorpiov Multi-Checkpoint Loader: checkpoint '{ckpt_name}' "
                f"was not found in your checkpoints folder. It may have been "
                f"moved or renamed since this slot was configured."
            )

        loaded = comfy.sd.load_checkpoint_guess_config(
            ckpt_path,
            output_vae=True,
            output_clip=True,
            embedding_directory=folder_paths.get_folder_paths("embeddings"),
        )
        model, clip, vae = loaded[0], loaded[1], loaded[2]

        return (model, clip, vae, tag)


NODE_CLASS_MAPPINGS = {
    "ScorpiovMultiCheckpointLoader": ScorpiovMultiCheckpointLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovMultiCheckpointLoader": "Scorpiov Multi-Checkpoint Loader",
}
