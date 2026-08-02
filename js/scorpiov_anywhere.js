import { app } from "../../scripts/app.js";

/*
 * Scorpiov Anywhere
 * ------------------
 * See scorpiov_anywhere.py for the architecture overview. Short version:
 * this file finds "broadcast sources" (either a Scorpiov Anywhere node's
 * connected inputs, or any node you've flagged via right-click), matches
 * them against unconnected inputs elsewhere in the graph, and rewrites the
 * serialized prompt right before it's sent to the backend.
 *
 * FIRST THING TO VERIFY ON YOUR MACHINE:
 * Open the browser console on your live ComfyUI page and run:
 *     typeof app.graphToPrompt
 * It should print "function". If it prints "undefined", your frontend
 * version exposes serialization under a different name/path -- tell me
 * exactly what console.log(app) shows near "graphToPrompt" / "queuePrompt"
 * and I'll adjust the hook. Everything else in this file (dynamic inputs,
 * the broadcast toggle) does not depend on this and can be tested
 * independently first.
 */

const ANYWHERE_TYPE = "ScorpiovAnywhere";
const LOG_PREFIX = "[Scorpiov Anywhere]";

// ---------------------------------------------------------------------
// 1. Dynamic input growth for the Scorpiov Anywhere node itself
// ---------------------------------------------------------------------

function isAnywhereNode(node) {
  return node?.comfyClass === ANYWHERE_TYPE || node?.type === ANYWHERE_TYPE;
}

function nextAnywhereSlotName(node) {
  return `anything_${(node.inputs?.length ?? 0) + 1}`;
}

function ensureTrailingEmptySlot(node) {
  const inputs = node.inputs || [];
  const last = inputs[inputs.length - 1];
  // If every existing slot is filled, grow a new empty one.
  if (!last || last.link != null) {
    node.addInput(nextAnywhereSlotName(node), "*");
  }
}

function pruneTrailingEmptySlots(node) {
  const inputs = node.inputs || [];
  // Keep exactly one empty trailing slot; remove extra empty ones above it.
  while (inputs.length > 1) {
    const secondLast = inputs[inputs.length - 2];
    const last = inputs[inputs.length - 1];
    if (secondLast.link == null && last.link == null) {
      node.removeInput(inputs.length - 1);
    } else {
      break;
    }
  }
}

// Manual renames need to survive save/reload, but a plain `input.label`
// assignment does NOT get persisted by ComfyUI's standard node
// serialization -- only `node.properties` is guaranteed to round-trip.
// So `node.properties.scorpiovLabels` (keyed by the stable slot name,
// e.g. "anything_4", not the display label) is the actual source of
// truth; `input.label` is just a live, disposable mirror of it that gets
// rebuilt on load and after every reconnect.
function applyPersistedLabels(node) {
  const saved = node.properties?.scorpiovLabels;
  if (!saved) return;
  for (const input of node.inputs || []) {
    if (saved[input.name] != null) {
      input.label = saved[input.name];
      input.scorpiovManualLabel = true;
    }
  }
}

function setPersistedLabel(node, inputName, value) {
  node.properties = node.properties || {};
  node.properties.scorpiovLabels = node.properties.scorpiovLabels || {};
  if (value) {
    node.properties.scorpiovLabels[inputName] = value;
  } else {
    delete node.properties.scorpiovLabels[inputName];
  }
}

// ---------------------------------------------------------------------
// 2. "Any-node broadcasting" -- flag an arbitrary node's outputs
// ---------------------------------------------------------------------

function toggleBroadcastFlag(node) {
  node.scorpiovBroadcast = !node.scorpiovBroadcast;
  node.setDirtyCanvas(true, true);
}

// ---------------------------------------------------------------------
// 3. Minimal visual indicator (small dot, top-left of node body)
// ---------------------------------------------------------------------

function drawBroadcastBadge(node, ctx) {
  const isSource = isAnywhereNode(node) || node.scorpiovBroadcast === true;
  if (!isSource) return;
  ctx.save();
  ctx.beginPath();
  ctx.arc(8, 8, 5, 0, Math.PI * 2);
  ctx.fillStyle = isAnywhereNode(node) ? "#6aa3ff" : "#7fd88f";
  ctx.fill();
  ctx.restore();
}

// ---------------------------------------------------------------------
// 4. The matching engine
// ---------------------------------------------------------------------

// A "resolved name" tries to find something more useful than "anything_1"
// for name-based matching: prefer a link's origin output name, since that's
// usually meaningful (e.g. "MODEL", "positive"), falling back to the local
// slot's own name/label if the user renamed it manually on the canvas.
function resolveSourceName(originOutputName, localSlot) {
  const label = localSlot?.label || localSlot?.name;
  const looksDefaulted = !label || /^anything_\d+$/i.test(label);
  return looksDefaulted ? originOutputName : label;
}

function collectBroadcastSources(graph) {
  // nodeId/slotIndex = the REAL upstream output the backend link should point
  // to (needed for applyBroadcastLinks). broadcasterNodeId = the node that
  // should visually represent this broadcast (the Anywhere node itself, or
  // the flagged node) -- these differ for Anywhere nodes, since the data
  // actually originates further upstream. colorKey lets the visualizer give
  // each distinct broadcast slot a consistent color.
  const sources = [];

  for (const node of graph._nodes) {
    if (isAnywhereNode(node)) {
      node.inputs?.forEach((input, slotIndex) => {
        if (input.link == null) return;
        const link = graph.links[input.link];
        if (!link) return;
        const originNode = graph.getNodeById(link.origin_id);
        if (!originNode) return;
        const originOutput = originNode.outputs?.[link.origin_slot];
        if (!originOutput) return;
        sources.push({
          nodeId: originNode.id,
          slotIndex: link.origin_slot,
          type: originOutput.type,
          name: resolveSourceName(originOutput.name, input),
          broadcasterNodeId: node.id,
          // For an Anywhere node, the visible "source" dot is its OWN input
          // (the anything_N slot the real data flows into), not the real
          // upstream output -- that's what a wire should visually connect to.
          broadcasterSlotIndex: slotIndex,
          broadcasterSlotIsInput: true,
          colorKey: `${node.id}:${slotIndex}`,
        });
      });
    } else if (node.scorpiovBroadcast === true) {
      (node.outputs || []).forEach((output, slotIndex) => {
        sources.push({
          nodeId: node.id,
          slotIndex,
          type: output.type,
          name: output.name,
          broadcasterNodeId: node.id,
          // For a flagged regular node, the source dot IS the real output.
          broadcasterSlotIndex: slotIndex,
          broadcasterSlotIsInput: false,
          colorKey: `${node.id}:${slotIndex}`,
        });
      });
    }
  }
  return sources;
}

function collectBroadcastTargets(graph) {
  const targets = []; // { nodeId, inputName, type }

  for (const node of graph._nodes) {
    if (isAnywhereNode(node)) continue; // never target our own broadcaster
    for (const input of node.inputs || []) {
      if (input.link != null) continue; // already wired, leave it alone
      if (!input.type || input.type === "*") continue; // nothing safe to match
      targets.push({ nodeId: node.id, inputName: input.name, type: input.type });
    }
  }
  return targets;
}

// Cycle safety: a broadcast match is only as good as its type/name score --
// nothing so far checks whether ACCEPTING it would wire a node's output
// back into something upstream of itself, closing a loop. ComfyUI's backend
// correctly refuses to run graphs with cycles, so we have to catch this
// ourselves before ever proposing the link, not just hope it works out.

// downstream[nodeId] = Set of node ids that nodeId's output currently
// (directly or indirectly) feeds, based on real, already-drawn connections
// PLUS any broadcast links already accepted earlier in this same pass.
function buildDownstreamMap(graph) {
  const downstream = new Map();
  for (const node of graph._nodes) {
    for (const input of node.inputs || []) {
      if (input.link == null) continue;
      const link = graph.links[input.link];
      if (!link) continue;
      if (!downstream.has(link.origin_id)) downstream.set(link.origin_id, new Set());
      downstream.get(link.origin_id).add(node.id);
    }
  }
  return downstream;
}

function canReach(downstream, fromId, toId) {
  const seen = new Set();
  const stack = [fromId];
  while (stack.length) {
    const current = stack.pop();
    if (current === toId) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    for (const next of downstream.get(current) || []) stack.push(next);
  }
  return false;
}

function recordEdge(downstream, fromId, toId) {
  if (!downstream.has(fromId)) downstream.set(fromId, new Set());
  downstream.get(fromId).add(toId);
}

// Returns Map<"nodeId::inputName", {nodeId, slotIndex}> of winning source per target,
// plus logs any unresolved ties to the console so they're visible, not silent.
function resolveBroadcastLinks(graph, { verbose = true } = {}) {
  const sources = collectBroadcastSources(graph);
  const targets = collectBroadcastTargets(graph);
  const winners = new Map();
  const downstream = buildDownstreamMap(graph);

  for (const target of targets) {
    let best = null;
    let bestScore = -1;
    let tie = false;

    for (const source of sources) {
      if (source.type !== target.type) continue;
      const nameMatch =
        source.name && target.inputName &&
        source.name.toLowerCase() === target.inputName.toLowerCase();
      const score = nameMatch ? 2 : 1;

      if (score > bestScore) {
        best = source;
        bestScore = score;
        tie = false;
      } else if (score === bestScore) {
        tie = true;
      }
    }

    if (best && !tie) {
      // Would accepting this match wire target's own output back into
      // something upstream of source, closing a loop? Check BEFORE
      // accepting, using the graph state as it stands after any earlier
      // accepted matches in this same pass -- multiple broadcasts can
      // combine to create a cycle even if no single one looks dangerous alone.
      if (canReach(downstream, target.nodeId, best.nodeId)) {
        if (verbose) {
          console.warn(
            `${LOG_PREFIX} skipped broadcast into node ${target.nodeId} input "${target.inputName}" -- ` +
            `would create a dependency cycle back to node ${best.nodeId}. ` +
            `Wire this one manually instead.`
          );
        }
        continue;
      }
      recordEdge(downstream, best.nodeId, target.nodeId);
      winners.set(`${target.nodeId}::${target.inputName}`, best);
    } else if (best && tie) {
      if (verbose) {
        console.warn(
          `${LOG_PREFIX} conflict: multiple equally-good sources for ` +
          `node ${target.nodeId} input "${target.inputName}" (type ${target.type}). ` +
          `Skipped -- connect it manually or rename one source to disambiguate.`
        );
      }
    }
  }
  return winners;
}

// ---------------------------------------------------------------------
// 5. Selection-based faint wire visualization
// ---------------------------------------------------------------------
// Nothing is drawn until you select a node -- this is purely a "help me
// see what's connected" aid, on demand, so it never clutters the canvas.
//   a) select a Scorpiov Anywhere (or broadcast-flagged) node -> faint
//      wires appear to every endpoint it currently feeds
//   b) select an endpoint node -> a faint wire appears back to whichever
//      broadcaster is feeding it
//   c) each distinct broadcast slot gets its own consistent color

const WIRE_PALETTE = [
  "#6aa3ff", "#7fd88f", "#ffb86a", "#ff6a9e", "#c58aff", "#6adfff", "#ffe36a",
];

function colorForKey(key) {
  let hash = 0;
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  return WIRE_PALETTE[hash % WIRE_PALETTE.length];
}

function nodeAnchor(node, side) {
  const pos = node.pos || [0, 0];
  const size = node.size || [140, 60];
  return {
    x: side === "right" ? pos[0] + size[0] : pos[0],
    y: pos[1] + size[1] / 2,
  };
}

// The real, precise position of a specific slot dot -- the same lookup
// LiteGraph itself uses to draw actual links, so our wires land exactly on
// the dot instead of a generic guess at the node's edge. Falls back to the
// rough edge-center only if something's missing (e.g. slot got removed
// between resolving and drawing this frame).
function slotAnchor(node, isInput, slotIndex) {
  if (typeof node.getConnectionPos === "function" && slotIndex != null && slotIndex >= 0) {
    const pos = node.getConnectionPos(isInput, slotIndex);
    if (pos) return { x: pos[0], y: pos[1] };
  }
  return nodeAnchor(node, isInput ? "left" : "right");
}

function findInputIndex(node, name) {
  return node.inputs?.findIndex((i) => i.name === name) ?? -1;
}

function drawFaintWire(ctx, from, to, color) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.globalAlpha = 0.4;
  ctx.lineWidth = 2;
  ctx.setLineDash([5, 5]);
  ctx.beginPath();
  ctx.moveTo(from.x, from.y);
  const midX = (from.x + to.x) / 2;
  ctx.bezierCurveTo(midX, from.y, midX, to.y, to.x, to.y);
  ctx.stroke();
  ctx.restore();
}

// Throttle cache for the visualizer specifically. The draw loop runs at
// display refresh rate (commonly 60fps) whenever anything is selected --
// recomputing the full matching engine (a scan of every node/slot in the
// graph, plus cycle-detection BFS) on every single one of those frames was
// a real, serious bug: constant wasted CPU work, and every skipped-match
// warning firing dozens of times per second forever. Recompute at most a
// few times a second; that's imperceptible for a "help me see connections"
// aid, and applyBroadcastLinks (the actual submission path) always
// recomputes fresh regardless of this cache.
const WIRE_CACHE_INTERVAL_MS = 200;
let wireCache = { winners: null, computedAt: 0 };

function getWinnersForVisualization(graph) {
  const now = Date.now();
  if (!wireCache.winners || now - wireCache.computedAt > WIRE_CACHE_INTERVAL_MS) {
    wireCache = { winners: resolveBroadcastLinks(graph, { verbose: false }), computedAt: now };
  }
  return wireCache.winners;
}

// Runs as LGraphCanvas.prototype.onDrawForeground, so `this` is the canvas
// and ctx is already in graph-space -- node.pos/node.size can be used directly.
function drawScorpiovSelectionWires(ctx) {
  const graph = this.graph;
  if (!graph) return;
  const selectedIds = Object.keys(this.selected_nodes || {});
  if (selectedIds.length === 0) return; // nothing selected -> draw nothing, cheap exit

  const winners = getWinnersForVisualization(graph);

  for (const idStr of selectedIds) {
    const selNode = graph.getNodeById(Number(idStr));
    if (!selNode) continue;

    // (a) selected node is a broadcaster -> wires out to every endpoint it feeds
    if (isAnywhereNode(selNode) || selNode.scorpiovBroadcast === true) {
      for (const [key, source] of winners.entries()) {
        if (source.broadcasterNodeId !== selNode.id) continue;
        const [targetIdStr, inputName] = key.split("::");
        const targetNode = graph.getNodeById(Number(targetIdStr));
        if (!targetNode) continue;
        drawFaintWire(
          ctx,
          slotAnchor(selNode, source.broadcasterSlotIsInput, source.broadcasterSlotIndex),
          slotAnchor(targetNode, true, findInputIndex(targetNode, inputName)),
          colorForKey(source.colorKey)
        );
      }
    }

    // (b) selected node is an endpoint -> wire back to whichever broadcaster feeds it
    for (const [key, source] of winners.entries()) {
      const [targetIdStr, inputName] = key.split("::");
      if (Number(targetIdStr) !== selNode.id) continue;
      const broadcasterNode = graph.getNodeById(source.broadcasterNodeId);
      if (!broadcasterNode) continue;
      drawFaintWire(
        ctx,
        slotAnchor(selNode, true, findInputIndex(selNode, inputName)),
        slotAnchor(broadcasterNode, source.broadcasterSlotIsInput, source.broadcasterSlotIndex),
        colorForKey(source.colorKey)
      );
    }
  }
}

// ---------------------------------------------------------------------
// 6. Rewrite the serialized prompt right before submission
// ---------------------------------------------------------------------

function applyBroadcastLinks(prompt, graph) {
  const winners = resolveBroadcastLinks(graph);

  for (const [key, source] of winners.entries()) {
    const [nodeId, inputName] = key.split("::");
    const targetEntry = prompt.output[nodeId];
    if (!targetEntry) continue;
    targetEntry.inputs[inputName] = [String(source.nodeId), source.slotIndex];
  }

  // Scorpiov Anywhere nodes carry no real backend behavior once their
  // connections have been rewired directly -- strip them from the prompt.
  for (const node of graph._nodes) {
    if (isAnywhereNode(node) && prompt.output[node.id]) {
      delete prompt.output[node.id];
    }
  }
}

// ---------------------------------------------------------------------
// 7. Extension registration
// ---------------------------------------------------------------------

app.registerExtension({
  name: "scorpiov.anywhere",

  async setup() {
    if (typeof app.graphToPrompt !== "function") {
      console.error(
        `${LOG_PREFIX} app.graphToPrompt not found -- broadcasting will NOT work ` +
        `on this ComfyUI frontend version. Dynamic inputs and the broadcast toggle ` +
        `still work. Open the console, inspect \`app\`, and report back what the ` +
        `serialization method is actually called.`
      );
      return;
    }
    const original = app.graphToPrompt.bind(app);
    app.graphToPrompt = async function (...args) {
      const prompt = await original(...args);
      try {
        applyBroadcastLinks(prompt, app.graph);
      } catch (err) {
        console.error(`${LOG_PREFIX} failed to apply broadcast links:`, err);
      }
      return prompt;
    };
    console.log(`${LOG_PREFIX} graph rewrite hook installed.`);

    const CanvasClass = window.LGraphCanvas || (typeof LGraphCanvas !== "undefined" ? LGraphCanvas : null);
    if (!CanvasClass || !app.canvas) {
      console.error(
        `${LOG_PREFIX} app.canvas not found -- selection wire visualization ` +
        `will NOT appear. Everything else still works.`
      );
      return;
    }
    // IMPORTANT: on this frontend, app.canvas carries its OWN instance-level
    // onDrawForeground that shadows LGraphCanvas.prototype.onDrawForeground
    // entirely -- patching the prototype (what earlier versions of this file
    // did) silently never fires. Patch the actual instance instead.
    const origOnDrawForeground = app.canvas.onDrawForeground?.bind(app.canvas);
    app.canvas.onDrawForeground = function (ctx, ...rest) {
      origOnDrawForeground?.(ctx, ...rest);
      try {
        drawScorpiovSelectionWires.call(this, ctx);
      } catch (err) {
        console.error(`${LOG_PREFIX} selection wire draw failed:`, err);
      }
    };
    console.log(`${LOG_PREFIX} selection wire visualizer installed.`);
  },

  beforeRegisterNodeDef(nodeType, nodeData) {
    // Add "Toggle Scorpiov Broadcasting" to every node's right-click menu.
    const origGetExtraMenuOptions = nodeType.prototype.getExtraMenuOptions;
    nodeType.prototype.getExtraMenuOptions = function (canvas, options) {
      origGetExtraMenuOptions?.apply(this, arguments);
      if (nodeData.name === ANYWHERE_TYPE) return; // doesn't need the toggle on itself
      options.push({
        content: this.scorpiovBroadcast
          ? "Remove Scorpiov Broadcasting"
          : "Add Scorpiov Broadcasting",
        callback: () => toggleBroadcastFlag(this),
      });
    };

    const origOnDrawForeground = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (ctx) {
      origOnDrawForeground?.apply(this, arguments);
      drawBroadcastBadge(this, ctx);
    };
  },

  nodeCreated(node) {
    if (!isAnywhereNode(node)) return;

    // Re-apply any manually-set names saved in this node's properties.
    // Needed both right after creation (properties may already be present
    // if this node came from a pasted/cloned node) and -- more importantly
    // -- after ComfyUI restores a saved graph, since onConfigure (below)
    // is where the persisted workflow JSON actually lands.
    applyPersistedLabels(node);

    // IMPORTANT: this MUST be an instance-level patch, not a
    // nodeType.prototype patch (that was the original, broken approach).
    // Another extension in this environment assigns its own
    // getSlotMenuOptions directly onto each node instance, which shadows
    // anything placed on the prototype -- the exact same class of bug as
    // the onDrawForeground/app.canvas issue elsewhere in this file. Wrapping
    // the instance's CURRENT function (whatever it is, native or another
    // extension's) guarantees ours actually runs.
    const origGetSlotMenuOptions = node.getSlotMenuOptions ? node.getSlotMenuOptions.bind(node) : null;
    node.getSlotMenuOptions = function (slotInfo) {
      const existing = origGetSlotMenuOptions?.(slotInfo) || [];
      if (!slotInfo?.input) return existing; // only offer on inputs, not outputs
      const input = this.inputs[slotInfo.slot];
      if (!input) return existing;
      existing.push({
        content: "Rename for Scorpiov matching...",
        callback: () => {
          // window.prompt is used deliberately, not as a fallback: it's a
          // real browser dialog, always fully on-screen and centered with
          // guaranteed OK/Cancel buttons. app.canvas.prompt draws its own
          // dialog near the cursor with no viewport clamping, so a
          // right-click near a screen edge can spawn it partly or fully
          // off-screen with no way to reach its buttons.
          const v = window.prompt("Match name (e.g. positive / negative)", input.label || input.name);
          if (v == null) return; // cancelled
          if (!v) {
            // Empty submission clears the persisted override and returns to auto-labeling.
            setPersistedLabel(this, input.name, null);
            input.scorpiovManualLabel = false;
            const link = input.link != null ? this.graph?.links?.[input.link] : null;
            const originNode = link ? this.graph?.getNodeById(link.origin_id) : null;
            const originOutput = originNode?.outputs?.[link?.origin_slot];
            input.label = originOutput?.name;
          } else {
            setPersistedLabel(this, input.name, v);
            input.label = v;
            input.scorpiovManualLabel = true;
          }
          this.setDirtyCanvas(true, true);
        },
      });
      return existing;
    };

    const origOnConfigure = node.onConfigure;
    node.onConfigure = function (info) {
      origOnConfigure?.apply(this, arguments);
      // `info` is this node's own saved data as it existed in the workflow
      // file -- properties (and therefore scorpiovLabels) are restored by
      // the time this runs, so this is the reliable point to reapply them.
      applyPersistedLabels(this);
    };

    const origOnConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function (type, index, connected, linkInfo) {
      origOnConnectionsChange?.apply(this, arguments);
      if (type !== LiteGraph.INPUT) return;

      const input = this.inputs[index];
      const hasPersistedOverride = this.properties?.scorpiovLabels?.[input?.name] != null;

      if (connected) {
        // Auto-label from the origin output's name (MODEL, CLIP, VAE, ...)
        // purely for readability -- unless this slot was manually renamed
        // via "Rename for Scorpiov matching...", which takes priority and
        // is never overwritten automatically. A persisted override always
        // wins, even if the transient flag hasn't been set yet this session.
        if (input && hasPersistedOverride) {
          input.label = this.properties.scorpiovLabels[input.name];
          input.scorpiovManualLabel = true;
        } else if (input && linkInfo && !input.scorpiovManualLabel) {
          const originNode = this.graph?.getNodeById(linkInfo.origin_id);
          const originOutput = originNode?.outputs?.[linkInfo.origin_slot];
          if (originOutput?.name) input.label = originOutput.name;
        }
        ensureTrailingEmptySlot(this);
      } else {
        if (input && !hasPersistedOverride && !input.scorpiovManualLabel) input.label = undefined;
        pruneTrailingEmptySlots(this);
      }
    };
  },
});
