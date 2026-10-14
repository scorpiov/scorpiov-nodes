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

// ── Build a styled read-only textarea and attach it as a DOM widget ──────────
// Sizing is user-controlled: the textarea has a fixed initial height and
// stays scrollable; we deliberately never resize the node from code (the
// old auto-grow handler ballooned the small Prompter node after every run
// and only ever grew it).
function addPreviewTextarea(node) {
    const container = document.createElement("div");
    container.style.cssText = [
        "width: 100%",
        "padding: 4px 0px",
        "box-sizing: border-box",
    ].join(";");

    // Header row: label left, character count + copy button right
    const headerRow = document.createElement("div");
    headerRow.style.cssText = [
        "display: flex",
        "align-items: center",
        "justify-content: space-between",
        "margin-bottom: 3px",
    ].join(";");

    const label = document.createElement("div");
    label.textContent = "📝 Resolved Prompt";
    label.style.cssText = [
        "font-size: 11px",
        "color: #9aa4b2",
        "font-family: sans-serif",
        "user-select: none",
    ].join(";");

    const rightGroup = document.createElement("div");
    rightGroup.style.cssText = [
        "display: flex",
        "align-items: center",
        "gap: 6px",
    ].join(";");

    const countEl = document.createElement("span");
    countEl.textContent = "";
    countEl.title = "Character count of the resolved prompt";
    countEl.style.cssText = [
        "font-size: 10px",
        "color: #9aa4b2",
        "font-family: sans-serif",
        "user-select: none",
    ].join(";");

    const copyBtn = document.createElement("button");
    copyBtn.textContent = "⧉ Copy";
    copyBtn.title = "Copy resolved prompt to clipboard";
    copyBtn.style.cssText = [
        "font-size: 10px",
        "padding: 1px 6px",
        "background: #171a21",
        "color: #cfd8e3",
        "border: 1px solid #374151",
        "border-radius: 3px",
        "cursor: pointer",
        "font-family: sans-serif",
        "outline: none",
    ].join(";");
    // Keep ComfyUI from treating the button click as a node drag
    copyBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    copyBtn.addEventListener("keydown", (e) => e.stopPropagation());
    copyBtn.onclick = () => copyResolvedPrompt(node, copyBtn);

    rightGroup.appendChild(countEl);
    rightGroup.appendChild(copyBtn);
    headerRow.appendChild(label);
    headerRow.appendChild(rightGroup);
    container.appendChild(headerRow);

    const textarea = document.createElement("textarea");
    textarea.readOnly = true;
    textarea.value = "(Run the workflow to see the resolved prompt here)";
    textarea.rows = 6;
    textarea.style.cssText = [
        "width: 100%",
        "height: 120px",
        "box-sizing: border-box",
        "background: #171a21",
        "color: #cfd8e3",
        "border: 1px solid #374151",
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

    container.appendChild(textarea);

    // addDOMWidget renders arbitrary HTML inside the node body
    const widget = node.addDOMWidget("preview_text", "customtext", container, {
        getValue() { return textarea.value; },
        setValue(v) { textarea.value = v ?? ""; updatePreviewCount(node); },
        serialize: false,
    });

    // Store direct references for fast updates
    node._scorpiovPreviewTextarea = textarea;
    node._scorpiovPreviewWidget   = widget;
    node._scorpiovPreviewCountEl  = countEl;

    return widget;
}

// Refresh the "N chars" counter from the current textarea content.
function updatePreviewCount(node) {
    const el = node._scorpiovPreviewCountEl;
    const ta = node._scorpiovPreviewTextarea;
    if (!el || !ta) return;
    const n = ta.value ? ta.value.length : 0;
    el.textContent = n ? `${n} chars` : "";
}

function flashCopyButton(btn, text) {
    const original = "⧉ Copy";
    btn.textContent = text;
    setTimeout(() => { btn.textContent = original; }, 1500);
}

function copyResolvedPrompt(node, btn) {
    const text = node._scorpiovPreviewTextarea?.value ?? "";
    if (!text) return;
    const done = () => flashCopyButton(btn, "✓ Copied");
    if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(text).then(done, () => fallbackCopyToClipboard(text, done));
    } else {
        // Non-secure contexts (plain http LAN hosts) don't expose
        // navigator.clipboard — use the classic hidden-textarea fallback.
        fallbackCopyToClipboard(text, done);
    }
}

function fallbackCopyToClipboard(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position: fixed; opacity: 0; pointer-events: none;";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* clipboard unavailable */ }
    ta.remove();
    done();
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

            // Update the textarea and the character count. Node size is
            // user-controlled — the old auto-grow logic (desiredHeight =
            // 500 + textHeight) ballooned the small Prompter node after
            // every run and could only ever grow it.
            if (node._scorpiovPreviewTextarea) {
                node._scorpiovPreviewTextarea.value = resolvedText;
                updatePreviewCount(node);
            }

            node.setDirtyCanvas(true, true);
        });
    },
});
