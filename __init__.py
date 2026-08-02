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
  - Scorpiov Anywhere            (scorpiov_anywhere.py)
"""

from .scorpiov_wildcard import (
    NODE_CLASS_MAPPINGS as WILDCARD_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as WILDCARD_NAMES,
)
from .scorpiov_width_height import (
    NODE_CLASS_MAPPINGS as WH_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as WH_NAMES,
)
from .scorpiov_image_meta import (
    NODE_CLASS_MAPPINGS as META_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as META_NAMES,
)
from .scorpiov_image_loader import (
    NODE_CLASS_MAPPINGS as LOADER_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as LOADER_NAMES,
)
from .scorpiov_save_image import (
    NODE_CLASS_MAPPINGS as SAVE_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as SAVE_NAMES,
)
from .scorpiov_lora_tag_loader import (
    NODE_CLASS_MAPPINGS as LORATAG_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as LORATAG_NAMES,
)
from .scorpiov_anywhere import (
    NODE_CLASS_MAPPINGS as ANYWHERE_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as ANYWHERE_NAMES,
)

NODE_CLASS_MAPPINGS = {**WILDCARD_CLASSES, **WH_CLASSES, **META_CLASSES, **LOADER_CLASSES, **SAVE_CLASSES, **LORATAG_CLASSES, **ANYWHERE_CLASSES}
NODE_DISPLAY_NAME_MAPPINGS = {**WILDCARD_NAMES, **WH_NAMES, **META_NAMES, **LOADER_NAMES, **SAVE_NAMES, **LORATAG_NAMES, **ANYWHERE_NAMES}

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
