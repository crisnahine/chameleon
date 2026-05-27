"""Convention schema, serialization, and extraction for Smart Injection v0.9.0."""
from __future__ import annotations
import json
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.extractors._base import ParsedFile

CONVENTIONS_SCHEMA_VERSION = 1
MIN_SAMPLE_SIZE = 10
MIN_SAMPLE_SIZE_NAMING = 5


def empty_conventions(*, generation: int) -> dict:
    return {
        "schema_version": CONVENTIONS_SCHEMA_VERSION,
        "generation": generation,
        "min_sample_size": MIN_SAMPLE_SIZE,
        "conventions": {
            "imports": {},
            "naming": {},
        },
    }


def serialize_conventions(conventions: dict) -> str:
    return json.dumps(conventions, indent=2, sort_keys=False, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Import frequency extraction
# ---------------------------------------------------------------------------

_FRAMEWORK_THRESHOLD = 0.80
_MIN_PREFERRED_COUNT = 10
_MIN_COMPETING_COUNT = 5

# Modules that are framework-mandatory when ubiquitous (above _FRAMEWORK_THRESHOLD).
# Non-framework modules above the threshold are strong team conventions and stay.
_FRAMEWORK_MODULES = frozenset({
    "react", "react-dom", "vue", "svelte", "next", "nuxt",
    "@angular/core", "@angular/common",
    "solid-js", "preact",
})


def extract_import_conventions(
    files: list[ParsedFile],
    *,
    competing_pairs: list[tuple[str, str]] | None = None,
) -> dict:
    """Extract import conventions from a cluster of ParsedFile objects.

    Returns {"preferred": [...], "competing": [...]}.
    - preferred: modules imported frequently but not ubiquitously (framework noise).
    - competing: pairs where a wrapper dominates and the raw import is rare/absent.
    """
    if len(files) < MIN_SAMPLE_SIZE:
        return {"preferred": [], "competing": []}

    total = len(files)
    module_counts: Counter[str] = Counter()
    for f in files:
        seen_in_file: set[str] = set()
        for module, _kind in f.import_specifiers:
            if module not in seen_in_file:
                module_counts[module] += 1
                seen_in_file.add(module)

    # --- competing pairs ---
    competing: list[dict] = []
    if competing_pairs:
        for preferred_mod, over_mod in competing_pairs:
            p_count = module_counts.get(preferred_mod, 0)
            o_count = module_counts.get(over_mod, 0)
            if p_count >= _MIN_COMPETING_COUNT and o_count <= 2:
                competing.append({
                    "preferred": preferred_mod, "over": over_mod,
                    "preferred_count": p_count, "over_count": o_count,
                })
    else:
        sorted_modules = sorted(module_counts.keys())
        for i, mod_a in enumerate(sorted_modules):
            for mod_b in sorted_modules[i + 1:]:
                if _is_wrapper_pair(mod_a, mod_b):
                    a_count = module_counts[mod_a]
                    b_count = module_counts[mod_b]
                    if a_count >= _MIN_COMPETING_COUNT and b_count <= 2:
                        competing.append({"preferred": mod_a, "over": mod_b, "preferred_count": a_count, "over_count": b_count})
                    elif b_count >= _MIN_COMPETING_COUNT and a_count <= 2:
                        competing.append({"preferred": mod_b, "over": mod_a, "preferred_count": b_count, "over_count": a_count})

    # --- preferred imports (frequent but not framework-mandatory) ---
    preferred: list[dict] = []
    for module, count in module_counts.most_common():
        if count / total > _FRAMEWORK_THRESHOLD and module in _FRAMEWORK_MODULES:
            continue
        if count < _MIN_PREFERRED_COUNT:
            continue
        preferred.append({"module": module, "source": module, "frequency": count, "total": total})

    return {"preferred": preferred, "competing": competing}


def _is_wrapper_pair(a: str, b: str) -> bool:
    """Heuristic: two modules are a wrapper pair if one base name contains the other."""
    base_a = a.rsplit("/", 1)[-1]
    base_b = b.rsplit("/", 1)[-1]
    return len(base_a) > 3 and len(base_b) > 3 and (base_a in base_b or base_b in base_a) and base_a != base_b


# ---------------------------------------------------------------------------
# Naming convention extraction
# ---------------------------------------------------------------------------

_PREFIX_RE = re.compile(r"^([A-Z])[A-Z]")
_ENFORCE_THRESHOLD = 0.95
_STRONG_THRESHOLD = 0.60


def extract_naming_conventions(*, declarations: dict[str, list[str]]) -> dict:
    """Detect prefix conventions (I-prefix, T-prefix, E-prefix) from declaration names.

    ``declarations`` maps a declaration type ("interface", "type", "enum") to a
    list of identifier names found in the codebase.  Returns a dict keyed by
    ``<type>_prefix`` with pattern, consistency, and sample_size when a
    dominant single-letter prefix is detected above ``_STRONG_THRESHOLD``.
    """
    result: dict = {}
    type_to_key = {"interface": "interface_prefix", "type": "type_prefix", "enum": "enum_prefix"}
    for decl_type, names in declarations.items():
        if len(names) < MIN_SAMPLE_SIZE_NAMING:
            continue
        key = type_to_key.get(decl_type)
        if not key:
            continue
        prefix_counts: Counter[str] = Counter()
        for name in names:
            m = _PREFIX_RE.match(name)
            if m:
                prefix_counts[m.group(1)] += 1
        if not prefix_counts:
            continue
        most_common_prefix, count = prefix_counts.most_common(1)[0]
        consistency = count / len(names)
        if consistency >= _STRONG_THRESHOLD:
            result[key] = {
                "pattern": most_common_prefix,
                "consistency": round(consistency, 3),
                "sample_size": len(names),
            }
    return result
