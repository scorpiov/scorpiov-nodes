import { app } from "../../scripts/app.js";

/*
 * Scorpiov Prompt Combiner/Router
 * ---------------------------------
 * See scorpiov_prompt_combiner.py for the architecture overview. Short
 * version: this file only handles two things client-side --
 *   1. Growing/shrinking the anything_N input slots as you wire them up
 *      (identical pattern to Scorpiov Anywhere).
 *   2. A right-click menu per input slot to set its weight and toggle it
 *      on/off, which gets mirrored into a hidden "scorpiov_config" widget
 *      so the Python side can actually see it (Python never sees
 *      node.properties directly -- only widget values and wired inputs).
 *
 * Everything else (the actual string joining / conditioning averaging)
 * happens in Python at execution time.
 */

const COMBINER_TYPE = "ScorpiovPromptCombiner";
const LOG_PREFIX = "[Scorpiov Prompt Combiner]";
const DEFAULT_SLOT_CFG = { weight: 1.0, enabled: true };

function isCombinerNode(node) {
  return node?.comfyClass === COMBINER_TYPE || node?.type === COMBINER_TYPE;
}

// ---------------------------------------------------------------------
// 1. Dynamic input growth -- same approach as Scorpiov Anywhere, with one
// important difference documented below.
//
// UNLIKE Scorpiov Anywhere, this node also declares "separator" and the
// hidden "scorpiov_config" widget in INPUT_TYPES. ComfyUI's frontend keeps
// a slot entry in node.inputs for EVERY widget that's convertible to an
// input socket -- not just the real anything_N sockets -- so node.inputs
// actually looks like:
//   [anything_1, separator, scorpiov_config, anything_2, anything_3, ...]
// Anywhere never had this problem because it declares nothing except
// anything_N, so node.inputs there only ever contains anything_N entries.
// Here, treating node.inputs.length or "the last array entry" as if it
// only contained anything_N slots undercounts/miscounts and grabs the
// wrong "last" slot. Confirmed live: with 4 real anything_N slots present,
// the old code computed node.inputs.length === 6 and proposed naming the
// next slot "anything_7" instead of "anything_5" -- and the equivalent
// mistake in pruning is what was deleting slots on a wire replace.
// Fix: always filter to anything_-prefixed entries first.
// ---------------------------------------------------------------------

function anythingInputs(node) {
  return (node.inputs || []).filter((i) => i.name && i.name.startsWith("anything_"));
}

function nextSlotName(node) {
  return `anything_${anythingInputs(node).length + 1}`;
}

function ensureTrailingEmptySlot(node) {
  const slots = anythingInputs(node);
  const last = slots[slots.length - 1];
  if (!last || last.link != null) {
    node.addInput(nextSlotName(node), "*");
  }
}

function pruneTrailingEmptySlots(node) {
  let slots = anythingInputs(node);
  while (slots.length > 1) {
    const secondLast = slots[slots.length - 2];
    const last = slots[slots.length - 1];
    if (secondLast.link == null && last.link == null) {
      // Removing must use the slot's real position in the FULL node.inputs
      // array (which also holds separator/scorpiov_config), not its
      // position within the filtered anything_-only list.
      const realIndex = node.inputs.indexOf(last);
      if (realIndex === -1) break; // shouldn't happen, but don't loop forever
      node.removeInput(realIndex);
      slots = anythingInputs(node);
    } else {
      break;
    }
  }
}

// ---------------------------------------------------------------------
// 2. Per-slot weight / enabled config.
//
// node.properties.scorpiovSlotConfig is the client-side source of truth
// (it round-trips through save/load automatically, same as any other
// property). But Python can't read node.properties -- so every time this
// changes, we also write it into a hidden STRING widget called
// "scorpiov_config", which DOES get sent to Python because it's a real
// widget value. Think of the hidden widget as "the properties, but in a
// form Python is actually allowed to see."
// ---------------------------------------------------------------------

function getSlotConfig(node, slotName) {
  return node.properties?.scorpiovSlotConfig?.[slotName] || { ...DEFAULT_SLOT_CFG };
}

function setSlotConfig(node, slotName, patch) {
  node.properties = node.properties || {};
  node.properties.scorpiovSlotConfig = node.properties.scorpiovSlotConfig || {};
  const current = node.properties.scorpiovSlotConfig[slotName] || { ...DEFAULT_SLOT_CFG };
  node.properties.scorpiovSlotConfig[slotName] = { ...current, ...patch };
  syncHiddenConfigWidget(node);
}

function findConfigWidget(node) {
  return (node.widgets || []).find((w) => w.name === "scorpiov_config");
}

function syncHiddenConfigWidget(node) {
  const widget = findConfigWidget(node);
  if (!widget) return;
  widget.value = JSON.stringify(node.properties?.scorpiovSlotConfig || {});
}

// Hide the config widget from the visible node body -- it's not meant to
// be hand-edited, only driven by the right-click menu below. This keeps
// the widget fully functional (still serialized, still sent to Python)
// while taking up no visual space.
function hideConfigWidget(node) {
  const widget = findConfigWidget(node);
  if (!widget || widget.scorpiovHidden) return;
  widget.scorpiovHidden = true;
  widget.computeSize = () => [0, -4];
  widget.draw = () => {};
}

// ---------------------------------------------------------------------
// 3. Extension registration
// ---------------------------------------------------------------------

app.registerExtension({
  name: "scorpiov.promptcombiner",

  nodeCreated(node) {
    if (!isCombinerNode(node)) return;

    hideConfigWidget(node);
    // In case this node was pasted/cloned with existing properties, make
    // sure the hidden widget actually reflects them right away.
    syncHiddenConfigWidget(node);

    // IMPORTANT: instance-level patch, not nodeType.prototype. On this
    // frontend, other extensions (and ComfyUI itself in some cases)
    // assign getSlotMenuOptions directly onto node instances, which
    // shadows anything placed on the prototype -- confirmed the hard way
    // while building Scorpiov Anywhere. Wrapping the instance's current
    // function (whatever it currently is) guarantees ours actually runs.
    const origGetSlotMenuOptions = node.getSlotMenuOptions
      ? node.getSlotMenuOptions.bind(node)
      : null;
    node.getSlotMenuOptions = function (slotInfo) {
      const existing = origGetSlotMenuOptions?.(slotInfo) || [];
      if (!slotInfo?.input) return existing; // only offer on inputs
      const input = this.inputs[slotInfo.slot];
      if (!input || !input.name?.startsWith("anything_")) return existing;

      const cfg = getSlotConfig(this, input.name);

      existing.push({
        content: `Set weight... (current: ${cfg.weight})`,
        callback: () => {
          const v = window.prompt(`Weight for ${input.name}`, String(cfg.weight));
          if (v == null) return; // cancelled
          const parsed = parseFloat(v);
          if (Number.isNaN(parsed)) {
            console.warn(`${LOG_PREFIX} ignoring non-numeric weight: ${v}`);
            return;
          }
          setSlotConfig(this, input.name, { weight: parsed });
          this.setDirtyCanvas(true, true);
        },
      });

      existing.push({
        content: cfg.enabled ? "Disable this input" : "Enable this input",
        callback: () => {
          setSlotConfig(this, input.name, { enabled: !cfg.enabled });
          this.setDirtyCanvas(true, true);
        },
      });

      return existing;
    };

    // Properties are restored by the time onConfigure runs (this is the
    // reliable point after a saved workflow loads), so re-sync the hidden
    // widget from them here too -- same reasoning as Anywhere's
    // applyPersistedLabels-on-onConfigure pattern.
    const origOnConfigure = node.onConfigure;
    node.onConfigure = function (info) {
      origOnConfigure?.apply(this, arguments);
      hideConfigWidget(this);
      syncHiddenConfigWidget(this);
    };

    const origOnConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function (type, index, connected, linkInfo) {
      origOnConnectionsChange?.apply(this, arguments);
      if (type !== LiteGraph.INPUT) return;

      const input = this.inputs[index];
      if (connected) {
        ensureTrailingEmptySlot(this);
      } else {
        // Clean up stored config for a slot that's been removed by pruning,
        // so it doesn't silently reappear if a same-named slot is recreated.
        if (input?.name && this.properties?.scorpiovSlotConfig) {
          delete this.properties.scorpiovSlotConfig[input.name];
          syncHiddenConfigWidget(this);
        }
        pruneTrailingEmptySlots(this);
      }
    };
  },
});
