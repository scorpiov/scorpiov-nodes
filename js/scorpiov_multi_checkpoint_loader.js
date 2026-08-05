import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ---------------------------------------------------------------------
// Scorpiov Multi-Checkpoint Loader — frontend
//
// Builds a small DOM widget on the node: one row per checkpoint slot
// (dropdown + editable tag field + remove button), plus an "Add
// Checkpoint" button. The row list is mirrored into the hidden
// `scorpiov_checkpoint_config` STRING widget as JSON on every change,
// which is what the Python side actually reads at execution time.
//
// See scorpiov_multi_checkpoint_loader.py for the execution-side logic.
// ---------------------------------------------------------------------

const NODE_NAME = "ScorpiovMultiCheckpointLoader";

// Checkpoint list is the same for every instance of this node, so fetch
// it once and cache the promise rather than hitting the API per node.
let _ckptListPromise = null;
function getCheckpointList() {
  if (!_ckptListPromise) {
    _ckptListPromise = api
      .fetchApi("/object_info/CheckpointLoaderSimple")
      .then((r) => r.json())
      .then((data) => {
        try {
          return data.CheckpointLoaderSimple.input.required.ckpt_name[0] || [];
        } catch (e) {
          console.error("Scorpiov Multi-Checkpoint Loader: could not read checkpoint list from /object_info", e);
          return [];
        }
      })
      .catch((err) => {
        console.error("Scorpiov Multi-Checkpoint Loader: checkpoint list fetch failed", err);
        return [];
      });
  }
  return _ckptListPromise;
}

// Best-effort short-name guess from a checkpoint filename. This is a
// starting suggestion only — the tag field is always editable, since no
// naming rule can reliably match how a person wants to abbreviate a
// checkpoint (e.g. "illustriousXL_v01.safetensors" -> "Ilus" is a
// judgment call, not something derivable in general).
function deriveTagFromFilename(name) {
  if (!name) return "";
  // Checkpoints can live in subfolders (e.g. "Illus\amanesseWorks_v20.safetensors");
  // only the filename itself is a useful tag source, never the path.
  let base = name.split(/[\\/]/).pop();
  base = base.replace(/\.[^/.]+$/, ""); // strip extension
  base = base.replace(
    /(^|[_\-\s])(v\d+(\.\d+)?|sdxl|xl|pruned|fp16|fp32|ema|noema|inpainting|refiner)(?=$|[_\-\s])/gi,
    " "
  );
  base = base.replace(/[_\-\s]+/g, "");
  if (!base) base = name.split(/[\\/]/).pop().replace(/\.[^/.]+$/, "").slice(0, 10);
  base = base.slice(0, 12);
  return base.charAt(0).toUpperCase() + base.slice(1);
}

function hideWidget(node, widgetName) {
  const widget = node.widgets?.find((w) => w.name === widgetName);
  if (!widget) return;
  // Plain (non-multiline) STRING widgets are canvas-drawn, so the
  // computeSize/draw override pattern works here (see dev reference
  // 3.19 / 5.8 for why this only works for non-multiline widgets).
  widget.computeSize = () => [0, -4];
  widget.draw = () => {};
}

function syncConfig(node) {
  const configWidget = node.widgets?.find((w) => w.name === "scorpiov_checkpoint_config");
  if (!configWidget || !node._scorpiovMCL) return;
  const slots = [];
  for (const row of node._scorpiovMCL.rowsContainer.children) {
    slots.push({
      ckpt_name: row._select.value,
      tag: row._tagInput.value,
    });
  }
  configWidget.value = JSON.stringify(slots);
  node.setDirtyCanvas(true, true);
}

function addRow(node, slot, checkpointList) {
  const rowsContainer = node._scorpiovMCL.rowsContainer;

  const row = document.createElement("div");
  row.style.display = "flex";
  row.style.gap = "4px";
  row.style.alignItems = "center";
  row.style.marginBottom = "3px";

  const select = document.createElement("select");
  select.style.flex = "1 1 auto";
  select.style.minWidth = "0";
  const options = checkpointList.includes(slot.ckpt_name)
    ? checkpointList
    : [slot.ckpt_name, ...checkpointList].filter(Boolean);
  for (const ckptName of options) {
    const opt = document.createElement("option");
    opt.value = ckptName;
    opt.textContent = ckptName;
    select.appendChild(opt);
  }
  select.value = slot.ckpt_name || (checkpointList[0] ?? "");

  const tagInput = document.createElement("input");
  tagInput.type = "text";
  tagInput.value = slot.tag || deriveTagFromFilename(select.value);
  tagInput.placeholder = "tag";
  tagInput.style.width = "70px";
  tagInput.style.flex = "0 0 auto";

  const removeBtn = document.createElement("button");
  removeBtn.textContent = "\u00d7";
  removeBtn.title = "Remove this checkpoint";
  removeBtn.style.flex = "0 0 auto";
  removeBtn.style.width = "22px";

  select.onchange = () => {
    // Only overwrite the tag with a fresh guess if the user hasn't
    // hand-edited this slot's tag already.
    if (!tagInput.dataset.userEdited) {
      tagInput.value = deriveTagFromFilename(select.value);
    }
    syncConfig(node);
  };
  tagInput.oninput = () => {
    tagInput.dataset.userEdited = "1";
    syncConfig(node);
  };
  removeBtn.onclick = () => {
    row.remove();
    syncConfig(node);
  };

  row._select = select;
  row._tagInput = tagInput;

  row.appendChild(select);
  row.appendChild(tagInput);
  row.appendChild(removeBtn);
  rowsContainer.appendChild(row);
}

async function rebuildSlotsFromConfig(node) {
  if (!node._scorpiovMCL) return;
  const configWidget = node.widgets?.find((w) => w.name === "scorpiov_checkpoint_config");
  let slots = [];
  try {
    slots = JSON.parse(configWidget?.value || "[]");
    if (!Array.isArray(slots)) slots = [];
  } catch (e) {
    slots = [];
  }

  node._scorpiovMCL.rowsContainer.innerHTML = "";
  const checkpointList = await getCheckpointList();
  for (const slot of slots) {
    addRow(node, slot, checkpointList);
    // Restored rows already have a saved tag; don't let a later
    // dropdown change silently clobber it.
    const lastRow = node._scorpiovMCL.rowsContainer.lastElementChild;
    if (lastRow) lastRow._tagInput.dataset.userEdited = "1";
  }
  node.setDirtyCanvas(true, true);
}

function setupMultiCheckpointUI(node) {
  hideWidget(node, "scorpiov_checkpoint_config");

  const container = document.createElement("div");
  container.style.display = "flex";
  container.style.flexDirection = "column";
  container.style.padding = "4px 2px";
  container.style.width = "100%";
  container.style.boxSizing = "border-box";

  const rowsContainer = document.createElement("div");
  rowsContainer.style.display = "flex";
  rowsContainer.style.flexDirection = "column";
  container.appendChild(rowsContainer);

  const addBtn = document.createElement("button");
  addBtn.textContent = "+ Add Checkpoint";
  addBtn.style.marginTop = "2px";
  addBtn.onclick = async () => {
    const checkpointList = await getCheckpointList();
    if (!checkpointList.length) {
      console.warn("Scorpiov Multi-Checkpoint Loader: no checkpoints found in your checkpoints folder.");
      return;
    }
    addRow(node, { ckpt_name: checkpointList[0], tag: deriveTagFromFilename(checkpointList[0]) }, checkpointList);
    syncConfig(node);
  };
  container.appendChild(addBtn);

  node._scorpiovMCL = { rowsContainer, container };

  const domWidget = node.addDOMWidget("scorpiov_checkpoint_ui", "scorpiov_checkpoint_ui", container, {
    serialize: false,
  });
  domWidget.serialize = false;
}

app.registerExtension({
  name: "Scorpiov.MultiCheckpointLoader",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_NAME) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      // onNodeCreated can fire more than once on error/refresh cycles
      // (dev reference 3.13-adjacent); guard against building the UI twice.
      if (this._scorpiovMCLBuilt) return;
      this._scorpiovMCLBuilt = true;
      setupMultiCheckpointUI(this);
      // Fresh node (not loaded from a saved graph): start with one slot
      // so the node isn't dropped in empty and unusable.
      //
      // IMPORTANT: the loaded-from-graph check happens INSIDE this async
      // continuation, not before it. onNodeCreated always fires before
      // onConfigure (LiteGraph creates the node, then calls .configure()
      // on it), so checking the flag synchronously here would always see
      // it as unset — even for a node being restored from a saved graph.
      // By the time this .then() callback runs, the synchronous
      // create-then-configure pass for the whole graph load has already
      // completed, so a restored node's onConfigure (and therefore its
      // flag) is guaranteed to have already run.
      getCheckpointList().then((list) => {
        if (this._scorpiovMCLLoadedFromGraph) return;
        if (list.length) {
          addRow(this, { ckpt_name: list[0], tag: deriveTagFromFilename(list[0]) }, list);
          syncConfig(this);
        }
      });
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      this._scorpiovMCLLoadedFromGraph = true;
      onConfigure?.apply(this, arguments);
      // widgets_values have been applied to widgets by this point, so the
      // hidden config widget now holds the saved JSON — rebuild the
      // visible rows from it.
      rebuildSlotsFromConfig(this);
    };
  },
});
