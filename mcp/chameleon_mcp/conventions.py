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
    # Auto-detection of competing pairs disabled: substring heuristics
    # produce too many false positives on real codebases (Button/UnlockButton,
    # Chart/LineChart). Competing pairs will be detected in v0.9.1 via
    # source-file import analysis (check if module A's file imports module B).

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
    """Heuristic: two modules are a wrapper pair if one is a prefixed version of the other.

    Matches: useCustomQuery wraps useQuery (useQuery is a suffix of useCustomQuery).
    Rejects: ~/components/Button vs ../../../../Grid/UnlockButton (different basenames).
    Rejects: Chart vs LineChart (different components, not a wrapper relationship).

    The key insight: a wrapper re-exports or extends the wrapped module, so the
    wrapper's basename must END with the wrapped module's basename. Plain substring
    containment (Button in UnlockButton) produces massive false positives on real
    codebases.
    """
    base_a = a.rsplit("/", 1)[-1]
    base_b = b.rsplit("/", 1)[-1]
    if len(base_a) < 4 or len(base_b) < 4 or base_a == base_b:
        return False
    # Wrapper must end with the wrapped name (useCustomQuery ends with Query = useQuery's base)
    # AND be strictly longer (not equal)
    if base_a.endswith(base_b) and len(base_a) > len(base_b):
        return True
    if base_b.endswith(base_a) and len(base_b) > len(base_a):
        return True
    return False


# ---------------------------------------------------------------------------
# Declaration name extraction (for naming conventions)
# ---------------------------------------------------------------------------

_TS_INTERFACE_NAME_RE = re.compile(r"^\s*(?:export\s+)?interface\s+([A-Z]\w*)", re.MULTILINE)
_TS_TYPE_NAME_RE = re.compile(r"^\s*(?:export\s+)?type\s+([A-Z]\w*)\s*[=<]", re.MULTILINE)
_TS_ENUM_NAME_RE = re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Z]\w*)", re.MULTILINE)


def extract_declarations_from_content(
    content: str, *, language: str
) -> dict[str, list[str]]:
    """Extract interface/type/enum declaration names from file content.

    Returns {"interface": ["IFoo", "IBar"], "type": ["TBaz"], "enum": ["EQux"]}.
    Only TypeScript is supported; Ruby returns empty dict.
    """
    if language != "typescript":
        return {}
    result: dict[str, list[str]] = {}
    interfaces = _TS_INTERFACE_NAME_RE.findall(content)
    if interfaces:
        result["interface"] = interfaces
    types = _TS_TYPE_NAME_RE.findall(content)
    if types:
        result["type"] = types
    enums = _TS_ENUM_NAME_RE.findall(content)
    if enums:
        result["enum"] = enums
    return result


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


# ---------------------------------------------------------------------------
# Aggregate extraction (used by bootstrap orchestrator)
# ---------------------------------------------------------------------------


def extract_all_conventions(
    *,
    files_by_archetype: dict[str, list[ParsedFile]],
    declarations_by_archetype: dict[str, dict[str, list[str]]],
    generation: int,
) -> dict:
    """Extract import and naming conventions for each archetype.

    Called by the bootstrap orchestrator after clustering.  Returns a
    full conventions dict ready for ``serialize_conventions`` and
    writing to ``conventions.json``.
    """
    conventions = empty_conventions(generation=generation)
    for archetype, files in files_by_archetype.items():
        import_conv = extract_import_conventions(files)
        if import_conv["preferred"] or import_conv["competing"]:
            conventions["conventions"]["imports"][archetype] = import_conv
    for archetype, declarations in declarations_by_archetype.items():
        naming_conv = extract_naming_conventions(declarations=declarations)
        if naming_conv:
            conventions["conventions"]["naming"][archetype] = naming_conv
    return conventions


# ---------------------------------------------------------------------------
# SessionStart formatting (v0.9.0)
# ---------------------------------------------------------------------------


def format_conventions_for_session(conventions: dict) -> str:
    """Format conventions for SessionStart injection.

    Imperative framing for >=95% consistency, context for 60-95%.
    Skip anything below 60%.
    """
    lines: list[str] = []
    conv = conventions.get("conventions", {})

    import_lines: list[str] = []
    seen_competing: set[str] = set()
    for _arch, data in conv.get("imports", {}).items():
        for c in data.get("competing", []):
            key = f"{c['preferred']}>{c['over']}"
            if key not in seen_competing:
                seen_competing.add(key)
                import_lines.append(f"- Use {c['preferred']}, not {c['over']}")

    # Surface top preferred imports (high-frequency, cross-archetype)
    seen_preferred: set[str] = set()
    all_preferred: list[tuple[int, str]] = []
    for _arch, data in conv.get("imports", {}).items():
        for p in data.get("preferred", []):
            mod = p["module"]
            if mod not in seen_preferred:
                seen_preferred.add(mod)
                all_preferred.append((p["frequency"], mod))
    all_preferred.sort(reverse=True)
    for _freq, mod in all_preferred[:10]:
        basename = mod.rsplit("/", 1)[-1]
        if len(basename) > 2 and basename not in ("index", "types", "utils"):
            import_lines.append(f"- Prefer {mod}")

    naming_lines: list[str] = []
    seen_naming: set[str] = set()
    for _arch, data in conv.get("naming", {}).items():
        for key in ("interface_prefix", "type_prefix", "enum_prefix"):
            entry = data.get(key)
            if not entry or key in seen_naming:
                continue
            consistency = entry.get("consistency", 0)
            if consistency < _STRONG_THRESHOLD:
                continue
            seen_naming.add(key)
            type_name = key.replace("_prefix", "").replace("_", " ")
            pattern = entry["pattern"]
            pct = f"{consistency:.0%}"
            if consistency >= _ENFORCE_THRESHOLD:
                naming_lines.append(f"- Prefix {type_name}s with {pattern} ({pct}, enforced)")
            else:
                naming_lines.append(f"- Prefix {type_name}s with {pattern} ({pct})")

    if not import_lines and not naming_lines:
        return ""

    lines.append("<chameleon-conventions>")
    lines.append("Follow these on every edit. Auto-derived from this codebase.")
    lines.append("")
    if import_lines:
        lines.append("IMPORTS (enforce):")
        lines.extend(import_lines)
        lines.append("")
    if naming_lines:
        lines.append("NAMING:")
        lines.extend(naming_lines)
        lines.append("")
    lines.append("</chameleon-conventions>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tier 1 PreToolUse convention echo (~30 tokens)
# ---------------------------------------------------------------------------


def format_conventions_echo(conventions: dict, *, archetype: str) -> str:
    """Compact one-line convention echo for Tier 1 PreToolUse pointer. ~30 tokens max."""
    parts: list[str] = []
    conv = conventions.get("conventions", {})

    arch_imports = conv.get("imports", {}).get(archetype, {})
    for c in arch_imports.get("competing", [])[:2]:
        parts.append(f"Imports: {c['preferred']}")
    if not parts:
        top_preferred = arch_imports.get("preferred", [])[:2]
        for p in top_preferred:
            basename = p["module"].rsplit("/", 1)[-1]
            if len(basename) > 2 and basename not in ("index", "types", "utils"):
                parts.append(f"Imports: {p['module']}")
                break

    arch_naming = conv.get("naming", {}).get(archetype, {})
    for key in ("interface_prefix", "type_prefix"):
        entry = arch_naming.get(key)
        if entry and entry.get("consistency", 0) >= _STRONG_THRESHOLD:
            parts.append(f"Naming: {entry['pattern']}-prefix")
            break

    return ". ".join(parts)
