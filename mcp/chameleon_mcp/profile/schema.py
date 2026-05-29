"""Profile JSON schema validators.

Phase 1C: minimal validators ensuring structure is well-formed.
Phase 2 expands with full schema enforcement, JSON parser hardening
(Round 4 #14: depth cap 64, duplicate-key rejection, numeric range bounds,
NFC normalization before validation).

See docs/architecture.md "Profile schema" + "Security mitigations" #5 + #14.
"""

from __future__ import annotations

import re
from typing import Any

CURRENT_SCHEMA_VERSION = 7

MAX_JSON_DEPTH = 64

ARCHETYPE_NAME_RE = re.compile(r"\A[a-z][a-z0-9-]{0,63}\Z")


class SchemaError(Exception):
    """Raised when a profile artifact fails schema validation."""


def _check_depth(obj: Any, depth: int = 0) -> None:
    """Recursively check that JSON nesting depth does not exceed MAX_JSON_DEPTH."""
    if depth > MAX_JSON_DEPTH:
        raise SchemaError(f"JSON nesting depth exceeds {MAX_JSON_DEPTH}")
    if isinstance(obj, dict):
        for v in obj.values():
            _check_depth(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _check_depth(item, depth + 1)


def _no_duplicate_keys(pairs: list) -> dict:
    """object_pairs_hook that rejects duplicate keys (Round 4 hardening)."""
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise SchemaError(f"duplicate key in JSON: {key}")
        seen.add(key)
    return dict(pairs)


