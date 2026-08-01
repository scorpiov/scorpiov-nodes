# scorpiov-nodes
<img width="3523" height="1477" alt="workflow(1)" src="https://github.com/user-attachments/assets/1a45cb30-7633-44a3-a65e-d0c8af0acdbd" />


A collection of custom ComfyUI nodes I use in my own workflows — wildcard prompting, LoRA tag loading, and Civitai-ready image saving with automatic metadata.

## Installation

1. Download or clone this repo into `ComfyUI/custom_nodes/scorpiov-nodes/`
2. Restart ComfyUI

No extra Python dependencies beyond what ComfyUI already ships with.

## Nodes

| Node | Category | What it does |
|---|---|---|
| Wildcard Processor 🎲 | Scorpiov/Prompt | Resolves wildcards + inline `{a\|b\|c}` groups, loads `<lora:...>` tags, outputs CONDITIONING |
| Wildcard Prompter 📝 | Scorpiov/Prompt | Text-only version — no model/clip needed, just resolves wildcards to a string |
| Lora Tag Loader 🏷️ | Scorpiov/Loaders | Reads `<lora:name:weight>` tags out of any text, loads them onto model/clip, strips the tags |
| Width Height | Scorpiov/Latent | Aspect ratio picker for empty latents |
| Image Meta Reader 🔍 | Scorpiov/Image | Reads A1111 and ComfyUI-native PNG metadata back out of an image |
| Image Loader 🖼️ | Scorpiov/Image | Loads an image, also outputs its filename and full path |
| Save Image 💾 | Scorpiov/Image | Saves images with full Civitai-compatible metadata, auto-detected from your workflow |

---

### Wildcard Processor 🎲
<img width="1050" height="728" alt="Screenshot 2026-07-31 214155" src="https://github.com/user-attachments/assets/0c4b9fa2-a349-40a1-811e-403151acd593" />

All-in-one wildcard processor with LoRA loading, serial/random modes, and prompt preview.
- Put your wildcard `.txt` files into the `wildcards/` folder in the node folder — any subfolder depth, just reference by filename with `__filename__` (no path needed)
- Supports inline `{option_a|option_b|option_c}` groups, including nested
- Random or Serial selection mode; Serial remembers position and loops
- Parses and loads `<lora:name:weight>` tags found in the resolved text directly onto the model/clip
- Outputs CONDITIONING, MODEL, CLIP, and the resolved STRING
- **Comments** are supported both in wildcard `.txt` files and typed directly into the node's own text box — see [Comments](#comments) below
- A live **🎨 Highlighted Preview** box sits under the text field, color-coding `{ }` groups, `( )` weights, `:weight` numbers, and comments as you type — read-only, just a visual aid

### Wildcard Prompter 📝
<img width="806" height="846" alt="workflow(2)" src="https://github.com/user-attachments/assets/8f7232b8-7637-40d5-8466-4124f2429a1d" />

Text-only variant of the Wildcard Processor — no `model`/`clip` inputs, no LoRA loading. Just resolves wildcards and outputs the string, so you can feed it into a Lora Tag Loader or CLIPTextEncode. Useful when you want to build your prompt text before deciding what to do with it. Same comment support and Highlighted Preview as the Wildcard Processor.

#### Comments

Both wildcard nodes, and the `.txt` files they load from `wildcards/`, support two comment styles:

- `# comment` — everything from a `#` to the end of that line is dropped. Works on its own line, or trailing after real text (`blonde hair # my favorite`).
- `/* comment */` — block comments, C-style. Can span multiple lines, and the markers don't need to sit alone on their own line — `/* this` on one line and `is a comment */` two lines later works exactly like you'd expect from any C-style comment.

Comments are stripped before wildcards or `{a|b|c}` groups are resolved, so they never reach the resolved prompt, Save Image, or embedded PNG metadata.

### Lora Tag Loader 🏷️
<img width="1260" height="800" alt="Screenshot 2026-07-31 211411" src="https://github.com/user-attachments/assets/203cd9fb-9688-419d-9d63-e4c828ceef19" />

Reads `<lora:name:weight>` or `<lora:name:weight:clip_weight>` tags out of a text prompt (e.g. from Wildcard Prompter), loads each named LoRA onto the given model/clip, and strips the tags out of the text so it's safe to feed into a standard `CLIPTextEncode` node.

- `<lora:name:0.8>` → same weight applied to both the model and CLIP
- `<lora:name:0.8:0.6>` → separate model weight (0.8) and CLIP weight (0.6), matching how [comfyui_lora_tag_loader](https://github.com/badjeff/comfyui_lora_tag_loader) handles it
- Searches every folder registered under ComfyUI's `loras` path, matched by filename — subfolders in your LoRA library "just work," and a subfolder prefix inside the tag itself (e.g. `<lora:MyFolder\mylora:0.8>`) is handled too
- Outputs: `model`, `clip`, `text` (tags stripped, ready for CLIPTextEncode), and `loras_info` — a formatted string with each LoRA's name, weight, and content hash, meant to be wired straight into **Save Image**'s `loras` input for Civitai-compatible metadata

Typical chain: `Wildcard Prompter` → `Lora Tag Loader` → two `CLIPTextEncode` nodes (positive/negative) → `KSampler`, with `loras_info` also wired to `Save Image`.

### Width Height
<img width="1189" height="646" alt="workflow(4)" src="https://github.com/user-attachments/assets/3a13e3a2-5feb-4fe0-80fa-2d6ac39b7713" />

Aspect Ratio Node, set width/height for empty latents or choose from a list
- Add to the list by updating the `ratios.txt` file in the node folder
  - Eg: `My Custom Ratio (2:1) | 2048 | 1024`

### Image Meta Reader 🔍
<img width="2213" height="1326" alt="workflow(5)" src="https://github.com/user-attachments/assets/b919038d-1ce0-4813-a9ca-802f8687d917" />

Reads PNG metadata in both A1111 and ComfyUI-native JSON formats — model, VAE, LoRAs, prompts, and the full raw metadata — into a single scrollable textarea. Has a "Read Metadata Now" button so you can inspect an image without running the whole workflow.

### Image Loader 🖼️
<img width="1808" height="786" alt="Screenshot 2026-07-19 132754" src="https://github.com/user-attachments/assets/f9f223ce-6997-4b8c-88c6-625b38b445a1" />

Loads an image and outputs the tensor, the bare filename, and the full file path — handy when a downstream node needs to know exactly which file it's working with.

### Save Image 💾
<img width="581" height="798" alt="Screenshot 2026-07-31 211015" src="https://github.com/user-attachments/assets/83ec6dc6-711c-4b0d-b6c5-1fe399c08f52" />

Saves images with full A1111/Civitai-compatible metadata embedded in the PNG.

- **Required wires:** `images`, `positive_prompt`, `negative_prompt`
- **`loras`** is also a wire-only input (no typed-text fallback) — connect it from Lora Tag Loader's `loras_info` output, or Image Meta Reader's `loras` output. Leave it unconnected to skip LoRA metadata entirely.
- **Everything else is auto-detected** from your workflow graph — steps, CFG, sampler, scheduler, seed, checkpoint name, and VAE name are all read automatically. Typing a value into any of those fields overrides the auto-detected one.
- **⚙ Advanced Settings** collapses `model_name`, `vae_name`, `steps`, `cfg`, `sampler_name`, and `scheduler` behind a toggle button, collapsed by default, since these usually only matter when overriding auto-detection. `seed`, `control_after_generate`, `save_metadata`, and `model_hash` stay visible.
- **LoRA + Civitai badges:** wire `Lora Tag Loader`'s `loras_info` output into the `loras` input, and Save Image will embed the `<lora:name:weight>` tag and a proper `Lora hashes:` line so Civitai can recognize and badge the LoRAs used.
- **Model hash:** the checkpoint file is automatically hashed (SHA256 → Civitai's "AutoV2" short hash) and embedded as `Model hash:`, same as the `model_hash` field can be typed manually to override.
- **Hashing is cached**, keyed by file content (not filename or path), so a given checkpoint or LoRA is only ever hashed once — after that, saves reuse the cached hash instantly. The cache lives at `scorpiov-nodes/.scorpiov_hash_cache.json`; delete it any time to force a clean re-hash of everything.
- `filename_prefix` supports subfolders via `/` and date tokens like `%date:yyyy-MM-dd%`

---

## Notes

- All LoRA-aware nodes search every folder registered under ComfyUI's `loras` path — you don't need to configure anything, just drop LoRA files anywhere under your usual loras directory (subfolders included).
- Metadata written by Save Image is readable by Automatic1111, Civitai, and this package's own Image Meta Reader.
- Built and tested against ComfyUI's newer Vue-based frontend. If you're on the older classic frontend, the JS-side UI extras (Highlighted Preview, Advanced Settings toggle) should still work, but haven't been explicitly tested there.
