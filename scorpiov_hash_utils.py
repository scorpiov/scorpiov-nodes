"""
Scorpiov Hash Utils
--------------------
Shared helper for computing Civitai-style "AutoV2" hashes (the first 10 hex
characters of a file's SHA256 digest) for checkpoints and LoRAs, used to
populate "Model hash:" and "Lora hashes:" in A1111-compatible metadata.

WHY THIS EXISTS
----------------
Civitai links an uploaded image to the exact model/LoRA page using this
short hash, not the filename. Hashing a multi-GB checkpoint on every save
would slow saves down noticeably, so results are cached on disk.

CACHE KEY: file content, not path/mtime
-----------------------------------------
Caching by (path, size, modified-time) is NOT safe: renaming a file often
leaves the OS modified-time untouched, and two different LoRA checkpoints
(e.g. different training steps of the same model) frequently share the
exact same file size. That combination can collide, silently serving a
stale hash for what is now different file content — e.g. deleting
jude.safetensors and renaming judestep2100.safetensors to jude.safetensors
could wrongly keep serving the old file's hash.

Instead, the cache is keyed on a quick content fingerprint: the file size
plus a hash of the first and last few MB. This costs only a few
milliseconds of I/O (not a full read), but changes whenever the actual
bytes change — including same-filename swaps — so a rename with unchanged
content still hits the cache, while a same-name content swap correctly
triggers a fresh real hash.

Cache file lives at: scorpiov-nodes/.scorpiov_hash_cache.json

STORAGE SAFETY
--------------
_save_cache writes through a ".tmp" file and renames it into place
(os.replace), so a crash mid-write can never leave a half-written JSON
behind. And get_autov2_hash re-checks the content fingerprint AFTER the
full hash — a file swapped mid-hash is still returned with its computed
hash, but it is never stored in the cache under the wrong key (a stale
entry there would lie forever, so the pairing is simply not cached).
"""

import os
import json
import hashlib

EXTENSION_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE    = os.path.join(EXTENSION_DIR, ".scorpiov_hash_cache.json")

_SAMPLE_SIZE = 4 * 1024 * 1024  # 4 MB read from the start and end each

_cache = None  # lazy-loaded dict, kept in memory for the process lifetime


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if os.path.isfile(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception as e:
            print(f"[Scorpiov Hash] Could not read cache file, starting fresh: {e}")
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save_cache():
    # Atomic write: build the file under a temp name, then rename it into
    # place. os.replace is atomic on both POSIX and Windows, so a crash
    # or power cut mid-write leaves either the old cache or the new one —
    # never a half-written JSON blob that _load_cache would choke on.
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print(f"[Scorpiov Hash] Could not write cache file: {e}")


def _content_fingerprint(filepath: str, size: int) -> str:
    """
    Cheap content-based cache key: file size + sha1 of the first and last
    _SAMPLE_SIZE bytes. Reads at most 2 * _SAMPLE_SIZE regardless of how
    large the file is, so it stays fast even for multi-GB checkpoints.
    Two files with different content will (for all practical purposes)
    never produce the same fingerprint, even if they share a filename or
    an OS-reported modified time.
    """
    fp = hashlib.sha1()
    fp.update(str(size).encode())
    with open(filepath, "rb") as f:
        fp.update(f.read(_SAMPLE_SIZE))
        if size > _SAMPLE_SIZE:
            f.seek(max(0, size - _SAMPLE_SIZE))
            fp.update(f.read(_SAMPLE_SIZE))
    return fp.hexdigest()


def get_autov2_hash(filepath: str) -> str:
    """
    Returns the first 10 hex characters of the file's SHA256 digest
    (Civitai's "AutoV2" hash format). Returns "" if the file doesn't exist
    or can't be read. Cached by content fingerprint — a file only pays the
    full-hash cost once per distinct set of bytes, regardless of filename
    or path.
    """
    if not filepath or not os.path.isfile(filepath):
        return ""

    try:
        size = os.path.getsize(filepath)
        key  = _content_fingerprint(filepath, size)
    except OSError as e:
        print(f"[Scorpiov Hash] Could not read file '{filepath}': {e}")
        return ""

    cache = _load_cache()

    if key in cache:
        return cache[key]["hash"]

    print(f"[Scorpiov Hash] Hashing (new content, will be cached): {os.path.basename(filepath)}")
    sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                sha256.update(chunk)
    except OSError as e:
        print(f"[Scorpiov Hash] Could not read file '{filepath}': {e}")
        return ""

    autov2 = sha256.hexdigest()[:10]

    # Re-check the content fingerprint AFTER the (slow) full hash: if the
    # file was swapped or rewritten mid-read, the hash we computed
    # describes different bytes than the cache key — never store that
    # pairing, or the cache would serve a wrong hash for this fingerprint
    # forever. The computed hash is still returned; it's just not cached.
    try:
        size_after = os.path.getsize(filepath)
        key_after = _content_fingerprint(filepath, size_after)
    except OSError:
        key_after = None

    if key_after == key:
        # Store the path alongside the hash purely for human debugging when
        # reading the cache file — lookups never use it.
        cache[key] = {"hash": autov2, "last_seen_path": filepath}
        _save_cache()
    else:
        print(f"[Scorpiov Hash] WARNING: '{os.path.basename(filepath)}' "
              f"changed during hashing — hash computed but NOT cached.")

    print(f"[Scorpiov Hash] {os.path.basename(filepath)} -> {autov2}")
    return autov2
