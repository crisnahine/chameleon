"""Tool config reading — `.prettierrc`, `tsconfig.json`, `.eslintrc*`, `.editorconfig`.

Per ARCHITECTURE.md "Bootstrap interview flow" step (c) + "Tracked dimensions
catalog" Tier 1 dimensions 30-34: tool configs are GROUND TRUTH for structural
rules (formatting, linting, type-check strictness). Statistical analysis defers
to these.

Phase 4.7 adds tsconfig `extends`-chain resolution (relative paths and bare
`@tsconfig/*` specifiers via node_modules), with cycle detection capped at
``_MAX_EXTENDS_HOPS`` hops.

Phase 2C.4 adds `.eslintrc.yml` / `.eslintrc.yaml` parsing via PyYAML and a
best-effort regex extraction for `.eslintrc.js` / `.cjs` / `.mjs`. Both
parsers fall back to the legacy "invisible" warning if parsing fails and
record a human-readable note under ``ToolConfigResult.parse_warnings``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyYAML ships via detect-secrets; declared as a direct dep in pyproject.toml.
    import yaml as _yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — defensive; PyYAML is a hard dep.
    _yaml = None  # type: ignore[assignment]


# Cap on tsconfig `extends` traversal. Real-world chains are typically 1–3
# hops; anything past this is almost certainly a cycle or a pathologically
# nested config we don't want to spend time on.
_MAX_EXTENDS_HOPS = 8


@dataclass
class ToolConfigResult:
    """Aggregated tool config findings for a workspace/repo."""

    prettier: dict | None = None
    tsconfig: dict | None = None
    eslint: dict | None = None
    editorconfig: dict | None = None

    has_prettier_js_plugins: bool = False
    """If True, plugin rules are invisible to chameleon (warn user)."""

    has_eslint_js_plugins: bool = False
    """ESLint custom plugin signal (similar invisibility caveat)."""

    sources: dict[str, str] = field(default_factory=dict)
    """Map of `tool` → relative path read, for /chameleon-status."""

    tsconfig_extends_chain: list[str] = field(default_factory=list)
    """Resolved `extends` chain (repo-relative or bare-specifier strings),
    starting from the root tsconfig and walking outward. Empty if there was
    no `extends` field."""

    parse_warnings: dict[str, str] = field(default_factory=dict)
    """Map of `tool` → human-readable warning (e.g. malformed JS payload,
    missing extends target, extends cycle). Surfaced into rules.json so
    /chameleon-status can show them."""


def read_tool_configs(repo_root: Path) -> ToolConfigResult:
    """Read all relevant tool configs from a repo root.

    Per-workspace caller should invoke this with the workspace root, not the
    monorepo root.
    """
    result = ToolConfigResult()

    # 1. .prettierrc (and .prettierrc.json, .prettierrc.js — JS not parsed)
    for name in (".prettierrc", ".prettierrc.json"):
        p = repo_root / name
        if p.exists():
            try:
                result.prettier = json.loads(p.read_text(errors="replace"))
                result.sources["prettier"] = name
                break
            except json.JSONDecodeError:
                pass
    # JS variant — flag but don't parse
    for name in (".prettierrc.js", ".prettierrc.cjs", "prettier.config.js"):
        if (repo_root / name).exists():
            result.sources["prettier"] = result.sources.get("prettier", name)
            result.has_prettier_js_plugins = True
            break
    # Detect plugin references in JSON form
    if result.prettier and isinstance(result.prettier, dict):
        if result.prettier.get("plugins"):
            result.has_prettier_js_plugins = True

    # 2. tsconfig.json — Phase 4.7 resolves `extends` chain (relative + bare).
    tsconfig = repo_root / "tsconfig.json"
    if tsconfig.exists():
        merged, chain, warning = _resolve_tsconfig_chain(tsconfig, repo_root)
        if merged is not None:
            result.tsconfig = merged
            result.sources["tsconfig"] = "tsconfig.json"
            result.tsconfig_extends_chain = chain
        if warning:
            result.parse_warnings["tsconfig"] = warning

    # 3. .eslintrc* (multiple file forms)
    for name in (".eslintrc.json", ".eslintrc"):
        p = repo_root / name
        if p.exists():
            try:
                result.eslint = json.loads(_strip_jsonc_comments(p.read_text(errors="replace")))
                result.sources["eslint"] = name
                break
            except json.JSONDecodeError:
                pass

    # YAML variants — Phase 2C.4 parses these with PyYAML.
    if result.eslint is None:
        for name in (".eslintrc.yml", ".eslintrc.yaml"):
            p = repo_root / name
            if p.exists():
                result.sources["eslint"] = name
                parsed, warning = _parse_eslint_yaml(p)
                if parsed is not None:
                    result.eslint = parsed
                if warning:
                    result.parse_warnings["eslint"] = warning
                break

    # JS variants — Phase 2C.4 attempts a best-effort regex extraction. If it
    # fails we fall back to today's "invisibility" warning so /chameleon-status
    # still flags the gap.
    if result.eslint is None:
        for name in (".eslintrc.js", ".eslintrc.cjs", ".eslintrc.mjs"):
            p = repo_root / name
            if p.exists():
                result.sources["eslint"] = name
                parsed, warning = _parse_eslint_js(p)
                if parsed is not None:
                    result.eslint = parsed
                else:
                    # Parse failed — surface the invisibility flag like before.
                    result.has_eslint_js_plugins = True
                if warning:
                    result.parse_warnings["eslint"] = warning
                break
    else:
        # Already loaded eslint (JSON/YAML); still note .js sibling if present
        # for source visibility, but the loaded config wins.
        for name in (".eslintrc.js", ".eslintrc.cjs", ".eslintrc.mjs"):
            if (repo_root / name).exists() and "eslint" not in result.sources:
                result.sources["eslint"] = name
                result.has_eslint_js_plugins = True
                break

    if result.eslint and isinstance(result.eslint, dict):
        if result.eslint.get("plugins"):
            result.has_eslint_js_plugins = True

    # 4. .editorconfig (INI format — minimal parsing)
    editorconfig_path = repo_root / ".editorconfig"
    if editorconfig_path.exists():
        result.editorconfig = _parse_editorconfig(editorconfig_path)
        result.sources["editorconfig"] = ".editorconfig"

    return result


def _strip_jsonc_comments(text: str) -> str:
    """Minimal JSONC → JSON: strip // and /* */ comments. Preserves strings."""
    # Remove /* ... */ block comments (non-greedy)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Remove // line comments (but not inside strings — heuristic)
    out_lines = []
    for line in text.splitlines():
        # Naive: strip after // unless preceded by ":" (URL-like)
        # tsconfig comments are typically full-line or end-of-line.
        stripped = line.split("//", 1)[0] if "//" in line else line
        out_lines.append(stripped)
    text = "\n".join(out_lines)
    # Strip trailing commas before ] or }
    text = re.sub(r",(\s*[\]}])", r"\1", text)
    return text


# ---------------------------------------------------------------------------
# tsconfig extends-chain resolution
# ---------------------------------------------------------------------------


def _resolve_tsconfig_chain(
    tsconfig_path: Path, repo_root: Path
) -> tuple[dict | None, list[str], str | None]:
    """Walk a tsconfig.json's `extends` chain and return the merged config.

    Returns ``(merged_config, chain, warning)``:
      * ``merged_config`` — the effective tsconfig dict (closest-wins merge);
        ``None`` if the root file itself failed to parse.
      * ``chain`` — list of repo-relative or bare-specifier strings naming each
        config visited, ordered root-first.
      * ``warning`` — a human-readable note when something went wrong
        (cycle, missing extends target, hop cap exceeded); ``None`` if clean.

    The merge follows TypeScript semantics for our purposes: `compilerOptions`
    is dict-merged shallowly with the *derived* config winning on conflict.
    Top-level fields (`include`, `exclude`, `files`) are taken from the
    nearest config that defines them. Path-resolving fields inside
    `compilerOptions` (`paths`, `baseUrl`) follow the same closest-wins rule,
    matching TS's documented behaviour that derived configs replace rather
    than merge these arrays/maps.
    """
    chain: list[str] = []
    visited: set[Path] = set()
    warning: str | None = None

    # Parse the root file first; if that fails we return None like the
    # legacy code path.
    root_config = _load_tsconfig_file(tsconfig_path)
    if root_config is None:
        return None, chain, "tsconfig.json failed to parse"

    chain.append("tsconfig.json")
    visited.add(tsconfig_path.resolve())

    # Build a list of (config_path, parsed_dict) walking outwards along
    # `extends`. The merge then applies right-to-left so closer-in configs
    # win.
    configs: list[dict] = [root_config]
    current_path = tsconfig_path
    current = root_config

    hops = 0
    while True:
        extends_field = current.get("extends") if isinstance(current, dict) else None
        if not extends_field:
            break
        hops += 1
        if hops > _MAX_EXTENDS_HOPS:
            warning = (
                f"tsconfig extends chain exceeded {_MAX_EXTENDS_HOPS} hops; "
                "stopping to avoid runaway resolution"
            )
            break

        # `extends` in TS is normally a single string, but TS 5.0+ allows an
        # array of strings (later wins). Handle the common single-string case;
        # for arrays, treat the *last* entry as the primary parent and
        # merge the earlier ones underneath (mirrors TS behaviour: items are
        # resolved from right to left, with rightmost as the immediate
        # parent).
        extends_targets = (
            [extends_field] if isinstance(extends_field, str) else list(extends_field)
        )

        # Resolve each target; preserve order so we can merge them.
        resolved_parents: list[dict] = []
        resolved_names: list[str] = []
        chain_broke = False
        for raw_target in extends_targets:
            if not isinstance(raw_target, str):
                continue
            resolved = _resolve_extends_target(raw_target, current_path, repo_root)
            if resolved is None:
                warning = (
                    f"tsconfig extends target {raw_target!r} could not be resolved "
                    f"from {_safe_rel(current_path, repo_root)}"
                )
                chain_broke = True
                break
            target_path, parent_config = resolved
            real = target_path.resolve()
            if real in visited:
                warning = (
                    f"tsconfig extends cycle detected at {_safe_rel(target_path, repo_root)} "
                    f"(already visited)"
                )
                chain_broke = True
                break
            visited.add(real)
            resolved_parents.append(parent_config)
            resolved_names.append(_safe_rel(target_path, repo_root))

        if chain_broke or not resolved_parents:
            break

        # Apply parents in order: earlier entries are weaker, last entry is
        # the immediate parent and continues the chain.
        configs.extend(resolved_parents)
        chain.extend(resolved_names)
        current = resolved_parents[-1]
        # For continuing the walk, current_path must point at the immediate
        # parent's file so relative `extends` further up resolves correctly.
        # Recompute via the same resolver to grab the path consistently.
        last_target = extends_targets[-1]
        if isinstance(last_target, str):
            last_resolved = _resolve_extends_target(last_target, current_path, repo_root)
            if last_resolved is None:
                break
            current_path = last_resolved[0]
        else:
            break

    # Merge: walk weakest-first (deepest parent) up to derived (root tsconfig)
    # so that derived fields win.
    merged: dict = {}
    for cfg in reversed(configs):
        _merge_tsconfig_into(merged, cfg)

    # `extends` itself is metadata, not a real compilerOption — drop it from
    # the merged output so downstream code can't get confused.
    merged.pop("extends", None)

    return merged, chain, warning


def _resolve_extends_target(
    target: str, from_path: Path, repo_root: Path
) -> tuple[Path, dict] | None:
    """Resolve a tsconfig `extends` target to a (path, parsed dict) pair.

    Resolution rules (simplified from TS spec):
      * Absolute paths — used as-is.
      * Relative paths (start with ``./`` or ``../``) — joined to the
        *containing* tsconfig's directory. Missing ``.json`` extension is
        tolerated.
      * Bare specifiers (e.g. ``@tsconfig/strictest``) — resolved by walking
        up from the containing tsconfig looking for
        ``node_modules/<specifier>/tsconfig.json``. The ``<specifier>/`` part
        may already include a path (``@foo/bar/tsconfig.base.json``); in that
        case we don't append ``tsconfig.json``.

    Returns ``None`` if the target can't be located or fails to parse.
    """
    from_dir = from_path.parent

    candidates: list[Path] = []
    # Path-like targets: start with `./`, `../`, or `/`. Anything else
    # — including ``@tsconfig/strictest/tsconfig.json`` or
    # ``some-package`` — is a bare specifier and is resolved via
    # node_modules even if it happens to end in ``.json``.
    if target.startswith(("./", "../")) or target.startswith("/"):
        # Relative or absolute path-like target. Try as-given, with .json,
        # and with /tsconfig.json appended.
        if Path(target).is_absolute():
            base_candidates = [Path(target)]
        else:
            base_candidates = [from_dir / target]
        for base in base_candidates:
            candidates.append(base)
            if base.suffix != ".json":
                candidates.append(base.with_suffix(".json"))
                candidates.append(base / "tsconfig.json")
    else:
        # Bare specifier — walk up looking for node_modules/<target>.
        # Default to <target>/tsconfig.json unless the target already ends
        # with a .json file.
        target_path = Path(target)
        if target_path.suffix == ".json":
            sub = target_path
        else:
            sub = target_path / "tsconfig.json"
        for ancestor in [from_dir, *from_dir.parents]:
            candidate = ancestor / "node_modules" / sub
            candidates.append(candidate)
            # Some published @tsconfig/* packages use a non-default
            # filename. Also try a "tsconfig.base.json" fallback if the
            # tsconfig.json variant doesn't exist.
            if target_path.suffix != ".json":
                candidates.append(ancestor / "node_modules" / target_path / "tsconfig.base.json")
            # Stop once we leave the repo root to avoid leaking outside the
            # workspace. (repo_root is the bootstrap root, not the FS root.)
            try:
                if ancestor.resolve() == repo_root.resolve():
                    break
            except OSError:  # pragma: no cover — defensive
                break

    for cand in candidates:
        if cand.is_file():
            parsed = _load_tsconfig_file(cand)
            if parsed is not None:
                return cand, parsed
    return None


def _load_tsconfig_file(path: Path) -> dict | None:
    """Read + JSONC-parse a tsconfig file. Returns None on failure."""
    try:
        text = _strip_jsonc_comments(path.read_text(errors="replace"))
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _merge_tsconfig_into(target: dict, source: dict) -> None:
    """Shallow-merge ``source`` into ``target`` with tsconfig semantics.

    Top-level fields: ``source`` wins on conflict (we walk outermost → innermost
    so the caller controls order).

    ``compilerOptions``: dict-merged so individual options can be overridden
    granularly. Nested objects within compilerOptions (e.g. ``paths``) are
    replaced wholesale — matching the TS spec where ``paths`` from a derived
    config supersedes the parent's.
    """
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if key == "compilerOptions" and isinstance(value, dict):
            existing = target.get("compilerOptions")
            if isinstance(existing, dict):
                merged_co = dict(existing)
                merged_co.update(value)
                target["compilerOptions"] = merged_co
            else:
                target["compilerOptions"] = dict(value)
        else:
            target[key] = value


def _safe_rel(path: Path, repo_root: Path) -> str:
    """Render a path relative to repo_root when possible, falling back to str."""
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except (ValueError, OSError):
        return str(path)


# ---------------------------------------------------------------------------
# .eslintrc YAML / JS parsing
# ---------------------------------------------------------------------------


def _parse_eslint_yaml(path: Path) -> tuple[dict | None, str | None]:
    """Parse a .eslintrc.yml / .yaml file via PyYAML."""
    if _yaml is None:
        return None, "PyYAML unavailable; cannot parse YAML eslint config"
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return None, f"could not read {path.name}: {exc}"
    try:
        loaded = _yaml.safe_load(text)
    except _yaml.YAMLError as exc:  # type: ignore[attr-defined]
        return None, f"malformed YAML in {path.name}: {exc}"
    if not isinstance(loaded, dict):
        return None, f"{path.name} did not parse to a mapping"
    return loaded, None


# Match `module.exports = { ... };` (or `export default { ... };`) and capture
# the object literal body. Conservative — bails if we can't find a balanced
# brace pair on a best-effort scan.
_EXPORTS_RE = re.compile(
    r"(?:module\.exports|exports\.default|export\s+default)\s*=?\s*(\{)",
    re.DOTALL,
)


def _parse_eslint_js(path: Path) -> tuple[dict | None, str | None]:
    """Best-effort extract a top-level object literal from .eslintrc.js.

    The strategy: locate the ``module.exports = { ... }`` (or
    ``export default { ... }``) assignment, scan forward with a depth counter
    that tracks string/comment context, then attempt to coerce the JS-ish
    object literal into JSON. If anything fails we return ``(None, reason)``
    and the caller falls back to the legacy "invisible plugin" warning.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return None, f"could not read {path.name}: {exc}"

    match = _EXPORTS_RE.search(text)
    if not match:
        return None, f"{path.name}: no top-level module.exports assignment found"

    start = match.end() - 1  # position of the opening brace
    body = _scan_balanced_braces(text, start)
    if body is None:
        return None, f"{path.name}: unbalanced braces in module.exports object"

    coerced = _jsish_to_json(body)
    try:
        parsed = json.loads(coerced)
    except json.JSONDecodeError as exc:
        return None, f"{path.name}: object literal not JSON-coercible ({exc.msg})"
    if not isinstance(parsed, dict):
        return None, f"{path.name}: top-level export is not an object"
    return parsed, None


def _scan_balanced_braces(text: str, start: int) -> str | None:
    """Return the substring from ``start`` (a ``{``) to its matching ``}``.

    Aware of single / double / backtick strings and ``// + /* */`` comments
    so braces inside those don't fool the depth counter. Returns ``None`` if
    the braces aren't balanced.
    """
    if start >= len(text) or text[start] != "{":
        return None

    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        # Skip line comments
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                return None
            i = nl + 1
            continue
        # Skip block comments
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                return None
            i = end + 2
            continue
        # Skip string literals (single, double, template)
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


# Match an unquoted JS identifier used as an object key. Matches keys that
# come right after `{` or `,` (with optional whitespace) and end before `:`.
_JS_KEY_RE = re.compile(r'([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)')

# Match strings using single quotes — replace with double-quoted variant.
# Handles escaped single quotes within.
_SINGLE_QUOTE_STRING_RE = re.compile(r"'((?:\\.|[^'\\])*)'")

# Trailing commas before `}` or `]`.
_TRAILING_COMMA_RE = re.compile(r",(\s*[\]}])")


def _jsish_to_json(text: str) -> str:
    """Coerce a JS-ish object literal into something json.loads can handle.

    Handles three common deviations from strict JSON:
      * unquoted keys (``rules:`` → ``"rules":``)
      * single-quoted strings
      * trailing commas

    This is a best-effort shim: any deeply funky payload (template literals,
    spread, function values, regex literals) will still fall through to the
    parse-fail path, which is the documented fallback.
    """
    # 1. Strip line/block comments so they don't confuse the quote scan.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(^|[^:])//[^\n]*", r"\1", text)
    # 2. Convert single-quoted strings to JSON double-quoted strings.
    def _swap_quotes(m: re.Match[str]) -> str:
        inner = m.group(1)
        # unescape \' inside, then escape " for JSON
        inner = inner.replace("\\'", "'").replace('"', '\\"')
        return f'"{inner}"'

    text = _SINGLE_QUOTE_STRING_RE.sub(_swap_quotes, text)
    # 3. Quote bare identifier keys.
    text = _JS_KEY_RE.sub(r'\1"\2"\3', text)
    # 4. Strip trailing commas.
    text = _TRAILING_COMMA_RE.sub(r"\1", text)
    return text


# ---------------------------------------------------------------------------
# .editorconfig
# ---------------------------------------------------------------------------


def _parse_editorconfig(path: Path) -> dict:
    """Minimal .editorconfig parser. Returns {section: {key: value}}."""
    result: dict[str, dict[str, str]] = {}
    current_section: str | None = "root"
    result["root"] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            result.setdefault(current_section, {})
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if current_section is not None:
                result.setdefault(current_section, {})[key] = value
    return result
