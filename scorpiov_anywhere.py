"""
Scorpiov Anywhere
------------------
A broadcast/connector node inspired by cg-use-everywhere's "Anything Everywhere"
node, rebuilt for reliability on newer (Vue-based) ComfyUI frontends.

IMPORTANT ARCHITECTURE NOTE (read this before touching the Python side):
This class intentionally does almost nothing. It exists only so ComfyUI's
backend recognizes "ScorpiovAnywhere" as a valid node type and gives it
dynamic ANY-typed input sockets to draw on the canvas.

The actual work -- deciding which other nodes should receive each connected
input -- happens entirely in js/scorpiov_anywhere.js, at the moment the graph
is serialized into a prompt (right before it's sent to the backend for
execution). The JS layer rewrites that serialized JSON to add direct links
from the *real* upstream source node straight to each matched target, and
deletes this node from the JSON entirely. By the time the Python backend
ever sees the prompt, ScorpiovAnywhere nodes are already gone.

That means: if the JS rewrite works correctly, `broadcast()` below should
never actually run in a real generation. It's kept only as a safety net --
if something ever reaches the backend without being rewritten (e.g. the API
was called directly, bypassing the JS layer), this no-ops cleanly instead of
crashing the whole prompt.
"""

# ComfyUI's convention for an "accepts/returns literally anything" type.
ANY = "*"


class ScorpiovAnywhere:
    NAME = "Scorpiov Anywhere"
    DESCRIPTION = (
        "Broadcasts each connected input to any other empty, type-matching input "
        "elsewhere in the graph. No wires needed at the receiving end. "
        "This node is removed from the workflow automatically before execution -- "
        "the JS extension rewires the graph directly."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            # A single starter slot. The JS side grows additional
            # anything_2, anything_3, ... slots dynamically as each one
            # gets connected -- see js/scorpiov_anywhere.js.
            "optional": {
                "anything_1": (ANY, {}),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "broadcast"
    OUTPUT_NODE = True
    CATEGORY = "Scorpiov/Routing"

    def broadcast(self, **kwargs):
        # Safety-net no-op. See module docstring.
        return {}


NODE_CLASS_MAPPINGS = {
    "ScorpiovAnywhere": ScorpiovAnywhere,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovAnywhere": "Scorpiov Anywhere",
}
