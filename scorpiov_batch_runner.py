class ScorpiovBatchPromptRunner:
    """
    Scorpiov Batch Prompt Runner

    A control-panel node, not part of the actual generation graph. It is
    never wired to anything and has no meaningful outputs. All real work
    happens client-side (see scorpiov_batch_runner.js): it locates the
    Wildcard Prompter, Multi-Checkpoint Loader, and Save Image nodes
    already in your graph by type, then submits one queue job per
    (checkpoint x prompt line) combination -- driving checkpoint_index,
    the Wildcard Prompter's serial line position, and Save Image's
    filename_prefix as "{line:03d}-{tag}".

    This Python class exists only so ComfyUI recognizes the node type and
    gives it a place on the canvas -- same pattern as Scorpiov Anywhere
    (dev reference §4.11). It is never actually executed as part of a
    normal queued prompt, since nothing is ever wired to it.
    """

    CATEGORY = "Scorpiov/Batch"
    RETURN_TYPES = ()
    FUNCTION = "noop"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def noop(self):
        return {}


NODE_CLASS_MAPPINGS = {
    "ScorpiovBatchPromptRunner": ScorpiovBatchPromptRunner,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovBatchPromptRunner": "Scorpiov Batch Prompt Runner \U0001f680",
}
