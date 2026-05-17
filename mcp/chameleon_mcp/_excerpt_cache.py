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

CONTEXT_TRANSFORM_VERSION: bump on ANY change to the value-shaping
transform applied between read and cache store — i.e. any change to
chameleon_mcp.sanitization.sanitize_for_chameleon_context OR the 3200
truncation rule in tools.get_pattern_context.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

CONTEXT_TRANSFORM_VERSION = 1

_CAP = 64
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
