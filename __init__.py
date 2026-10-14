"""
Scorpiov-Nodes — ComfyUI Custom Node Package
Place this folder in: ComfyUI/custom_nodes/scorpiov-nodes/

Nodes included:
  - Scorpiov Wildcard Processor  (scorpiov_wildcard.py)
  - Scorpiov Wildcard Prompter   (scorpiov_wildcard.py)
  - Scorpiov Width Height        (scorpiov_width_height.py)
  - Scorpiov Image Meta Reader   (scorpiov_image_meta.py)
  - Scorpiov Image Loader        (scorpiov_image_loader.py)
  - Scorpiov Save Image          (scorpiov_save_image.py)
  - Scorpiov Lora Tag Loader     (scorpiov_lora_tag_loader.py)
  - Scorpiov Anywhere            (scorpiov_anywhere.py)
  - Scorpiov Prompt Combiner/Router (scorpiov_prompt_combiner.py)
  - Scorpiov Multi-Checkpoint Loader (scorpiov_multi_checkpoint_loader.py)
  - Scorpiov Batch Prompt Runner (scorpiov_batch_runner.py)

Each module is imported in isolation: if one file breaks against a
future ComfyUI API, the rest of the pack still loads and the console
says exactly which module failed and why. (The old top-level import
chain took all ten node types down when any single one broke.)
"""

import importlib

_MODULES = [
    "scorpiov_wildcard",
    "scorpiov_width_height",
    "scorpiov_image_meta",
    "scorpiov_image_loader",
    "scorpiov_save_image",
    "scorpiov_lora_tag_loader",
    "scorpiov_anywhere",
    "scorpiov_prompt_combiner",
    "scorpiov_multi_checkpoint_loader",
    "scorpiov_batch_runner",
]

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

_failed = []
for _name in _MODULES:
    try:
        _mod = importlib.import_module(f".{_name}", __package__)
        NODE_CLASS_MAPPINGS.update(getattr(_mod, "NODE_CLASS_MAPPINGS", {}))
        NODE_DISPLAY_NAME_MAPPINGS.update(
            getattr(_mod, "NODE_DISPLAY_NAME_MAPPINGS", {}))
    except Exception as _e:  # one broken module must not kill the pack
        _failed.append(_name)
        import traceback
        print(f"[Scorpiov] FAILED to load {_name}: {_e}")
        traceback.print_exc()

if _failed:
    print(f"[Scorpiov] {len(_failed)} module(s) failed to load: "
          f"{', '.join(_failed)} — the remaining nodes are available.")
else:
    print(f"[Scorpiov] All {len(_MODULES)} modules loaded "
          f"({len(NODE_CLASS_MAPPINGS)} node types).")

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
