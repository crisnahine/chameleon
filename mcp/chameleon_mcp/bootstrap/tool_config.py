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
    rubocop: dict | None = None
    """BUG-014: extracted .rubocop.yml content (top-level cops, plugins,
    AllCops settings, etc.). None when the repo has no rubocop config."""

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
    # still flags the gap. BUG-020 (v0.5.6): also look at flat-config files
    # (eslint.config.{js,mjs,cjs,ts}) which ESLint 9+ ships by default.
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
                    # Parse failed — surface the invisibility flag like before.
                    result.has_eslint_js_plugins = True
                if warning:
                    result.parse_warnings["eslint"] = warning
                break
    else:
        # Already loaded eslint (JSON/YAML); still note JS/flat sibling if
        # present for source visibility, but the loaded config wins.
        for name in eslint_js_candidates:
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

    # 5. .rubocop.yml — BUG-014 (v0.5.6). Pre-v0.5.6 all four Ruby test
    # repos returned ``rules: {}`` because chameleon had no Ruby-tool
    # extractor. Reads top-level keys (AllCops, plugins, cops) so a Ruby
    # file's get_pattern_context surfaces real linting guidance.
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

    return result


def _strip_jsonc_comments(text: str) -> str:
    """Minimal JSONC → JSON: strip // and /* */ comments. Preserves strings.

    BUG-NEW-012 (v0.5.7-redo): the previous implementation naively split on
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
                # Preserve escape pair verbatim
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            out.append(ch)
            i += 1
            continue
        # Outside string
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                # Line comment: skip to newline
                j = text.find("\n", i + 2)
                if j == -1:
                    j = n
                i = j
                continue
            if nxt == "*":
                # Block comment: skip to */
                j = text.find("*/", i + 2)
                if j == -1:
                    i = n
                else:
                    i = j + 2
                continue
        out.append(ch)
        i += 1
    stripped = "".join(out)
    # Strip trailing commas before ] or }
    stripped = re.sub(r",(\s*[\]}])", r"\1", stripped)
    return stripped


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

        # BUG-NEW-012 (v0.5.7-redo): also try workspace packages when the
        # specifier looks like an org-prefixed local package
        # (e.g. ``@plane/typescript-config/react-library.json``).
        # pnpm/yarn-workspace monorepos publish their config packages
        # through ``link:`` / ``workspace:`` deps, so they don't end up
        # under ``node_modules/`` in fresh checkouts. The repo's
        # ``packages/<name>/`` is the actual on-disk location.
        # Pass from_dir (the tsconfig's parent dir) so the workspace
        # walker starts in a directory, not a file.
        candidates.extend(
            _resolve_workspace_package_target(target, from_dir, repo_root)
        )

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
                data = json.loads(pkg.read_text(errors="replace"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and data.get("workspaces"):
                return walker
        parent = walker.parent
        if parent == walker:
            break
        walker = parent
    return None


def _resolve_workspace_package_target(
    target: str, from_path: Path, repo_root: Path
) -> list[Path]:
    """Find candidate paths for an org-prefixed workspace-local package.

    BUG-NEW-012 (v0.5.7-redo): pnpm/yarn-workspace monorepos like plane
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

    # Org-prefixed specifier (@org/pkg/...) parses as ["@org", "pkg", ...].
    if head.startswith("@") and len(parts) >= 2:
        pkg_name = parts[1]
        rest = Path(*parts[2:]) if len(parts) > 2 else None
    else:
        return []

    if not pkg_name:
        return []

    # Find workspace root; fall back to repo_root if no ws root upstream.
    ws_root = _workspace_monorepo_root(from_path) or repo_root

    candidates: list[Path] = []
    for ws_parent in ("packages", "apps", "services", "workspaces"):
        pkg_dir = ws_root / ws_parent / pkg_name
        if not pkg_dir.is_dir():
            continue
        if rest is None:
            # Default to tsconfig.json in the package root.
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


def _parse_rubocop_yaml(path: Path) -> tuple[dict | None, str | None]:
    """Parse a .rubocop.yml file via PyYAML (BUG-014, v0.5.6).

    Captures the top-level mapping verbatim: ``AllCops``, ``plugins``,
    ``require``, ``inherit_from``, plus every cop entry. We cap the
    payload at ~50KB to keep rules.json bounded even for very large
    real-world configs (gitlab's .rubocop.yml is 42KB).
    """
    if _yaml is None:
        return None, "PyYAML unavailable; cannot parse rubocop config"
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return None, f"could not read {path.name}: {exc}"
    if len(text) > 200_000:
        text = text[:200_000]
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


def _parse_eslint_js_via_node(path: Path) -> tuple[dict | None, str | None]:
    """Evaluate an ESLint config via Node and return its exported object.

    BUG-003 / BUG-020 (v0.5.6): the regex-based parser below handles only
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
        # Dynamic import path: works for both flat-config (eslint.config.*)
        # and legacy .eslintrc.mjs.
        script = (
            "(async () => {"
            "  try {"
            f"    const m = await import({json.dumps(str(path.resolve()))});"
            "    const v = m.default ?? m;"
            "    process.stdout.write(JSON.stringify(v, (k, val) => {"
            # Functions / undefined / symbols aren't JSON-serializable;
            # drop them so the rest of the config survives.
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
        # CJS path
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
        # Trim node's stderr to first line so the warning stays compact.
        first_line = (result.stderr.splitlines() or ["non-zero exit"])[0]
        return None, f"{name}: node eval returned non-zero ({first_line[:120]})"
    try:
        parsed = json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError as exc:
        return None, f"{name}: node output not JSON ({exc.msg})"
    if parsed is None:
        return None, f"{name}: node returned empty"
    # Flat config exports an array of config blocks; merge known keys for
    # consumer-side simplicity. For legacy .eslintrc.* a plain dict is
    # already returned.
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

    BUG-003 (v0.5.6): try the Node-eval path first (most accurate); fall
    back to the regex-based parser only when Node is unavailable or the
    eval fails.

    The strategy: locate the ``module.exports = { ... }`` (or
    ``export default { ... }``) assignment, scan forward with a depth counter
    that tracks string/comment context, then attempt to coerce the JS-ish
    object literal into JSON. If anything fails we return ``(None, reason)``
    and the caller falls back to the legacy "invisible plugin" warning.
    """
    parsed, node_warning = _parse_eslint_js_via_node(path)
    if parsed is not None:
        return parsed, None

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
        # Prefer the node failure note if it had one; both being None is rare.
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
