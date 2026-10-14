# Scorpiov-Nodes fix pass — Oct 14, 2026

Full-pack fix pass following the Oct 14 code review (26 findings in
`~/workspace/scorpiov-review/REVIEW.md`). Status: built and unit-tested
on the VM; **not yet deployed to the PC or pushed to GitHub** — both are
gated on live testing in ComfyUI.

Verification so far: all Python files compile; all 9 JS files pass
`node --check`; 53 unit tests pass (parsers, wildcard engine, save path,
end-to-end RGBA save with real model hash); a full-package import test
with stubbed ComfyUI modules registers all 11 node types, and the new
`__init__` fault isolation was demonstrated live (one deliberately
broken module → the other 10 still load, with the failure named).

## New file
- **scorpiov_common.py** — the pack's parsers (LoRA tag regex, node-type
  sets, name helpers) existed in 3–4 drifted copies; they now live in one
  place and every parser imports them from here.

## scorpiov_image_meta.py
- `_resolve_path`: `get_input_directory()` returns ONE string, not a
  list — bare-filename lookup could never match. Now searches input,
  output and temp folders + annotated paths.
- Prompt source: parses BOTH PNG chunks when present and prefers the
  A1111 `parameters` text (the executed prompt) over the ComfyUI graph
  (the as-typed template) — this is why loaded images showed the wrong
  prompt, especially wildcard-template prompts.
- Sampler following now routes through the general text resolver, so
  workflows whose positive comes from the Wildcard Processor (or other
  conditioning nodes) resolve instead of returning blank.
- Defensive parsing: link-valued LoRA weights, non-dict graphs and bad
  JSON degrade to partial results instead of crashing the node.
- `_read_png_meta`: context manager (no more locked files on Windows),
  full decode only when the header read finds no metadata keys.
- `IS_CHANGED` fingerprints the file (mtime+size) — re-saving over the
  same filename no longer serves stale cached metadata.

## scorpiov_wildcard.py
- Random mode uses a private `random.Random(seed)` — no longer reseeds
  Python's global RNG for the whole ComfyUI process. Seed widget gains
  the frontend randomize control.
- Serial mode: stored index is clamped modulo the current file length
  (editing a file shorter no longer crashes); `serial_start_line` is
  honoured when it *changes* while leaving the Batch Runner's
  reset-once/auto-increment flow intact.
- LoRA handling: index built once and cached (was a full disk walk per
  generation); stem collisions now warn in the console; duplicate tags
  are applied once, not twice; `<LoRA:...>` any-case tags and
  `<lora:name:w:clip_w>` now parse and apply separate clip weights.
- Wildcard files are cached (mtime+size) instead of re-read per pick;
  read as utf-8-sig so a Windows BOM can't corrupt line 1.
- Comment rules unified (header, code and validator now agree) and `\#`
  escapes a literal `#` (hex colours etc.).
- Expansion guard: resolved-text size ceiling turns runaway recursive
  wildcard files into a clear error instead of a memory blow-up.
- Index build warns about filenames that `__token__` syntax can never
  reference (spaces/dots/accents). Refresh endpoint tolerates an empty
  body and also invalidates the LoRA index; line_count answers JSON
  errors instead of an HTML 500.

## scorpiov_save_image.py
- Real metadata hashes: model hash is computed from the actual
  checkpoint/UNET file (via scorpiov_hash_utils, disk-cached); LoRA
  hashes are written only when the Lora Tag Loader supplied a real one.
  The fabricated `0000000000` hashes and hardcoded `Version: v1.9.0`
  are gone.
- UNETLoader (Flux/Qwen) workflows now record the model (was blank);
  LoRAs are auto-extracted from LoraLoader nodes (the tooltip always
  promised this); sampler fields fill per-field from any sampler in the
  graph, with SamplerCustom chains followed via KSamplerSelect /
  BasicScheduler / noise links.
- Filename safety: `:` and control characters sanitised (Windows ADS /
  failed saves), reserved device names (CON, NUL, COM1…) guarded,
  segment length capped. New prefix tokens `%seed% %model% %width%
  %height%`. Batch images share one prefix with sequential counters
  (folder scanned once per save call, not once per image).
- CFG is no longer rounded to 1 decimal in the recorded metadata.
- 2-channel (LA) tensors save correctly instead of raising; PNG chunk
  serialisation failures skip the chunk, never the image; duplicate
  chunks are never written. The Qwen RGBA channel fix is preserved.

## Secondary Python nodes
- **lora_tag_loader**: uses scorpiov_common parsing; LoRA index cached
  with collision warnings; duplicate tags deduped. Its real-hash
  `loras_info` output format is unchanged (Save now honours the hashes).
- **image_loader**: input listing is recursive (subfolders appear in the
  dropdown); file opened with a context manager; LA / palette+alpha
  images flatten onto white like RGBA; IS_CHANGED hashes in 8 MB chunks.
- **width_height**: in 16-channel mode the node now snaps dimensions
  down to the 16-pixel grid those models pack to, with a console note —
  a 1000x832 request on Qwen 2.1 comes out 992x832 (core's own latent
  node does the identical thing; it's the model's packing, verified
  live), so the node's reported width/height now match reality instead
  of silently promising 1000. The empty latent also carries core's
  `downscale_ratio_spacial: 8` hint — without it, Qwen-Image 2.1's
  sampler/VAE path assumes packed /16 geometry and an 832x832 request
  silently decodes to 1664x1664 (found in live testing, Oct 14).
  Malformed ratios.txt can no longer break node
  registration; upscaled dimensions clamp at 8192 with a warning (OOM
  guard); new `latent_channels` input (appended last) — 4 (SD/SDXL) or
  16 (Flux/SD3/Qwen) channel empty latents; IS_CHANGED no longer relies
  on per-process-randomised `hash()`.
- **prompt_combiner**: conditioning merge pads to the LONGEST input
  instead of truncating everything to the first input's length; config
  weights parse defensively; malformed entries and shape mismatches
  degrade with a warning instead of crashing the queue.
- **multi_checkpoint_loader**: non-dict slot entries raise a clear
  error; default tag is the checkpoint's clean base name (no path or
  extension leaking into filenames).
- **hash_utils**: cache file written atomically (temp + os.replace);
  a file swapped mid-hash can no longer poison the cache.

## JS frontend
- **wildcard**: preview restyled to the pack's shared palette (was neon
  green on navy); the 500px+ auto-grow that ballooned the Prompter after
  every run is removed — fixed-height scrollable preview, node size is
  the user's; header gains a character count and a Copy button; dead
  `wildcard_folder` field removed from the refresh call.
- **image_meta**: same palette; forced 540px minimum and auto-grow
  removed (fixed-height panel).
- **anywhere**: the "broadcasting" flag on ordinary nodes now lives in
  node.properties, so it survives workflow save/reload (it silently
  didn't before); dynamic slot naming can no longer collide after a
  middle slot is removed.
- **prompt_combiner**: slot naming collision fix; unplugging a wire no
  longer deletes that slot's weight/enabled settings (only actual slot
  removal does).
- **multi_checkpoint_loader**: checkpoint list refreshes on a 30 s TTL
  instead of being cached for the whole session.
- **batch_runner**: an empty checkpoint tag can no longer put
  "undefined" into filename_prefix (falls back to a name derived from
  the checkpoint filename).
- **save_image / image_loader JS**: palette aligned; save_image's stale
  header comment (a `model_hash` widget that never existed) corrected.

## __init__.py
- Each module imports in isolation with a clear console report — one
  broken file can no longer take the whole pack out of the node menu.

## Also prepared (repo hygiene, for the GitHub step)
- `.gitattributes` pinning LF for code files, so Windows CRLF copies
  stop producing phantom whole-file diffs.
- Open decision for Vik: the repo's `wildcards/` folder holds only the
  5 starter files — fine as samples, but GitHub is not a backup of the
  local wildcard library. Unchanged in this pass.
