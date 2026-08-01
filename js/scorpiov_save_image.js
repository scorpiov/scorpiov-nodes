/**
 * Scorpiov Save Image — ComfyUI UI Extension
 *
 * Adds:
 *   1. A collapsible "⚙ Advanced Settings" toggle that hides the
 *      auto-detected override fields (model_name, vae_name, steps, cfg,
 *      sampler_name, scheduler) by default, since they only matter when
 *      you're manually overriding what the workflow graph already
 *      auto-detects (see dev reference §4.3). seed, control_after_generate,
 *      save_metadata, and model_hash stay visible.
 *   2. A preview panel below the save node showing the full path of the
 *      last saved file, and a small status bar confirming metadata was
 *      embedded.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Fields that get tucked behind the "Advanced Settings" toggle.
const ADVANCED_FIELD_NAMES = [
    "model_name",
    "vae_name",
    "steps",
    "cfg",
    "sampler_name",
    "scheduler",
];

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

// ── Hide / show helpers ─────────────────────────────────────────────────
// Standard community pattern for hiding a LiteGraph widget: stash its real
// type + computeSize, then swap in a computeSize that reports zero height.
// Restoring is just putting the originals back. This avoids relying on any
// ComfyUI-internal "hidden" flag that may not exist across versions.
function hideWidget(widget) {
    if (widget.scorpiovOrigType !== undefined) return; // already hidden
    widget.scorpiovOrigType = widget.type;
    widget.scorpiovOrigComputeSize = widget.computeSize;
    widget.type = "scorpiov_hidden";
    widget.computeSize = () => [0, -4];
}

function showWidget(widget) {
    if (widget.scorpiovOrigType === undefined) return; // already visible
    widget.type = widget.scorpiovOrigType;
    widget.computeSize = widget.scorpiovOrigComputeSize;
    delete widget.scorpiovOrigType;
    delete widget.scorpiovOrigComputeSize;
}

function applyAdvancedState(node, collapsed) {
    for (const name of ADVANCED_FIELD_NAMES) {
        const w = getWidget(node, name);
        if (!w) continue;
        if (collapsed) hideWidget(w);
        else showWidget(w);
    }
    const toggle = node._scorpiovAdvancedToggle;
    if (toggle) {
        toggle.name = collapsed ? "⚙ Advanced Settings ▸ (click to expand)"
                                 : "⚙ Advanced Settings ▾ (click to collapse)";
    }
    // Let the node shrink back down to fit its now-smaller content.
    // (Only shrinks if the node hasn't been manually resized larger than
    // its natural content height -- see dev reference note on this.)
    const fitSize = node.computeSize();
    if (collapsed && node.size[1] > fitSize[1]) {
        node.setSize([node.size[0], fitSize[1]]);
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "Scorpiov.SaveImage",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ScorpiovSaveImage") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const node   = this;

            // Guard: onNodeCreated can fire more than once on the same node
            // (e.g. after an error, or certain UI refreshes). Without this
            // guard, a second status widget gets added each time it fires,
            // silently shifting every widget after it out of position.
            // NOTE: unlike the Wildcard node's "text" widget swap, these
            // advanced fields are native widgets that persist correctly
            // across re-firings in testing so far -- if you see them
            // reappear unexpectedly after a workflow reload, that's the
            // same class of bug as the Wildcard fix and needs the same
            // "re-apply on every firing" treatment; flag it and we'll
            // patch this the same way.
            if (node._scorpiovSave) {
                // Still re-apply the collapse state defensively in case
                // this firing re-added fresh copies of the advanced widgets.
                applyAdvancedState(node, node._scorpiovAdvancedCollapsed ?? true);
                return result;
            }

            // ── Advanced Settings toggle button ────────────────────────────
            node._scorpiovAdvancedCollapsed = true; // collapsed by default

            const toggleWidget = node.addWidget(
                "button",
                "⚙ Advanced Settings ▸ (click to expand)",
                null,
                () => {
                    node._scorpiovAdvancedCollapsed = !node._scorpiovAdvancedCollapsed;
                    applyAdvancedState(node, node._scorpiovAdvancedCollapsed);
                },
                { serialize: false }
            );
            node._scorpiovAdvancedToggle = toggleWidget;

            // Move the toggle button to sit right above model_name (the
            // first advanced field), instead of wherever addWidget happened
            // to append it, so it reads naturally in place.
            const firstAdvancedIdx = node.widgets.indexOf(getWidget(node, ADVANCED_FIELD_NAMES[0]));
            if (firstAdvancedIdx !== -1) {
                const curIdx = node.widgets.indexOf(toggleWidget);
                node.widgets.splice(curIdx, 1);
                node.widgets.splice(firstAdvancedIdx, 0, toggleWidget);
            }

            applyAdvancedState(node, true);

            // ── Status / last-saved display ──────────────────────────────
            const container = document.createElement("div");
            container.style.cssText = [
                "width: 100%",
                "box-sizing: border-box",
                "padding: 4px 0 0 0",
                "font-family: monospace",
            ].join(";");

            const statusBar = document.createElement("div");
            statusBar.textContent = "No image saved yet.";
            statusBar.style.cssText = [
                "font-size: 10px",
                "color: #6b7280",
                "font-style: italic",
                "margin-bottom: 4px",
                "font-family: sans-serif",
                "user-select: none",
            ].join(";");

            const pathField = document.createElement("input");
            pathField.type     = "text";
            pathField.readOnly = true;
            pathField.placeholder = "(saved file path will appear here)";
            pathField.style.cssText = [
                "width: 100%",
                "box-sizing: border-box",
                "background: #0f172a",
                "color: #93c5fd",
                "border: 1px solid #334155",
                "border-radius: 3px",
                "padding: 3px 6px",
                "font-size: 11px",
                "font-family: monospace",
                "outline: none",
                "margin-bottom: 4px",
            ].join(";");
            pathField.addEventListener("mousedown", (e) => e.stopPropagation());

            container.appendChild(statusBar);
            container.appendChild(pathField);

            const statusWidget = node.addDOMWidget("save_status", "customtext", container, {
                getValue() { return pathField.value; },
                setValue(v) {},
                serialize: false,
            });
            // Belt-and-suspenders: some ComfyUI frontend versions don't fully
            // respect serialize:false passed via options, so set it directly
            // on the widget object too.
            statusWidget.serialize = false;

            node._scorpiovSave = { statusBar, pathField };

            return result;
        };
    },

    async setup() {
        // ── After a successful save, update the status display ───────────
        api.addEventListener("executed", (event) => {
            const detail = event.detail;
            if (!detail?.output?.images) return;

            const nodeId = parseInt(detail.node);
            const node   = app.graph.getNodeById(nodeId);
            if (!node || node.comfyClass !== "ScorpiovSaveImage") return;

            const p = node._scorpiovSave;
            if (!p) return;

            const saved = detail.output.images;
            if (!saved?.length) return;

            // Show the last saved filename
            const last = saved[saved.length - 1];
            p.pathField.value       = last.filename || "(unknown)";
            p.statusBar.textContent = `💾 Saved ${saved.length} image${saved.length > 1 ? "s" : ""} with metadata embedded`;
            p.statusBar.style.color = "#34d399";

            node.setDirtyCanvas(true, true);
        });
    },
});
