"""Convention schema, serialization, and extraction for Smart Injection v0.9.0."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
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
            "inheritance": {},
            "method_calls": {},
            "key_exports": {},
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
# Inheritance frequency extraction
# ---------------------------------------------------------------------------

_INHERITANCE_THRESHOLD = 0.60

# Regex for Ruby class inheritance and include/extend
_RUBY_CLASS_RE = re.compile(r"^\s*class\s+\w+\s*<\s*([\w:]+)", re.MULTILINE)
_RUBY_INCLUDE_RE = re.compile(r"^\s*include\s+([\w:]+)", re.MULTILINE)
# Regex for Ruby DSL calls at class body level (2-space indent)
_RUBY_DSL_CALL_RE = re.compile(
    r"^  (validates|validate|belongs_to|has_many|has_one|has_and_belongs_to_many"
    r"|scope|enum|before_action|after_action|around_action"
    r"|before_validation|after_commit|after_save|before_save|after_create"
    r"|before_create|before_destroy|after_destroy"
    r"|delegate|attr_accessor|attr_reader"
    r"|sidekiq_options|sidekiq_throttle"
    r"|render_data|render_error"
    r"|has_paper_trail|acts_as_taggable_on"
    r"|mount_uploader|has_one_attached|has_many_attached"
    r"|default_scope|counter_culture)\b",
    re.MULTILINE,
)


def extract_inheritance_conventions(files: list[ParsedFile]) -> dict:
    """Detect dominant base class and include mixins by reading file content."""
    if len(files) < MIN_SAMPLE_SIZE:
        return {}

    total = len(files)
    base_counts: Counter[str] = Counter()
    include_counts: Counter[str] = Counter()

    for f in files:
        try:
            content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        seen_bases: set[str] = set()
        for m in _RUBY_CLASS_RE.finditer(content):
            base = m.group(1)
            if base not in seen_bases:
                base_counts[base] += 1
                seen_bases.add(base)
        seen_includes: set[str] = set()
        for m in _RUBY_INCLUDE_RE.finditer(content):
            inc = m.group(1)
            if inc not in seen_includes:
                include_counts[inc] += 1
                seen_includes.add(inc)

    result: dict = {}

    if base_counts:
        top_base, top_count = base_counts.most_common(1)[0]
        if top_count / total >= _INHERITANCE_THRESHOLD:
            result["dominant_base"] = top_base
            result["frequency"] = round(top_count / total, 3)
            result["sample_size"] = total

    if include_counts:
        top_include, inc_count = include_counts.most_common(1)[0]
        if inc_count / total >= _INHERITANCE_THRESHOLD:
            result["dominant_include"] = top_include
            result["include_frequency"] = round(inc_count / total, 3)

    return result


# ---------------------------------------------------------------------------
# Method-call frequency extraction
# ---------------------------------------------------------------------------


_TS_EXPORT_NAME_RE = re.compile(
    r"^\s*export\s+(?:const|let|var|function|class|interface|type|enum)\s+(\w+)",
    re.MULTILINE,
)
_RUBY_CLASS_NAME_RE = re.compile(r"^\s*class\s+(\w+)", re.MULTILINE)
_RUBY_MODULE_NAME_RE = re.compile(r"^\s*module\s+(\w+)", re.MULTILINE)

_MAX_KEY_EXPORTS = 20


def extract_key_exports(files: list[ParsedFile], *, language: str) -> list[str]:
    """Extract the most common exported names across files in an archetype."""
    if len(files) < MIN_SAMPLE_SIZE:
        return []

    name_counts: Counter[str] = Counter()
    for f in files:
        try:
            content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        seen: set[str] = set()
        if language == "typescript":
            for m in _TS_EXPORT_NAME_RE.finditer(content):
                name = m.group(1)
                if name not in seen and len(name) > 1:
                    name_counts[name] += 1
                    seen.add(name)
        elif language == "ruby":
            for m in _RUBY_CLASS_NAME_RE.finditer(content):
                name = m.group(1)
                if name not in seen:
                    name_counts[name] += 1
                    seen.add(name)
            for m in _RUBY_MODULE_NAME_RE.finditer(content):
                name = m.group(1)
                if name not in seen:
                    name_counts[name] += 1
                    seen.add(name)

    # Return top N by frequency, excluding very common names
    skip = {"default", "module", "class", "React", "Component", "ApplicationRecord", "Base"}
    result = []
    for name, _count in name_counts.most_common(_MAX_KEY_EXPORTS + len(skip)):
        if name in skip:
            continue
        result.append(name)
        if len(result) >= _MAX_KEY_EXPORTS:
            break
    return result


def extract_method_call_conventions(files: list[ParsedFile]) -> dict:
    """Extract top DSL/method call patterns by reading file content."""
    if len(files) < MIN_SAMPLE_SIZE:
        return {}

    total = len(files)
    call_counts: Counter[str] = Counter()

    for f in files:
        try:
            content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        seen: set[str] = set()
        for m in _RUBY_DSL_CALL_RE.finditer(content):
            call = m.group(1)
            if call not in seen:
                call_counts[call] += 1
                seen.add(call)

    if not call_counts:
        return {}

    common_top5 = [name for name, _count in call_counts.most_common(5)]
    return {"common_top5": common_top5, "sample_size": total}


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
    for archetype, files in files_by_archetype.items():
        inheritance_conv = extract_inheritance_conventions(files)
        if inheritance_conv:
            conventions["conventions"].setdefault("inheritance", {})[archetype] = inheritance_conv
    for archetype, files in files_by_archetype.items():
        method_conv = extract_method_call_conventions(files)
        if method_conv:
            conventions["conventions"].setdefault("method_calls", {})[archetype] = method_conv
    for archetype, files in files_by_archetype.items():
        language = "typescript"  # default
        if any(str(f.path).endswith(".rb") for f in files[:3]):
            language = "ruby"
        exports = extract_key_exports(files, language=language)
        if exports:
            conventions["conventions"].setdefault("key_exports", {})[archetype] = exports
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

    # Inheritance conventions
    inheritance_lines: list[str] = []
    seen_inheritance: set[str] = set()
    for _arch, data in conv.get("inheritance", {}).items():
        base = data.get("dominant_base")
        if base and base not in seen_inheritance:
            seen_inheritance.add(base)
            freq = data.get("frequency", 0)
            if freq >= _ENFORCE_THRESHOLD:
                inheritance_lines.append(f"- Inherit {base} ({freq:.0%}, enforced)")
            elif freq >= _STRONG_THRESHOLD:
                inheritance_lines.append(f"- Inherit {base} ({freq:.0%})")
        include = data.get("dominant_include")
        if include and include not in seen_inheritance:
            seen_inheritance.add(include)
            inc_freq = data.get("include_frequency", 0)
            if inc_freq >= _STRONG_THRESHOLD:
                inheritance_lines.append(f"- Include {include} ({inc_freq:.0%})")

    # Method call conventions
    method_lines: list[str] = []
    seen_methods: set[str] = set()
    for _arch, data in conv.get("method_calls", {}).items():
        for call in data.get("common_top5", [])[:3]:
            if call not in seen_methods:
                seen_methods.add(call)
    if seen_methods:
        method_lines.append(f"- Common DSL: {', '.join(sorted(seen_methods)[:8])}")

    # Key exports (check before creating)
    export_lines: list[str] = []
    all_exports: set[str] = set()
    for _arch, names in conv.get("key_exports", {}).items():
        for n in names[:5]:
            all_exports.add(n)
    if all_exports:
        sorted_exports = sorted(all_exports)[:15]
        export_lines.append(f"- Check before creating: {', '.join(sorted_exports)}")

    if not import_lines and not naming_lines and not inheritance_lines and not export_lines:
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
    if inheritance_lines:
        lines.append("INHERITANCE:")
        lines.extend(inheritance_lines)
        lines.append("")
    if method_lines:
        lines.append("PATTERNS:")
        lines.extend(method_lines)
        lines.append("")
    if export_lines:
        lines.append("REUSE:")
        lines.extend(export_lines)
        lines.append("")
    lines.append("</chameleon-conventions>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tier 1 PreToolUse convention echo (~30 tokens)
# ---------------------------------------------------------------------------


_SOURCE_EXTENSIONS = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".rb", ".py",
})


def format_directory_listing(file_path: str | None, *, max_files: int = 15) -> str:
    """List sibling files in the same directory, framed as actionable context.

    Returns something like:
    "Nearby: useDebounce.ts, useToggle.ts, useConfig.ts -- check before creating a new file."

    Returns empty string if directory doesn't exist, has 0 siblings, or file_path is None.
    """
    if not file_path:
        return ""
    try:
        parent = Path(file_path).parent
        if not parent.is_dir():
            return ""
        target_name = Path(file_path).name
        siblings = sorted(
            entry.name
            for entry in parent.iterdir()
            if entry.is_file()
            and entry.suffix in _SOURCE_EXTENSIONS
            and entry.name != target_name
        )
    except OSError:
        return ""
    if not siblings:
        return ""
    display = siblings[:max_files]
    return f"Nearby: {', '.join(display)} -- check before creating a new file."


def format_conventions_echo(conventions: dict, *, archetype: str) -> str:
    """Compact one-line convention echo for Tier 1 PreToolUse pointer. ~30 tokens max.

    Tries the specific archetype first. Falls back to the most common
    convention across ALL archetypes so the echo is never empty when
    the repo has conventions (archetype naming can differ between
    clustering and file matching).
    """
    parts: list[str] = []
    conv = conventions.get("conventions", {})

    # Imports: try this archetype, then fall back to any archetype
    arch_imports = conv.get("imports", {}).get(archetype, {})
    if not arch_imports and conv.get("imports"):
        arch_imports = next(iter(conv["imports"].values()), {})
    for c in arch_imports.get("competing", [])[:2]:
        parts.append(f"Imports: {c['preferred']}")
    if not parts:
        top_preferred = arch_imports.get("preferred", [])[:2]
        for p in top_preferred:
            basename = p["module"].rsplit("/", 1)[-1]
            if len(basename) > 2 and basename not in ("index", "types", "utils"):
                parts.append(f"Imports: {p['module']}")
                break

    # Naming: try this archetype, then fall back
    arch_naming = conv.get("naming", {}).get(archetype, {})
    if not arch_naming and conv.get("naming"):
        arch_naming = next(iter(conv["naming"].values()), {})
    for key in ("interface_prefix", "type_prefix"):
        entry = arch_naming.get(key)
        if entry and entry.get("consistency", 0) >= _STRONG_THRESHOLD:
            parts.append(f"Naming: {entry['pattern']}-prefix")
            break

    # Inheritance: try this archetype, then fall back
    arch_inheritance = conv.get("inheritance", {}).get(archetype, {})
    if not arch_inheritance and conv.get("inheritance"):
        arch_inheritance = next(iter(conv["inheritance"].values()), {})
    base = arch_inheritance.get("dominant_base")
    if base and arch_inheritance.get("frequency", 0) >= _STRONG_THRESHOLD:
        parts.append(f"Base: {base}")

    return ". ".join(parts)
