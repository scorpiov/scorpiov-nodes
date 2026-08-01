/**
 * Scorpiov Wildcard Node — ComfyUI UI Extension
 *
 * Adds to the node:
 *   1. A "🎨 Highlighted Preview" read-only box directly below the real
 *      "text" input, that live-mirrors what you type with syntax coloring:
 *        - # comments and block comments between /* and *\/ markers
 *        - { } groups by nesting depth
 *        - ( ) weight parens by nesting depth (separate from { })
 *        - the ":1.5" style weight number
 *      The real "text" box itself is left completely untouched -- ComfyUI's
 *      own multiline STRING widget is already a DOM-based "customtext"
 *      widget internally, so trying to replace it fights the framework's
 *      own widget instead of adding alongside it. This preview is purely
 *      additive, the same proven pattern as the Resolved Prompt box below.
 *   2. A "🔄 Refresh Wildcards" button — resets serial state & rescans folder.
 *      Does NOT trigger generation. Calls POST /scorpiov/wildcard/refresh directly.
 *   3. A read-only multiline text box showing the fully resolved prompt
 *      (all wildcards + comments removed) after each generation run.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function getWildcardFolder(node) {
    return getWidget(node, "wildcard_folder")?.value ?? "";
}

// ── Shared visual constants ───────────────────────────────────────────────
const BRACE_COLORS  = ["#e5c07b", "#c586c0", "#56b6c2", "#98c379"]; // { } nesting depth
const PAREN_COLORS  = ["#61afef", "#d19a66", "#e06c75", "#c678dd"]; // ( ) nesting depth
const WEIGHT_COLOR  = "#e5e510"; // the ":1.5" weight number
const COMMENT_COLOR = "#6a9955";

function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function highlight(raw, warningLabel) {
    const lines = raw.split("\n");
    let inBlock = false;
    let braceDepth = 0;
    let parenDepth = 0;

    const htmlLines = lines.map((line) => {
        const trimmed = line.trim();

        if (inBlock) {
            if (trimmed === "*/") inBlock = false;
            return `<span style="color:${COMMENT_COLOR};font-style:italic">${escapeHtml(line)}</span>`;
        }
        if (trimmed === "/*") {
            inBlock = true;
            return `<span style="color:${COMMENT_COLOR};font-style:italic">${escapeHtml(line)}</span>`;
        }
        if (trimmed.startsWith("#")) {
            return `<span style="color:${COMMENT_COLOR};font-style:italic">${escapeHtml(line)}</span>`;
        }

        const hashIdx = line.indexOf("#");
        const livePart = hashIdx === -1 ? line : line.slice(0, hashIdx);
        const commentPart = hashIdx === -1 ? "" : line.slice(hashIdx);

        let colored = "";
        let i = 0;
        while (i < livePart.length) {
            const ch = livePart[i];

            if (ch === "{") {
                colored += `<span style="color:${BRACE_COLORS[braceDepth % BRACE_COLORS.length]}">{</span>`;
                braceDepth++;
                i++;
            } else if (ch === "}") {
                braceDepth = Math.max(0, braceDepth - 1);
                colored += `<span style="color:${BRACE_COLORS[braceDepth % BRACE_COLORS.length]}">}</span>`;
                i++;
            } else if (ch === "(") {
                colored += `<span style="color:${PAREN_COLORS[parenDepth % PAREN_COLORS.length]}">(</span>`;
                parenDepth++;
                i++;
            } else if (ch === ")") {
                parenDepth = Math.max(0, parenDepth - 1);
                colored += `<span style="color:${PAREN_COLORS[parenDepth % PAREN_COLORS.length]}">)</span>`;
                i++;
            } else if (ch === ":" && /^-?\d*\.?\d+/.test(livePart.slice(i + 1))) {
                const match = livePart.slice(i + 1).match(/^-?\d*\.?\d+/)[0];
                colored += `<span style="color:${WEIGHT_COLOR}">:${escapeHtml(match)}</span>`;
                i += 1 + match.length;
            } else {
                colored += escapeHtml(ch);
                i++;
            }
        }

        return colored + (commentPart
            ? `<span style="color:${COMMENT_COLOR};font-style:italic">${escapeHtml(commentPart)}</span>`
            : "");
    });

    if (warningLabel) {
        const warnings = [];
        if (braceDepth > 0) warnings.push(`${braceDepth} unclosed {`);
        if (parenDepth > 0) warnings.push(`${parenDepth} unclosed (`);
        warningLabel.textContent = warnings.length ? `⚠ ${warnings.join(", ")}` : "";
    }

    return htmlLines.join("\n");
}

// ── Live highlighted preview (read-only, mirrors the real "text" widget) ───
function addHighlightedPreview(node) {
    const container = document.createElement("div");
    container.style.cssText = "width: 100%; padding: 4px 0px; box-sizing: border-box;";

    const label = document.createElement("div");
    label.textContent = "🎨 Highlighted Preview (live, read-only)";
    label.style.cssText = [
        "font-size: 11px", "color: #999", "margin-bottom: 3px",
        "font-family: sans-serif", "user-select: none",
    ].join(";");

    const warningLabel = document.createElement("div");
    warningLabel.style.cssText = [
        "font-size: 10px", "color: #e06c75", "font-family: sans-serif",
        "min-height: 14px", "margin-bottom: 2px",
    ].join(";");

    const colorBox = document.createElement("div");
    colorBox.style.cssText = [
        "width: 100%", "box-sizing: border-box", "background: #1a1a2e",
        "border: 1px solid #444", "border-radius: 4px", "padding: 6px 8px",
        "font-size: 12px", "font-family: monospace", "line-height: 1.5",
        "white-space: pre-wrap", "word-wrap: break-word",
        "min-height: 100px", "max-height: 400px", "overflow: auto",
        "resize: vertical", "color: #ddd",
    ].join(";");

    container.appendChild(label);
    container.appendChild(warningLabel);
    container.appendChild(colorBox);

    function render() {
        const textWidget = getWidget(node, "text");
        const raw = textWidget?.value ?? "";
        colorBox.innerHTML = highlight(raw, warningLabel) + "\n";
    }

    render();

    const widget = node.addDOMWidget("scorpiov_highlight_preview", "customtext", container, {
        getValue() { return ""; },
        setValue() {},
        serialize: false,
    });
    widget.serialize = false;

    // Poll the real "text" widget for changes rather than relying on
    // framework-internal change events, which may not fire the same way
    // (or at all, on every keystroke) across ComfyUI frontend versions.
    // Cheap enough at this interval to not matter performance-wise.
    let lastSeen = null;
    const intervalId = setInterval(() => {
        const textWidget = getWidget(node, "text");
        const current = textWidget?.value ?? "";
        if (current !== lastSeen) {
            lastSeen = current;
            render();
        }
    }, 250);

    node._scorpiovHighlightInterval = intervalId;

    // Clean up the interval if the node is deleted, so it doesn't keep
    // polling (and referencing a stale node) forever.
    const onRemoved = node.onRemoved;
    node.onRemoved = function () {
        clearInterval(intervalId);
        return onRemoved?.apply(this, arguments);
    };

    return widget;
}

// ── Read-only resolved-prompt preview ───────────────────────────────────────
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

            // Guard: onNodeCreated can fire more than once on the same node.
            // Without this, a second highlighted-preview widget gets added
            // each time it fires, duplicating it and shifting other widgets.
            if (!node._scorpiovHighlightAdded) {
                const previewWidget = addHighlightedPreview(node);

                // Move it to sit right after the real "text" widget, so it
                // visually reads as "the colored version of the box above".
                const textIdx = node.widgets.indexOf(getWidget(node, "text"));
                if (textIdx !== -1) {
                    const curIdx = node.widgets.indexOf(previewWidget);
                    node.widgets.splice(curIdx, 1);
                    node.widgets.splice(textIdx + 1, 0, previewWidget);
                }

                node._scorpiovHighlightAdded = true;
                console.log("[Scorpiov Wildcard] Highlighted preview widget added:", node.id);
            }

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
