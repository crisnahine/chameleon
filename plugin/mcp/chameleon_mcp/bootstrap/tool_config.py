"""Tool config reading — `.prettierrc`, `tsconfig.json`, `.eslintrc*`, `.editorconfig`.

Per docs/architecture.md "Bootstrap interview flow" step (c) + "Tracked dimensions
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

try:
    import yaml as _yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — defensive; PyYAML is a hard dep.
    _yaml = None  # type: ignore[assignment]


_MAX_EXTENDS_HOPS = 8

# Upper bound on bytes any tool-config reader pulls into memory. Bootstrap-time
# only, never a hook hot path, but a hostile or accidentally-huge config (a
# multi-GB .eslintrc.yml) must not be read whole. The cap is generous: real
# eslint/tsconfig/package.json/pyproject configs are tiny, so a legit large
# monorepo manifest still parses, while a pathological file is bounded. A
# structured config (JSON/TOML) truncated mid-value parses fail-open to None/{}
# in each reader's existing except, which is the right outcome for a file this
# size.
_MAX_CONFIG_BYTES = 4_000_000


def _read_capped(path: Path, *, max_bytes: int | None = None) -> str:
    """Read at most ``max_bytes`` of a config file as text.

    Reads bounded so a hostile multi-GB config is never materialized whole, then
    decodes with ``errors="replace"`` to match every reader's prior ``read_text``
    shape. ``max_bytes`` defaults to ``_MAX_CONFIG_BYTES``, resolved at call time
    so the bound stays tunable. Raises ``OSError`` on a read failure so callers
    keep their existing ``except OSError`` handling.
    """
    cap = _MAX_CONFIG_BYTES if max_bytes is None else max_bytes
    with path.open("rb") as fh:
        raw = fh.read(cap)
    return raw.decode("utf-8", errors="replace")


def _load_toml(path: Path) -> dict:
    """Parse a TOML config to a dict, or ``{}`` on any read/parse failure.

    Bounded via ``_read_capped``; fail-open like the sibling JSON/YAML readers so
    a malformed config contributes nothing rather than raising.
    """
    try:
        import tomllib

        data = tomllib.loads(_read_capped(path))
    except (OSError, ValueError, ImportError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class ToolConfigResult:
    """Aggregated tool config findings for a workspace/repo."""

    prettier: dict | None = None
    tsconfig: dict | None = None
    eslint: dict | None = None
    editorconfig: dict | None = None
    rubocop: dict | None = None
    """BUG-014: extracted .rubocop.yml content (top-level cops, plugins,
    AllCops settings, etc.). None when the repo has no rubocop config."""

    python_format: dict | None = None
    """Declared Python formatter config (black / ruff / flake8): ``line_length``
    and ``quote_style`` ("single"/"double"). None when no Python formatter config
    is present. Pure TOML/INI parse, no repo-code execution."""

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

    for name in (".prettierrc", ".prettierrc.json"):
        p = repo_root / name
        if p.exists():
            try:
                result.prettier = json.loads(_read_capped(p))
                result.sources["prettier"] = name
                break
            except json.JSONDecodeError:
                pass
    for name in (".prettierrc.js", ".prettierrc.cjs", "prettier.config.js"):
        if (repo_root / name).exists():
            result.sources["prettier"] = result.sources.get("prettier", name)
            result.has_prettier_js_plugins = True
            break
    if result.prettier and isinstance(result.prettier, dict):
        if result.prettier.get("plugins"):
            result.has_prettier_js_plugins = True

    tsconfig = repo_root / "tsconfig.json"
    if tsconfig.exists():
        merged, chain, warning = _resolve_tsconfig_chain(tsconfig, repo_root)
        if merged is not None:
            result.tsconfig = merged
            result.sources["tsconfig"] = "tsconfig.json"
            result.tsconfig_extends_chain = chain
        if warning:
            result.parse_warnings["tsconfig"] = warning

    for name in (".eslintrc.json", ".eslintrc"):
        p = repo_root / name
        if p.exists():
            try:
                result.eslint = json.loads(_strip_jsonc_comments(_read_capped(p)))
                result.sources["eslint"] = name
                break
            except json.JSONDecodeError:
                pass

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

    eslint_js_candidates = (
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.mjs",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        "eslint.config.ts",
    )
    if result.eslint is None:
        for name in eslint_js_candidates:
            p = repo_root / name
            if p.exists():
                result.sources["eslint"] = name
                parsed, warning = _parse_eslint_js(p)
                if parsed is not None:
                    result.eslint = parsed
                else:
                    result.has_eslint_js_plugins = True
                if warning:
                    result.parse_warnings["eslint"] = warning
                break
    else:
        for name in eslint_js_candidates:
            if (repo_root / name).exists() and "eslint" not in result.sources:
                result.sources["eslint"] = name
                result.has_eslint_js_plugins = True
                break

    if result.eslint and isinstance(result.eslint, dict):
        if result.eslint.get("plugins"):
            result.has_eslint_js_plugins = True

    editorconfig_path = repo_root / ".editorconfig"
    if editorconfig_path.exists():
        result.editorconfig = _parse_editorconfig(editorconfig_path)
        result.sources["editorconfig"] = ".editorconfig"

    for name in (".rubocop.yml", ".rubocop.yaml"):
        p = repo_root / name
        if p.exists():
            parsed, warning = _parse_rubocop_yaml(p)
            if parsed is not None:
                result.rubocop = parsed
                result.sources["rubocop"] = name
            if warning:
                result.parse_warnings["rubocop"] = warning
            break

    py_fmt, py_src = _read_python_format(repo_root)
    if py_fmt:
        result.python_format = py_fmt
        if py_src:
            result.sources["python_format"] = py_src

    return result


def _read_python_format(repo_root: Path) -> tuple[dict | None, str | None]:
    """Declared Python line-length + quote-style from black / ruff / flake8.

    Pure TOML/INI parse (no repo-code execution). Precedence for line length:
    ruff > black > flake8/pycodestyle. Quote style: ruff ``[tool.ruff.format]``
    quote-style first, else black (double unless string-normalization is
    skipped). Returns ``({line_length?, quote_style?}, source)`` or ``(None,
    None)`` when nothing is declared. Fails open: a malformed config contributes
    nothing rather than raising.

    Ruff config discovery mirrors ruff's own: a standalone ``.ruff.toml`` or
    ``ruff.toml`` takes precedence over ``pyproject.toml``'s ``[tool.ruff]`` and
    ruff does NOT merge them (the first file found wins). In a standalone file the
    keys live at the TOP LEVEL (``line-length``, ``[format]``), whereas in
    pyproject they are nested under ``[tool.ruff]`` / ``[tool.ruff.format]``.
    Black config is independent and only ever read from ``[tool.black]``.
    """
    fmt: dict = {}
    source: str | None = None

    # Resolve the ruff config once: standalone .ruff.toml > ruff.toml >
    # pyproject [tool.ruff]. A standalone file's keys are already top-level, so
    # it maps directly onto the same shape as the [tool.ruff] table.
    ruff: dict = {}
    ruff_source: str | None = None
    for name in (".ruff.toml", "ruff.toml"):
        p = repo_root / name
        if p.is_file():
            ruff = _load_toml(p)
            ruff_source = name
            break

    black: dict = {}
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        data = _load_toml(pyproject)
        tool = data.get("tool") if isinstance(data, dict) else None
        tool = tool if isinstance(tool, dict) else {}
        # Only fall back to pyproject [tool.ruff] when no standalone ruff config
        # was found — ruff ignores [tool.ruff] entirely once a standalone file
        # exists, so honoring both would fabricate a merge ruff never performs.
        if ruff_source is None and isinstance(tool.get("ruff"), dict):
            ruff = tool["ruff"]
            ruff_source = "pyproject.toml"
        if isinstance(tool.get("black"), dict):
            black = tool["black"]

    if ruff or black:
        ruff_format = ruff.get("format") if isinstance(ruff.get("format"), dict) else {}

        ll_ruff = _coerce_positive_int(ruff.get("line-length"))
        ll = ll_ruff if ll_ruff is not None else _coerce_positive_int(black.get("line-length"))
        if ll is not None:
            fmt["line_length"] = ll
            if source is None:
                source = ruff_source if ll_ruff is not None else "pyproject.toml"
        qs = ruff_format.get("quote-style")
        if qs in ("single", "double"):
            fmt["quote_style"] = qs
            if source is None:
                source = ruff_source
        elif "quote_style" not in fmt and black and not black.get("skip-string-normalization"):
            # black normalizes to double quotes unless told not to.
            fmt["quote_style"] = "double"
            if source is None:
                source = "pyproject.toml"
        # ruff indent: indent-style ("tab"/"space") lives under [tool.ruff.format];
        # indent-width is a top-level [tool.ruff] key the formatter inherits. black
        # has no indent config (it is always 4 spaces).
        ist = ruff_format.get("indent-style")
        if ist in ("tab", "space"):
            fmt["indent_style"] = ist
            if source is None:
                source = ruff_source
        iw = _coerce_positive_int(ruff.get("indent-width"))
        if iw is not None:
            fmt["indent_width"] = iw
            if source is None:
                source = ruff_source

    if "line_length" not in fmt:
        for name in ("setup.cfg", "tox.ini", ".flake8"):
            p = repo_root / name
            if not p.is_file():
                continue
            try:
                import configparser

                cp = configparser.ConfigParser()
                cp.read_string(_read_capped(p))
            except (OSError, configparser.Error):
                continue
            for section in ("flake8", "pycodestyle"):
                if cp.has_option(section, "max-line-length"):
                    ll = _coerce_positive_int(cp.get(section, "max-line-length"))
                    if ll is not None:
                        fmt["line_length"] = ll
                        source = source or name
                        break
            if "line_length" in fmt:
                break

    return (fmt or None), source


def _coerce_positive_int(value) -> int | None:
    """A positive int from an int/str, or None."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _strip_jsonc_comments(text: str) -> str:
    """Minimal JSONC → JSON: strip // and /* */ comments. Preserves strings.

    BUG-NEW-012: the previous implementation naively split on
    `//` and ate everything after, which corrupted URL string literals like
    ``"$schema": "https://json.schemastore.org/tsconfig"``. tsconfig files
    that ship a $schema URL (most modern ones do) failed to parse, returning
    `None` from `_load_tsconfig_file` and dropping the whole extends chain.

    The fix is a single-pass scanner that tracks string-literal state so
    `//` and `/* */` only strip when outside a string. Escape sequences
    inside strings are handled minimally — enough to survive ``\\"`` and
    ``\\\\``. Trailing-comma cleanup runs after.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i + 2)
                if j == -1:
                    j = n
                i = j
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                if j == -1:
                    i = n
                else:
                    i = j + 2
                continue
        out.append(ch)
        i += 1
    stripped = "".join(out)
    stripped = re.sub(r",(\s*[\]}])", r"\1", stripped)
    return stripped


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

    root_config = _load_tsconfig_file(tsconfig_path)
    if root_config is None:
        return None, chain, "tsconfig.json failed to parse"

    chain.append("tsconfig.json")
    visited.add(tsconfig_path.resolve())

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

        extends_targets = [extends_field] if isinstance(extends_field, str) else list(extends_field)

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

        configs.extend(resolved_parents)
        chain.extend(resolved_names)
        current = resolved_parents[-1]
        last_target = extends_targets[-1]
        if isinstance(last_target, str):
            last_resolved = _resolve_extends_target(last_target, current_path, repo_root)
            if last_resolved is None:
                break
            current_path = last_resolved[0]
        else:
            break

    merged: dict = {}
    for cfg in reversed(configs):
        _merge_tsconfig_into(merged, cfg)

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
    if target.startswith(("./", "../")) or target.startswith("/"):
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
        target_path = Path(target)
        if target_path.suffix == ".json":
            sub = target_path
        else:
            sub = target_path / "tsconfig.json"
        for ancestor in [from_dir, *from_dir.parents]:
            candidate = ancestor / "node_modules" / sub
            candidates.append(candidate)
            if target_path.suffix != ".json":
                candidates.append(ancestor / "node_modules" / target_path / "tsconfig.base.json")
            try:
                if ancestor.resolve() == repo_root.resolve():
                    break
            except OSError:  # pragma: no cover — defensive
                break

        candidates.extend(_resolve_workspace_package_target(target, from_dir, repo_root))

    for cand in candidates:
        if cand.is_file():
            parsed = _load_tsconfig_file(cand)
            if parsed is not None:
                return cand, parsed
    return None


def _workspace_monorepo_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a workspace-monorepo root.

    BUG-NEW-012 helper: the workspace root carries one of:
      * ``pnpm-workspace.yaml`` (pnpm)
      * ``package.json`` with a ``workspaces`` field (yarn / npm 7+)

    The first ancestor that matches wins. Returns None if no workspace
    root is found within 8 levels (we don't need to walk far).
    """
    walker = start
    for _ in range(8):
        if (walker / "pnpm-workspace.yaml").is_file():
            return walker
        pkg = walker / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(_read_capped(pkg))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and data.get("workspaces"):
                return walker
        parent = walker.parent
        if parent == walker:
            break
        walker = parent
    return None


def _resolve_workspace_package_target(target: str, from_path: Path, repo_root: Path) -> list[Path]:
    """Find candidate paths for an org-prefixed workspace-local package.

    BUG-NEW-012: pnpm/yarn-workspace monorepos like plane
    publish their tsconfig packages via ``link:`` / ``workspace:`` deps,
    so ``node_modules/@plane/typescript-config/react-library.json`` does
    not exist on a fresh checkout. The actual files live at
    ``packages/typescript-config/react-library.json``.

    Heuristic: for a specifier ``@org/pkg-name/sub/path.json``:
      1. Find the workspace monorepo root by walking up from from_path
         until we hit ``pnpm-workspace.yaml`` or a ``package.json`` with
         a ``workspaces`` field. Fall back to repo_root if none found.
      2. Look in ``<ws_root>/packages/<pkg-name>/``, ``apps/<pkg-name>/``,
         etc.

    We need step 1 because read_tool_configs is called with the
    workspace dir as repo_root (not the actual monorepo root), so
    repo_root alone can't locate sibling packages.
    """
    target_path = Path(target)
    parts = target_path.parts
    if not parts:
        return []
    head = parts[0]

    if head.startswith("@") and len(parts) >= 2:
        pkg_name = parts[1]
        rest = Path(*parts[2:]) if len(parts) > 2 else None
    else:
        return []

    if not pkg_name:
        return []

    ws_root = _workspace_monorepo_root(from_path) or repo_root

    candidates: list[Path] = []
    for ws_parent in ("packages", "apps", "services", "workspaces"):
        pkg_dir = ws_root / ws_parent / pkg_name
        if not pkg_dir.is_dir():
            continue
        if rest is None:
            candidates.append(pkg_dir / "tsconfig.json")
            candidates.append(pkg_dir / "tsconfig.base.json")
        else:
            candidates.append(pkg_dir / rest)
            if rest.suffix != ".json":
                candidates.append(pkg_dir / rest.with_suffix(".json"))
    return candidates


def _load_tsconfig_file(path: Path) -> dict | None:
    """Read + JSONC-parse a tsconfig file. Returns None on failure."""
    try:
        text = _strip_jsonc_comments(_read_capped(path))
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
    """Render a path relative to repo_root, ``../``-style when outside it.

    A cross-package tsconfig extends in a monorepo workspace points outside
    the workspace dir; the old absolute-string fallback persisted machine-
    (and, under production-ref pinning, materialized-worktree-) specific
    paths into committed rules.json. A bounded ``..`` walk-up stays
    deterministic and resolves 1:1 in any checkout of the same layout.
    """
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except (ValueError, OSError):
        pass
    try:
        import os as _os

        rel = _os.path.relpath(str(path.resolve()), str(repo_root.resolve()))
    except (ValueError, OSError):
        return str(path)
    # A target on another root/drive renders an absurd ../ chain; keep the
    # absolute string for anything implausibly far above the workspace.
    if rel.count("..") <= 8:
        return rel
    return str(path)


def _parse_eslint_yaml(path: Path) -> tuple[dict | None, str | None]:
    """Parse a .eslintrc.yml / .yaml file via PyYAML."""
    if _yaml is None:
        return None, "PyYAML unavailable; cannot parse YAML eslint config"
    try:
        text = _read_capped(path)
    except OSError as exc:
        return None, f"could not read {path.name}: {exc}"
    try:
        loaded = _yaml.safe_load(text)
    except _yaml.YAMLError as exc:  # type: ignore[attr-defined]
        return None, f"malformed YAML in {path.name}: {exc}"
    if not isinstance(loaded, dict):
        return None, f"{path.name} did not parse to a mapping"
    return loaded, None


# ERB tags inside .rubocop.yml (gitlab: `parallel: <%= ENV['CI'] ... %>`).
# Rendering ERB would execute repo code, which chameleon never does during
# bootstrap; instead the tags are neutralized TEXTUALLY so the rest of the
# (usually large) config still parses: value tags become a placeholder scalar,
# control-flow tags vanish.
_ERB_VALUE_TAG_RE = re.compile(r"<%=.*?%>", re.DOTALL)
_ERB_CONTROL_TAG_RE = re.compile(r"<%[^=].*?%>", re.DOTALL)


def _parse_rubocop_yaml(path: Path) -> tuple[dict | None, str | None]:
    """Parse a .rubocop.yml file via PyYAML (BUG-014).

    Captures the top-level mapping verbatim: ``AllCops``, ``plugins``,
    ``require``, ``inherit_from``, plus every cop entry. We cap the
    payload at ~50KB to keep rules.json bounded even for very large
    real-world configs (gitlab's .rubocop.yml is 42KB).

    A config carrying ERB (Rails repos commonly template a value or two) is
    not valid YAML as-is; rather than silently dropping the WHOLE rubocop
    config — the primary linter on a Rails repo — the parse retries once with
    the ERB tags neutralized textually (never rendered: rendering executes
    repo code). The success result then carries a warning noting the
    substitution so a placeholder value is never mistaken for a real one.
    """
    if _yaml is None:
        return None, "PyYAML unavailable; cannot parse rubocop config"
    try:
        text = _read_capped(path)
    except OSError as exc:
        return None, f"could not read {path.name}: {exc}"
    if len(text) > 200_000:
        text = text[:200_000]
    try:
        loaded = _yaml.safe_load(text)
    except _yaml.YAMLError as exc:  # type: ignore[attr-defined]
        first_error = f"malformed YAML in {path.name}: {exc}"
        loaded, note = _parse_rubocop_tolerant(text, path.name)
        if loaded is None:
            return None, first_error
        return loaded, note
    if not isinstance(loaded, dict):
        return None, f"{path.name} did not parse to a mapping"
    return loaded, None


def _parse_rubocop_tolerant(text: str, name: str) -> tuple[dict | None, str | None]:
    """Best-effort recovery parse for a .rubocop.yml the strict pass rejected.

    Two tolerated constructs, both neutralized TEXTUALLY/structurally without
    ever executing or constructing anything:
    - ERB tags: value tags become the placeholder scalar ``erb_omitted``,
      control-flow tags vanish.
    - Custom YAML tags (``!ruby/regexp /…/``): mapped to their raw scalar
      string instead of an object construction error.
    Returns (parsed, warning-note) or (None, None) when even the tolerant
    pass fails.
    """
    if _yaml is None:
        return None, None
    tolerated: list[str] = []
    neutralized = text
    if "<%" in neutralized:
        neutralized = _ERB_VALUE_TAG_RE.sub("erb_omitted", neutralized)
        neutralized = _ERB_CONTROL_TAG_RE.sub("", neutralized)
        tolerated.append("ERB tags neutralized (templated values appear as 'erb_omitted')")

    class _TolerantLoader(_yaml.SafeLoader):  # type: ignore[name-defined]
        pass

    _TolerantLoader.add_multi_constructor(
        "!", lambda loader, suffix, node: getattr(node, "value", None)
    )
    try:
        loaded = _yaml.load(neutralized, Loader=_TolerantLoader)  # noqa: S506 — SafeLoader subclass
    except _yaml.YAMLError:
        return None, None
    if not isinstance(loaded, dict):
        return None, None
    tolerated.append("custom YAML tags read as plain strings")
    return loaded, f"{name} required a tolerant parse: " + "; ".join(tolerated)


_EXPORTS_RE = re.compile(
    r"(?:module\.exports|exports\.default|export\s+default)\s*=?\s*(\{)",
    re.DOTALL,
)


def _parse_eslint_js_via_node(path: Path) -> tuple[dict | None, str | None]:
    """Evaluate an ESLint config via Node and return its exported object.

    BUG-003 / BUG-020: the regex-based parser below handles only
    trivial object literals. Real-world configs use computed values,
    spread, parserOptions nested objects, etc. — anything beyond the
    simplest shape produces "object literal not JSON-coercible". The
    plugin already requires Node >= 20, so shelling out to Node gives
    us the same value ESLint sees.

    Strategy:
      - For ``.eslintrc.{js,cjs}``: ``node -e "console.log(JSON.stringify(require('<path>')))"``
      - For ``.eslintrc.mjs`` / ``eslint.config.{js,mjs,cjs,ts}``: use
        dynamic import — ``await import('<path>')`` and JSON.stringify the
        default export.

    Returns ``(parsed_dict, None)`` on success or ``(None, reason)``.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return None, "node not on PATH"

    name = path.name
    is_esm = name.endswith(".mjs") or name == "eslint.config.mjs"
    is_flat = name.startswith("eslint.config.")

    if is_esm or is_flat:
        script = (
            "(async () => {"
            "  try {"
            f"    const m = await import({json.dumps(str(path.resolve()))});"
            "    const v = m.default ?? m;"
            "    process.stdout.write(JSON.stringify(v, (k, val) => {"
            "      if (typeof val === 'function' || typeof val === 'symbol') return undefined;"
            "      return val;"
            "    }));"
            "  } catch (e) {"
            "    process.stderr.write(String(e && e.stack || e));"
            "    process.exit(2);"
            "  }"
            "})();"
        )
        cmd = [node, "--input-type=module", "-e", script]
    else:
        script = (
            "try {"
            f"  const m = require({json.dumps(str(path.resolve()))});"
            "  const v = m && m.default ? m.default : m;"
            "  process.stdout.write(JSON.stringify(v, (k, val) => {"
            "    if (typeof val === 'function' || typeof val === 'symbol') return undefined;"
            "    return val;"
            "  }));"
            "} catch (e) {"
            "  process.stderr.write(String(e && e.stack || e));"
            "  process.exit(2);"
            "}"
        )
        cmd = [node, "-e", script]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"{name}: node eval failed ({exc})"
    if result.returncode != 0:
        first_line = (result.stderr.splitlines() or ["non-zero exit"])[0]
        return None, f"{name}: node eval returned non-zero ({first_line[:120]})"
    try:
        parsed = json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError as exc:
        return None, f"{name}: node output not JSON ({exc.msg})"
    if parsed is None:
        return None, f"{name}: node returned empty"
    if isinstance(parsed, list):
        merged: dict = {"flat": True, "rules": {}, "extends": [], "plugins": []}
        for block in parsed:
            if not isinstance(block, dict):
                continue
            rules = block.get("rules")
            if isinstance(rules, dict):
                merged["rules"].update(rules)
            extends = block.get("extends")
            if isinstance(extends, list):
                merged["extends"].extend(extends)
            plugins = block.get("plugins")
            if isinstance(plugins, list):
                merged["plugins"].extend(plugins)
            elif isinstance(plugins, dict):
                merged["plugins"].extend(plugins.keys())
        return merged, None
    if not isinstance(parsed, dict):
        return None, f"{name}: top-level export is not an object/array"
    return parsed, None


def _parse_eslint_js(path: Path) -> tuple[dict | None, str | None]:
    """Best-effort extract a top-level object literal from .eslintrc.js.

    SECURITY: a Node-eval path (``require()``/``import()`` of the config, the
    most accurate reader) exists but is DISABLED by default — loading a JS
    config executes the repo's own code, which would run arbitrary code when
    bootstrapping a cloned/untrusted repo. The default is a static regex
    parser that never executes repo code. Set ``CHAMELEON_ALLOW_ESLINT_EVAL=1``
    to opt into the Node-eval path for repos you trust.

    The static strategy: locate the ``module.exports = { ... }`` (or
    ``export default { ... }``) assignment, scan forward with a depth counter
    that tracks string/comment context, then attempt to coerce the JS-ish
    object literal into JSON. If anything fails we return ``(None, reason)``
    and the caller falls back to the legacy "invisible plugin" warning.
    """
    import os

    node_warning: str | None = None
    if os.environ.get("CHAMELEON_ALLOW_ESLINT_EVAL") == "1":
        parsed, node_warning = _parse_eslint_js_via_node(path)
        if parsed is not None:
            return parsed, None

    try:
        text = _read_capped(path)
    except OSError as exc:
        return None, f"could not read {path.name}: {exc}"

    match = _EXPORTS_RE.search(text)
    if not match:
        return None, f"{path.name}: no top-level module.exports assignment found"

    start = match.end() - 1
    body = _scan_balanced_braces(text, start)
    if body is None:
        return None, f"{path.name}: unbalanced braces in module.exports object"

    coerced = _jsish_to_json(body)
    try:
        parsed = json.loads(coerced)
    except json.JSONDecodeError as exc:
        if node_warning:
            return None, f"{path.name}: {node_warning} (regex fallback: {exc.msg})"
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
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                return None
            i = nl + 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                return None
            i = end + 2
            continue
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


_JS_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)")

_SINGLE_QUOTE_STRING_RE = re.compile(r"'((?:\\.|[^'\\])*)'")

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
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(^|[^:])//[^\n]*", r"\1", text)

    def _swap_quotes(m: re.Match[str]) -> str:
        inner = m.group(1)
        inner = inner.replace("\\'", "'").replace('"', '\\"')
        return f'"{inner}"'

    text = _SINGLE_QUOTE_STRING_RE.sub(_swap_quotes, text)
    text = _JS_KEY_RE.sub(r'\1"\2"\3', text)
    text = _TRAILING_COMMA_RE.sub(r"\1", text)
    return text


def _parse_editorconfig(path: Path) -> dict:
    """Minimal .editorconfig parser. Returns {section: {key: value}}."""
    result: dict[str, dict[str, str]] = {}
    current_section: str | None = "root"
    result["root"] = {}
    try:
        text = _read_capped(path)
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
