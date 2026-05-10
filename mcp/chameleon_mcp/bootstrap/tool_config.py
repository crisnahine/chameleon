"""Tool config reading — `.prettierrc`, `tsconfig.json`, `.eslintrc*`, `.editorconfig`.

Per ARCHITECTURE.md "Bootstrap interview flow" step (c) + "Tracked dimensions
catalog" Tier 1 dimensions 30-34: tool configs are GROUND TRUTH for structural
rules (formatting, linting, type-check strictness). Statistical analysis defers
to these.

Per Round 2 bootstrap edge case adversary: warn user when `.prettierrc`
references JS plugins (rules invisible to Python parsing). Phase 2C ships
basic readers; full extends-chain resolution deferred to Phase 4.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


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

    # 2. tsconfig.json (Phase 2C: read as-is; Phase 4 resolves `extends` chain)
    tsconfig = repo_root / "tsconfig.json"
    if tsconfig.exists():
        try:
            # tsconfig allows comments + trailing commas; strip comments minimally
            text = _strip_jsonc_comments(tsconfig.read_text(errors="replace"))
            result.tsconfig = json.loads(text)
            result.sources["tsconfig"] = "tsconfig.json"
        except json.JSONDecodeError:
            pass

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
    for name in (".eslintrc.js", ".eslintrc.cjs", ".eslintrc.mjs", ".eslintrc.yaml", ".eslintrc.yml"):
        if (repo_root / name).exists():
            result.sources["eslint"] = result.sources.get("eslint", name)
            # JS / YAML — Phase 2C doesn't parse; flag invisibility
            if name.endswith((".js", ".cjs", ".mjs")):
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
