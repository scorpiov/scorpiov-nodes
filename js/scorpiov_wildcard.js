/**
 * Scorpiov Wildcard Node — ComfyUI UI Extension
 *
 * Adds to the node:
 *   1. A "🔄 Refresh Wildcards" button — resets serial state & rescans folder.
 *      Does NOT trigger generation. Calls POST /scorpiov/wildcard/refresh directly.
 *
 *   2. A read-only multiline text box showing the fully resolved prompt
 *      (all wildcards replaced) after each generation run.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function getWildcardFolder(node) {
    return getWidget(node, "wildcard_folder")?.value ?? "";
}

// ── Build a styled read-only textarea and attach it as a DOM widget ──────────
function addPreviewTextarea(node) {
    const container = document.createElement("div");
    container.style.cssText = [
        "width: 100%",
        "padding: 4px 0px",
        "box-sizing: border-box",
    ].join(";");

    const label = document.createElement("div");
    label.textContent = "📝 Resolved Prompt";
    label.style.cssText = [
        "font-size: 11px",
        "color: #999",
        "margin-bottom: 3px",
        "font-family: sans-serif",
        "user-select: none",
    ].join(";");

    const textarea = document.createElement("textarea");
    textarea.readOnly = true;
    textarea.value = "(Run the workflow to see the resolved prompt here)";
    textarea.rows = 6;
    textarea.style.cssText = [
        "width: 100%",
        "box-sizing: border-box",
        "background: #1a1a2e",
        "color: #aaffaa",
        "border: 1px solid #444",
        "border-radius: 4px",
        "padding: 6px 8px",
        "font-size: 11px",
        "font-family: monospace",
        "line-height: 1.5",
        "resize: vertical",
        "cursor: default",
        "outline: none",
        "white-space: pre-wrap",
        "overflow-wrap: break-word",
        "overflow-x: auto",
        "overflow-y: auto",
    ].join(";");

    // Prevent ComfyUI from stealing key events while typing in this box
    textarea.addEventListener("keydown", (e) => e.stopPropagation());
    textarea.addEventListener("mousedown", (e) => e.stopPropagation());

    container.appendChild(label);
    container.appendChild(textarea);

    // addDOMWidget renders arbitrary HTML inside the node body
    const widget = node.addDOMWidget("preview_text", "customtext", container, {
        getValue() { return textarea.value; },
        setValue(v) { textarea.value = v; },
        serialize: false,
    });

    // Store a direct reference for fast updates
    node._scorpiovPreviewTextarea = textarea;
    node._scorpiovPreviewWidget   = widget;

    return widget;
}

// ── Shared setup applied to both wildcard node types ─────────────────────────
const SCORPIOV_WILDCARD_NODES = [
    "ScorpiovWildcardProcessor",
    "ScorpiovWildcardPrompter",
];

app.registerExtension({
    name: "Scorpiov.WildcardProcessor",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!SCORPIOV_WILDCARD_NODES.includes(nodeData.name)) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const node = this;

            // ── REFRESH BUTTON ───────────────────────────────────────────
            // Pure UI action — calls our REST endpoint, never queues a prompt.
            node.addWidget(
                "button",
                "🔄 Refresh Wildcards",
                null,
                async () => {
                    const btn = getWidget(node, "🔄 Refresh Wildcards");
                    const originalLabel = "🔄 Refresh Wildcards";

                    if (btn) btn.name = "⏳ Refreshing...";
                    node.setDirtyCanvas(true, true);

                    try {
                        const response = await fetch("/scorpiov/wildcard/refresh", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                node_id: String(node.id),
                                wildcard_folder: getWildcardFolder(node),
                            }),
                        });

                        const data = await response.json();

                        if (data.status === "ok") {
                            const count = data.wildcards_found?.length ?? 0;
                            if (btn) btn.name = `✅ Done — ${count} wildcard files found`;
                            console.log("[Scorpiov Wildcard] Refreshed.", data.wildcards_found);
                        } else {
                            if (btn) btn.name = `❌ Error: ${data.message}`;
                            console.error("[Scorpiov Wildcard] Refresh error:", data.message);
                        }
                    } catch (err) {
                        if (btn) btn.name = "❌ Refresh failed (check console)";
                        console.error("[Scorpiov Wildcard] Refresh fetch failed:", err);
                    }

                    setTimeout(() => {
                        if (btn) btn.name = originalLabel;
                        node.setDirtyCanvas(true, true);
                    }, 3000);
                },
                { serialize: false }
            );

            // ── RESOLVED PROMPT PREVIEW (DOM textarea) ───────────────────
            addPreviewTextarea(node);

            return result;
        };
    },

    async setup() {
        // ── Listen for execution results from the backend ────────────────
        // When our node finishes, ComfyUI sends an "executed" websocket
        // message with the "ui" dict from process(). We pull preview_text
        // from it and update the textarea.
        api.addEventListener("executed", (event) => {
            const detail = event.detail;
            if (!detail?.output?.preview_text) return;

            const nodeId      = parseInt(detail.node);
            const resolvedText = detail.output.preview_text[0];
            if (!resolvedText) return;

            const node = app.graph.getNodeById(nodeId);
            if (!node || !SCORPIOV_WILDCARD_NODES.includes(node.comfyClass)) return;

            // Update the textarea
            if (node._scorpiovPreviewTextarea) {
                node._scorpiovPreviewTextarea.value = resolvedText;
            }

            // Auto-size the node height to fit content (capped at +300px)
            const lineCount    = (resolvedText.match(/\n/g) ?? []).length + 3;
            const textHeight   = Math.min(lineCount * 16, 300);
            const desiredHeight = 500 + textHeight;
            if (node.size[1] < desiredHeight) {
                node.setSize([node.size[0], desiredHeight]);
            }

            node.setDirtyCanvas(true, true);
        });
    },
});
