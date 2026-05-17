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

Bounded TOCTOU window: get_pattern_context calls safe_open() (which
lstat-refuses symlinks, size-caps, and repo-boundary-checks at check
time and returns the resolved real path), then stat()s and later
read_text()s that resolved path inside the cache builder. The builder
re-stat()s after the read and raises OSError if mtime advanced --
caught by get_pattern_context's existing OSError handler, which fails
open to an empty canonical_excerpt and stores nothing. A normal
writer (editor save, refresh write) that advances mtime is detected.
The residual window is an adversarial writer that preserves mtime
across the swap (e.g. os.utime back to the prior value) -- bounded
by the witness path coming from a committed, trust-gated profile,
sanitization running on every cache miss, and the advisory nature of
the output. Fully closing that residual would require an
O_NOFOLLOW-opened fd whose fstat is trusted instead of a path-based
stat; that is out of scope for this cache.

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

CONTEXT_TRANSFORM_VERSION = 1


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
