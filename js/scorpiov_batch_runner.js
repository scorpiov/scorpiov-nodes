import { app } from "../../scripts/app.js";

// ---------------------------------------------------------------------
// Scorpiov Batch Prompt Runner — frontend
//
// This node has no real inputs/outputs; it's a control panel that finds
// three other nodes in your graph by type and drives them:
//   - ScorpiovWildcardPrompter      (which prompt line is used)
//   - ScorpiovMultiCheckpointLoader (which checkpoint is loaded)
//   - ScorpiovSaveImage             (the resulting filename_prefix, which
//                                     also controls the output subfolder)
//
// IMPORTANT — why this resets the wildcard's serial state only ONCE,
// not before every submission:
//
// The wildcard engine's serial mode (scorpiov_wildcard.py) only reads
// `serial_start_line` the first time it's asked for a given node+file
// combination; every call after that just auto-increments and ignores
// `serial_start_line` entirely. Since this Runner submits every job in
// the batch upfront (not waiting for each to actually finish executing),
// resetting before every submission would race ahead of the real GPU
// executions and silently erase earlier jobs' pinned lines.
//
// The fix: reset exactly once, at the very start of the whole batch,
// with serial_start_line = 1. Because each checkpoint's inner loop
// submits exactly `lineCount` jobs, the engine's own auto-increment
// wraps back to line 1 at every checkpoint boundary for free -- no
// further resets or network calls needed mid-batch. This was verified
// live against a real 43-line wildcard file before being relied on here.
// ---------------------------------------------------------------------

const NODE_NAME = "ScorpiovBatchPromptRunner";

function findSingle(type) {
  const matches = app.graph.nodes.filter((n) => n.type === type);
  if (matches.length !== 1) {
    return { error: `Expected exactly 1 "${type}" node in the graph, found ${matches.length}.` };
  }
  return { node: matches[0] };
}

function extractWildcardToken(text) {
  const matches = [...text.matchAll(/__([a-zA-Z0-9_\-]+)__/g)];
  if (matches.length === 0) return null;
  if (matches.length > 1) {
    console.warn(
      `Scorpiov Batch Prompt Runner: multiple __token__ references found in the ` +
        `Wildcard Prompter's text (${matches.map((m) => m[1]).join(", ")}); using the first one ("${matches[0][1]}").`
    );
  }
  return matches[0][1];
}

async function getLineCount(name) {
  const res = await fetch(`/scorpiov/wildcard/line_count?name=${encodeURIComponent(name)}`);
  return res.json();
}

async function resetWildcardState(nodeId) {
  await fetch("/scorpiov/wildcard/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_id: String(nodeId) }),
  });
}

async function runBatch(setStatus) {
  const wpResult = findSingle("ScorpiovWildcardPrompter");
  if (wpResult.error) return setStatus("\u274c " + wpResult.error);
  const wp = wpResult.node;

  const mclResult = findSingle("ScorpiovMultiCheckpointLoader");
  if (mclResult.error) return setStatus("\u274c " + mclResult.error);
  const mcl = mclResult.node;

  const saveResult = findSingle("ScorpiovSaveImage");
  if (saveResult.error) return setStatus("\u274c " + saveResult.error);
  const save = saveResult.node;

  // filename_prefix must be a free widget, not wired -- this Runner needs
  // to set the exact combined "{line}-{tag}" string per submission, which
  // a live upstream wire (e.g. from model_tag) would silently override.
  const filenameInput = save.inputs?.find((i) => i.name === "filename_prefix");
  if (filenameInput && filenameInput.link != null) {
    return setStatus(
      "\u274c Disconnect the wire into Save Image's filename_prefix first \u2014 " +
        "the Runner needs to set it directly, combining line number + checkpoint tag."
    );
  }
  const filenameWidget = save.widgets?.find((w) => w.name === "filename_prefix");
  if (!filenameWidget) return setStatus("\u274c Could not find a filename_prefix widget on Save Image.");

  const modeWidget = wp.widgets.find((w) => w.name === "mode");
  if (modeWidget.value !== "serial") {
    return setStatus('\u274c Wildcard Prompter\'s mode must be "serial" for the Runner to control which line is used.');
  }

  const textWidget = wp.widgets.find((w) => w.name === "text");
  const token = extractWildcardToken(textWidget.value);
  if (!token) return setStatus("\u274c No __wildcard__ token found in the Wildcard Prompter's text field.");

  setStatus(`Looking up line count for __${token}__...`);
  const lineInfo = await getLineCount(token);
  if (!lineInfo.found || !lineInfo.line_count) {
    return setStatus(`\u274c Wildcard file "${token}" not found or empty. Try Refresh Wildcards on the Prompter first.`);
  }
  const lineCount = lineInfo.line_count;

  const cfgWidget = mcl.widgets.find((w) => w.name === "scorpiov_checkpoint_config");
  let slots = [];
  try {
    slots = JSON.parse(cfgWidget?.value || "[]");
    if (!Array.isArray(slots)) slots = [];
  } catch (e) {
    slots = [];
  }
  if (!slots.length) return setStatus("\u274c No checkpoints configured on the Multi-Checkpoint Loader.");

  const checkpointIndexWidget = mcl.widgets.find((w) => w.name === "checkpoint_index");
  const startLineWidget = wp.widgets.find((w) => w.name === "serial_start_line");

  // Single reset for the whole batch -- see the file header comment for why.
  setStatus("Resetting wildcard position...");
  await resetWildcardState(wp.id);
  startLineWidget.value = 1;

  const total = slots.length * lineCount;
  let submitted = 0;
  setStatus(`Submitting 0 / ${total}...`);

  for (let s = 0; s < slots.length; s++) {
    const slot = slots[s];
    for (let i = 0; i < lineCount; i++) {
      const line = i + 1;
      checkpointIndexWidget.value = s;
      filenameWidget.value = `%date:yyyy-MM-dd%/${slot.tag}/${String(line).padStart(3, "0")}-${slot.tag}`;
      mcl.setDirtyCanvas(true, true);
      save.setDirtyCanvas(true, true);

      try {
        await app.queuePrompt(0, 1);
      } catch (err) {
        return setStatus(`\u274c Submission failed at checkpoint "${slot.tag}", line ${line}: ${err}`);
      }
      submitted++;
      setStatus(`Submitting ${submitted} / ${total}...`);
    }
  }

  setStatus(
    `\u2705 Queued ${total} jobs (${slots.length} checkpoints \u00d7 ${lineCount} lines). ` +
      `Watch the ComfyUI queue to track progress \u2014 nothing else should submit to this graph until it drains.`
  );
}

function setupBatchRunnerUI(node) {
  const container = document.createElement("div");
  container.style.cssText = "display:flex;flex-direction:column;gap:4px;padding:4px 2px;width:100%;box-sizing:border-box;";

  const runBtn = document.createElement("button");
  runBtn.textContent = "\ud83d\ude80 Run Batch";
  runBtn.style.cssText = "padding:6px;";

  const status = document.createElement("div");
  status.style.cssText = "font-size:11px;color:#aaa;font-family:sans-serif;white-space:pre-wrap;";
  status.textContent =
    "Locates your Wildcard Prompter, Multi-Checkpoint Loader, and Save Image nodes by type, " +
    "and queues one job per (checkpoint \u00d7 prompt line).";

  runBtn.onclick = async () => {
    runBtn.disabled = true;
    try {
      await runBatch((msg) => {
        status.textContent = msg;
        node.setDirtyCanvas(true, true);
      });
    } finally {
      runBtn.disabled = false;
    }
  };

  container.appendChild(runBtn);
  container.appendChild(status);

  const domWidget = node.addDOMWidget("scorpiov_batch_ui", "scorpiov_batch_ui", container, { serialize: false });
  domWidget.serialize = false;
}

app.registerExtension({
  name: "Scorpiov.BatchPromptRunner",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_NAME) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      if (this._scorpiovBatchBuilt) return;
      this._scorpiovBatchBuilt = true;
      setupBatchRunnerUI(this);
    };
  },
});
