"""
Scorpiov Prompt Combiner/Router
--------------------------------
Merges multiple STRING and/or CONDITIONING inputs into a single merged
STRING output and a single merged CONDITIONING output, with per-input
weight and enabled/disabled control.

ARCHITECTURE NOTE (read this before touching either file):
Unlike Scorpiov Anywhere, this node does real work at execution time
(string joining, conditioning weighted-averaging), so it's a normal
live-execution node -- not a JS-only graph rewrite. The dynamic
anything_N input growth (js/scorpiov_prompt_combiner.js) reuses the same
pattern as Scorpiov Anywhere, but everything below actually runs in
Python during a queued prompt.

WHY THE HIDDEN "scorpiov_config" WIDGET EXISTS:
Per-input weight and enabled/disabled are set via right-click on each
input slot, in JS. But Python nodes only ever receive two kinds of data:
widget values, and whatever comes down a wired connection. A live
node.properties value set in the browser is NOT automatically sent to
Python -- only widgets are. So the JS extension mirrors every slot's
{weight, enabled} into a hidden STRING widget called "scorpiov_config"
(a small JSON blob) every time you change it via right-click. That
hidden widget -- not node.properties -- is what this class actually
reads below. It is not meant to be edited by hand.

KNOWN EDGE CASE TO TEST: if you combine CONDITIONING from prompts that
were encoded with very different token lengths, the weighted-average
step below may need padding logic (ComfyUI's own ConditioningAverage
node has to handle this too). Flagging this now rather than solving it
speculatively -- report the actual error if/when it comes up and we'll
fix it against the real shapes involved.
"""

import json

import torch

# ComfyUI's convention for an "accepts/returns literally anything" type.
ANY = "*"


class ScorpiovPromptCombiner:
    NAME = "Scorpiov Prompt Combiner/Router"
    DESCRIPTION = (
        "Merges multiple STRING and/or CONDITIONING inputs into one merged "
        "STRING output and one merged CONDITIONING output. Per-input weight, "
        "order, and enabled/disabled state are set via right-click on each "
        "input slot."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "separator": ("STRING", {"default": ", "}),
            },
            "optional": {
                # A single starter slot. The JS side grows additional
                # anything_2, anything_3, ... slots dynamically as each one
                # gets connected -- see js/scorpiov_prompt_combiner.js.
                "anything_1": (ANY, {}),
                # Hidden JSON blob written by the JS extension, e.g.:
                #   {"anything_2": {"weight": 1.0, "enabled": true}, ...}
                # Slots not present here just default to weight 1.0, enabled.
                #
                # Deliberately NOT multiline: on this frontend, multiline
                # STRING widgets are rendered as a "customtext" widget type
                # with a real DOM <textarea> overlay and a hard-coded
                # minNodeSize (confirmed live: [400, 200]) that our JS-side
                # hide/shrink tricks can't override -- that's what was
                # forcing the node to occupy a large invisible footprint.
                # A plain (non-multiline) STRING widget is canvas-drawn, so
                # the JS extension's draw/computeSize overrides actually
                # take effect.
                "scorpiov_config": ("STRING", {"default": "{}"}),
            },
        }

    RETURN_TYPES = ("STRING", "CONDITIONING")
    RETURN_NAMES = ("merged_string", "merged_conditioning")
    FUNCTION = "combine"
    CATEGORY = "Scorpiov/Routing"

    def combine(self, separator=", ", scorpiov_config="{}", **kwargs):
        try:
            config = json.loads(scorpiov_config) if scorpiov_config else {}
        except (json.JSONDecodeError, TypeError):
            config = {}

        # Preserve creation order (anything_1, anything_2, ...) rather than
        # whatever order Python's **kwargs happens to hand them back in.
        def slot_index(name):
            try:
                return int(name.rsplit("_", 1)[-1])
            except (ValueError, IndexError):
                return 0

        ordered_names = sorted(
            (k for k in kwargs if k.startswith("anything_")),
            key=slot_index,
        )

        string_parts = []
        conditioning_items = []  # list of (tensor, extras_dict, weight)

        for name in ordered_names:
            value = kwargs[name]
            if value is None:
                continue

            slot_cfg = config.get(name, {})
            if not slot_cfg.get("enabled", True):
                continue
            weight = float(slot_cfg.get("weight", 1.0))

            if isinstance(value, str):
                if value != "":
                    # Weight has no numeric meaning for raw text the way it
                    # does for CONDITIONING tensors, so we fall back on
                    # ComfyUI's own prompt-emphasis syntax -- (text:1.30) --
                    # which CLIPTextEncode already knows how to interpret.
                    # weight == 1.0 (the default / unconfigured case) is left
                    # unwrapped so plain fragments stay exactly as typed.
                    if abs(weight - 1.0) > 1e-6:
                        string_parts.append(f"({value}:{weight:.2f})")
                    else:
                        string_parts.append(value)
            elif isinstance(value, list) and value and isinstance(value[0], (list, tuple)):
                # ComfyUI's CONDITIONING type is a list of [tensor, extras_dict] pairs.
                for cond_tensor, cond_extra in value:
                    conditioning_items.append((cond_tensor, cond_extra, weight))
            # Anything else (an unexpected/unsupported type on a slot) is
            # silently skipped -- this node only knows how to merge STRING
            # and CONDITIONING.

        merged_string = separator.join(string_parts)
        merged_conditioning = self._merge_conditioning(conditioning_items)

        return (merged_string, merged_conditioning)

    @staticmethod
    def _merge_conditioning(items):
        if not items:
            return []
        if len(items) == 1:
            tensor, extra, _weight = items[0]
            return [[tensor, dict(extra)]]

        total_weight = sum(w for _, _, w in items) or 1.0
        base_tensor, base_extra, base_weight = items[0]
        # Match ComfyUI's own ConditioningAverage: align every tensor to a
        # single target token length (truncate longer ones, zero-pad
        # shorter ones) before blending, rather than assuming all inputs
        # were encoded to the same length.
        target_len = base_tensor.shape[1]

        def align_len(t):
            if t.shape[1] == target_len:
                return t
            if t.shape[1] > target_len:
                return t[:, :target_len]
            pad = torch.zeros(
                (t.shape[0], target_len - t.shape[1], t.shape[2]),
                dtype=t.dtype,
                device=t.device,
            )
            return torch.cat([t, pad], dim=1)

        merged = align_len(base_tensor) * (base_weight / total_weight)

        # pooled_output (used heavily by SDXL-style models for overall
        # composition/style) needs blending too -- previously this was
        # silently dropped in favor of just the first input's value, which
        # is the most likely cause of degraded/poor KSampler output when
        # merged_conditioning was wired straight into `positive`.
        merged_pooled = None
        base_pooled = base_extra.get("pooled_output")
        if base_pooled is not None:
            merged_pooled = base_pooled * (base_weight / total_weight)

        for tensor, extra, weight in items[1:]:
            merged = merged + align_len(tensor) * (weight / total_weight)

            pooled = extra.get("pooled_output")
            if pooled is not None:
                contribution = pooled * (weight / total_weight)
                merged_pooled = contribution if merged_pooled is None else merged_pooled + contribution

        merged_extra = dict(base_extra)
        if merged_pooled is not None:
            merged_extra["pooled_output"] = merged_pooled
        elif "pooled_output" in merged_extra:
            # Base had one but nothing actually contributed to a blended
            # version (shouldn't normally happen) -- don't ship a stale,
            # unblended pooled_output alongside blended token embeddings.
            del merged_extra["pooled_output"]

        return [[merged, merged_extra]]


NODE_CLASS_MAPPINGS = {
    "ScorpiovPromptCombiner": ScorpiovPromptCombiner,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovPromptCombiner": "Scorpiov Prompt Combiner/Router",
}
