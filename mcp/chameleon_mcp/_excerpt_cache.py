"""Process-global LRU memo for the sanitized canonical excerpt.

Lives in the long-lived daemon process (and the MCP stdio server). The
hook in-process fallback runs in a short-lived subprocess where this is
a per-invocation no-op — harmless, no cross-process sharing.

Key: (witness_abs_path: str, witness_st_mtime_ns: int,
      CONTEXT_TRANSFORM_VERSION: int).

The witness mtime is the load-bearing invalidation signal: editing the
witness source file in place changes st_mtime_ns and busts the entry,
which mtime_token (4 profile JSONs only) does NOT detect.

No lock: daemon.serve_forever handles one connection at a time
(daemon.py:386-418). If the daemon ever becomes multi-threaded, wrap
get_or_build in a threading.Lock.

Race-resistant fd-based open: get_pattern_context opens the witness
via safe_open_fd, which calls os.open(path, O_RDONLY | O_NOFOLLOW
| O_CLOEXEC) and fstat()s the returned fd. The cache key uses the
fstat's (st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns) plus the
resolved path and CONTEXT_TRANSFORM_VERSION. The builder reads bytes
from the open fd (not a path-based read_text), so a mid-read dirent
swap is structurally impossible -- the fd points at the inode the
fstat saw. A post-read fstat re-check on the fd catches any size /
mtime / ctime change while we read (truncation, extension, in-place
overwrite). The cache key shape means an attacker who preserves
st_mtime_ns across a swap must ALSO produce a file with the same
st_ino + st_dev + st_size + st_ctime_ns -- in practice impossible
without root, since ctime advances on every metadata operation
(including os.utime) and ino is filesystem-assigned. The residual is
a contrived no-content-change race (same ino, same size, same
mtime, same ctime, but somehow different bytes) which is benign
because the bytes are identical to the cached value.

CONTEXT_TRANSFORM_VERSION: bump on ANY change to the value-shaping
transform applied between read and cache store — i.e. any change to
chameleon_mcp.sanitization.sanitize_for_chameleon_context OR the 3200
truncation rule in tools.get_pattern_context.

CHAMELEON_EXCERPT_CACHE_CAP overrides the 64-entry LRU cap.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Callable

CONTEXT_TRANSFORM_VERSION = 2


def _resolve_cap() -> int:
    """LRU capacity; override via CHAMELEON_EXCERPT_CACHE_CAP (positive
    int). Falls back to 64 on unset/non-int/non-positive."""
    raw = os.environ.get("CHAMELEON_EXCERPT_CACHE_CAP")
    if raw is None:
        return 64
    try:
        val = int(raw)
    except ValueError:
        return 64
    return val if val > 0 else 64


_CAP = _resolve_cap()
_CACHE: OrderedDict[tuple, tuple[str, bool]] = OrderedDict()


def get_or_build(
    key: tuple, build: Callable[[], tuple[str, bool]]
) -> tuple[str, bool]:
    """Return the cached (content, truncated) for `key`, or build, store,
    and return it. LRU: most-recent key moves to the end; oldest evicted
    when over _CAP."""
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    value = build()
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    if len(_CACHE) > _CAP:
        _CACHE.popitem(last=False)
    return value


def clear() -> None:
    """Drop all entries. Used by tests and any future explicit
    invalidation hook."""
    _CACHE.clear()
