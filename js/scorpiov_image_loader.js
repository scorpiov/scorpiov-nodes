/**
 * Scorpiov Image Loader — ComfyUI UI Extension
 *
 * The image-upload widget (thumbnail preview, upload button, folder browser)
 * is built into ComfyUI's core and activates automatically for any node whose
 * INPUT_TYPES contains a widget with { image_upload: True }.
 *
 * What this file adds:
 *   1. A read-only info bar below the image picker that shows the resolved
 *      filename and full path after each run — handy at a glance.
 *   2. Auto-resize: the node grows tall enough to show the thumbnail
 *      comfortably without scrolling.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

app.registerExtension({
    name: "Scorpiov.ImageLoader",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ScorpiovImageLoader") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const node   = this;

            // ── Info bar (shown after a run) ─────────────────────────────
            const container = document.createElement("div");
            container.style.cssText = [
                "width: 100%",
                "box-sizing: border-box",
                "padding: 4px 0 0 0",
                "font-family: monospace",
            ].join(";");

            function makeInfoRow(icon, labelText) {
                const row = document.createElement("div");
                row.style.cssText = "margin-bottom: 4px;";

                const lbl = document.createElement("div");
                lbl.textContent = `${icon} ${labelText}`;
                lbl.style.cssText = [
                    "font-size: 10px",
                    "color: #9aa4b2",
                    "user-select: none",
                    "margin-bottom: 1px",
                ].join(";");

                const field = document.createElement("input");
                field.type     = "text";
                field.readOnly = true;
                field.value    = "(run the workflow to populate)";
                field.style.cssText = [
                    "width: 100%",
                    "box-sizing: border-box",
                    "background: #171a21",
                    "color: #93c5fd",
                    "border: 1px solid #374151",
                    "border-radius: 3px",
                    "padding: 3px 6px",
                    "font-size: 11px",
                    "font-family: monospace",
                    "outline: none",
                ].join(";");
                field.addEventListener("mousedown", (e) => e.stopPropagation());

                row.appendChild(lbl);
                row.appendChild(field);
                container.appendChild(row);
                return field;
            }

            const filenameField = makeInfoRow("📄", "Filename");
            const pathField     = makeInfoRow("📁", "Full Path");

            node.addDOMWidget("path_display", "customtext", container, {
                getValue() { return filenameField.value; },
                setValue(v) { /* display only */ },
                serialize: false,
            });

            // Store references for the executed handler
            node._scorpiovLoader = { filenameField, pathField };

            // Start at a comfortable height for the thumbnail + info bar
            node.setSize([node.size[0], Math.max(node.size[1], 320)]);

            return result;
        };
    },

    async setup() {
        // ── Populate the info bar after each workflow run ────────────────
        api.addEventListener("executed", (event) => {
            const detail = event.detail;
            if (!detail?.output) return;

            const nodeId = parseInt(detail.node);
            const node   = app.graph.getNodeById(nodeId);
            if (!node || node.comfyClass !== "ScorpiovImageLoader") return;

            const p = node._scorpiovLoader;
            if (!p) return;

            // ComfyUI sends string outputs as arrays in detail.output
            const filename  = detail.output?.filename?.[0]  ?? "";
            const filePath  = detail.output?.file_path?.[0] ?? "";

            if (filename)  p.filenameField.value = filename;
            if (filePath)  p.pathField.value     = filePath;

            node.setDirtyCanvas(true, true);
        });
    },
});
