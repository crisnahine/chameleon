"""Profile JSON schema validators.

Phase 1C: minimal validators ensuring structure is well-formed.
Phase 2 expands with full schema enforcement, JSON parser hardening
(Round 4 #14: depth cap 64, duplicate-key rejection, numeric range bounds,
NFC normalization before validation).

See ARCHITECTURE.md "Profile schema" + "Security mitigations" #5 + #14.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Schema version supported by this engine version.
# v0.4 (4.6) bumped from 5 → 6: repo_id derivation now prefers the git
# remote URL when available. v5 profiles are still readable by v0.4
# engines (the loader's engine_min_version check guards forward compat),
# but v0.3 engines cannot read v6.
# v0.5.2 bumped from 6 → 7: paths_pattern strings now carry a file
# extension suffix (e.g. "src/components:tsx") and preserve the
# workspace name on monorepo paths. v6 profiles are still readable;
# get_archetype reads both the v6 (extension-blind) and v7
# (extension-aware) bucket variants so v0.5.x profiles keep matching.
CURRENT_SCHEMA_VERSION = 7
SUPPORTED_SCHEMA_RANGE = (CURRENT_SCHEMA_VERSION - 2, CURRENT_SCHEMA_VERSION)

# Maximum JSON nesting depth (Round 4 hardening)
MAX_JSON_DEPTH = 64

# Archetype name pattern: lowercase letters/digits/hyphens, 1-64 chars
ARCHETYPE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


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


def load_profile_json(content: str) -> dict:
    """Parse and minimally validate a profile.json string.

    Performs Round 4 hardening:
    - Depth cap (64)
    - Duplicate-key rejection
    - Schema-version range check
    """
    parsed = json.loads(content, object_pairs_hook=_no_duplicate_keys)
    _check_depth(parsed)

    if not isinstance(parsed, dict):
        raise SchemaError("profile.json root must be a JSON object")

    schema_version = parsed.get("schema_version")
    if not isinstance(schema_version, int):
        raise SchemaError("schema_version is required and must be an integer")
    if schema_version < SUPPORTED_SCHEMA_RANGE[0] or schema_version > SUPPORTED_SCHEMA_RANGE[1]:
        raise SchemaError(
            f"schema_version {schema_version} outside supported range "
            f"{SUPPORTED_SCHEMA_RANGE}; migration needed"
        )

    return parsed


def validate_archetype_name(name: str) -> None:
    """Raise SchemaError if name does not match archetype name pattern."""
    if not isinstance(name, str):
        raise SchemaError(f"archetype name must be string, got {type(name)}")
    if not ARCHETYPE_NAME_RE.match(name):
        raise SchemaError(
            f"archetype name {name!r} must match {ARCHETYPE_NAME_RE.pattern}"
        )


# Production profile-directory loading lives in `loader.py` (double-fstat +
# generation consistency + engine_min_version enforcement). This module
# owns the schema primitives (SchemaError, load_profile_json,
# validate_archetype_name) used by both the loader and the tests.
