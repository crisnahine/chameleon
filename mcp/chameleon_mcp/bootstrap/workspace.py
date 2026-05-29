"""Workspace detection for monorepos.

Detects pnpm/yarn/lerna/turbo/nx workspaces and proposes per-workspace
bootstrap. Per docs/architecture.md "Bootstrap interview flow" step (b)
+ Round 2 bootstrap edge case adversary recommendations.

Phase 2C.5 expands workspace_paths population beyond `package.json`
workspaces to also resolve pnpm-workspace.yaml (via PyYAML),
lerna.json `packages`, and turbo.json (which can declare its own
`workspaces`/`packages` array in turbo 1.10+). nx.json rarely declares
workspaces inline and is intentionally skipped — the apps/+libs/
heuristic remains for Nx.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml as _yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — defensive; PyYAML is a hard dep.
    _yaml = None  # type: ignore[assignment]


@dataclass
class WorkspaceInfo:
    """Detected workspace structure (or single-package if no workspace markers)."""

    is_workspace: bool
    manager: str | None
    workspace_paths: list[Path] = field(default_factory=list)
    """Sub-package roots (e.g., apps/web, packages/ui). Each is a candidate for
    its own per-workspace .chameleon/ profile."""

    @property
    def has_workspaces(self) -> bool:
        return self.is_workspace and len(self.workspace_paths) > 0


def detect_workspace(repo_root: Path) -> WorkspaceInfo:
    """Detect workspace manager + sub-package paths.

    Detection precedence (first match wins):
    1. pnpm-workspace.yaml
    2. yarn workspaces (in package.json)
    3. lerna.json
    4. turbo.json (with `pipeline`/`tasks` field; turbo monorepo)
    5. nx.json (Nx workspace)

    Returns WorkspaceInfo with is_workspace=False if none detected.
    """
    pnpm_workspace = repo_root / "pnpm-workspace.yaml"
    if pnpm_workspace.exists():
        return WorkspaceInfo(
            is_workspace=True,
            manager="pnpm",
            workspace_paths=_expand_workspace_globs(repo_root, _read_pnpm_globs(pnpm_workspace)),
        )

    package_json = repo_root / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text(errors="replace"))
        except json.JSONDecodeError:
            pkg = {}
        workspaces = pkg.get("workspaces")
        if workspaces:
            globs: list[str] = []
            if isinstance(workspaces, list):
                globs = [str(g) for g in workspaces]
            elif isinstance(workspaces, dict):
                packages = workspaces.get("packages", [])
                if isinstance(packages, list):
                    globs = [str(g) for g in packages]
            if globs:
                return WorkspaceInfo(
                    is_workspace=True,
                    manager="yarn",
                    workspace_paths=_expand_workspace_globs(repo_root, globs),
                )

    lerna_json = repo_root / "lerna.json"
    if lerna_json.exists():
        try:
            lerna = json.loads(lerna_json.read_text(errors="replace"))
        except json.JSONDecodeError:
            lerna = {}
        packages = lerna.get("packages") or ["packages/*"]
        if isinstance(packages, list):
            return WorkspaceInfo(
                is_workspace=True,
                manager="lerna",
                workspace_paths=_expand_workspace_globs(repo_root, [str(p) for p in packages]),
            )

    turbo_json = repo_root / "turbo.json"
    if turbo_json.exists():
        try:
            turbo = json.loads(turbo_json.read_text(errors="replace"))
        except json.JSONDecodeError:
            turbo = {}
        if "pipeline" in turbo or "tasks" in turbo:
            turbo_globs = _read_turbo_globs(turbo)
            ws_paths: list[Path] = []
            if turbo_globs:
                ws_paths = _expand_workspace_globs(repo_root, turbo_globs)
            elif package_json.exists():
                try:
                    pkg = json.loads(package_json.read_text(errors="replace"))
                except json.JSONDecodeError:
                    pkg = {}
                pkg_ws = pkg.get("workspaces")
                pkg_globs: list[str] = []
                if isinstance(pkg_ws, list):
                    pkg_globs = [str(g) for g in pkg_ws]
                elif isinstance(pkg_ws, dict):
                    packages = pkg_ws.get("packages", [])
                    if isinstance(packages, list):
                        pkg_globs = [str(g) for g in packages]
                if pkg_globs:
                    ws_paths = _expand_workspace_globs(repo_root, pkg_globs)

            return WorkspaceInfo(
                is_workspace=True,
                manager="turbo",
                workspace_paths=ws_paths,
            )

    nx_json = repo_root / "nx.json"
    if nx_json.exists():
        ws_json = repo_root / "workspace.json"
        ws_paths: list[Path] = []
        if ws_json.exists():
            try:
                ws = json.loads(ws_json.read_text(errors="replace"))
            except json.JSONDecodeError:
                ws = {}
            projects = ws.get("projects", {}) or {}
            for project_path in projects.values():
                if isinstance(project_path, str):
                    p = repo_root / project_path
                    if p.is_dir():
                        ws_paths.append(p)
        if not ws_paths:
            for sub in ("apps", "libs", "packages"):
                base = repo_root / sub
                if base.is_dir():
                    ws_paths.extend(p for p in base.iterdir() if p.is_dir())
        return WorkspaceInfo(
            is_workspace=True,
            manager="nx",
            workspace_paths=ws_paths,
        )

    return WorkspaceInfo(is_workspace=False, manager=None, workspace_paths=[])


def _read_pnpm_globs(pnpm_workspace_yaml: Path) -> list[str]:
    """Parse pnpm-workspace.yaml → list of package globs.

    Uses PyYAML when available; falls back to a minimal hand-rolled parser
    for the conventional ``packages: [- foo, - bar]`` shape so we never crash
    bootstrap even if the YAML library somehow goes missing at runtime.
    """
    try:
        text = pnpm_workspace_yaml.read_text(errors="replace")
    except OSError:
        return []

    if _yaml is not None:
        try:
            loaded = _yaml.safe_load(text)
        except _yaml.YAMLError:  # type: ignore[attr-defined]
            loaded = None
        if isinstance(loaded, dict):
            packages = loaded.get("packages")
            if isinstance(packages, list):
                return [str(p) for p in packages if isinstance(p, str | int)]

    globs: list[str] = []
    in_packages = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("packages:"):
            in_packages = True
            continue
        if in_packages:
            if stripped.startswith("- "):
                value = stripped[2:].strip().strip("'\"")
                if value:
                    globs.append(value)
            elif raw_line and not raw_line.startswith((" ", "\t", "-")):
                in_packages = False
    return globs


def _read_turbo_globs(turbo: dict) -> list[str]:
    """Extract workspace globs from a parsed turbo.json.

    Turbo accepts either ``workspaces`` or ``packages`` as a sibling of
    ``pipeline``/``tasks``. Both should be string arrays; anything else is
    ignored.
    """
    for field_name in ("workspaces", "packages"):
        value = turbo.get(field_name)
        if isinstance(value, list):
            return [str(v) for v in value if isinstance(v, str | int)]
        if isinstance(value, dict):
            packages = value.get("packages")
            if isinstance(packages, list):
                return [str(v) for v in packages if isinstance(v, str | int)]
    return []


def _expand_workspace_globs(repo_root: Path, globs: list[str]) -> list[Path]:
    """Expand workspace globs to actual sub-package directory paths.

    Each glob like "apps/*" or "packages/*" expands to all immediate sub-dirs.
    """
    paths: list[Path] = []
    seen: set[Path] = set()
    for glob in globs:
        if glob.startswith("!"):
            continue
        glob = glob.rstrip("/")
        if not glob or glob in (".", ".."):
            continue
        try:
            matches = list(repo_root.glob(glob))
        except (ValueError, IndexError):
            continue
        for p in matches:
            if p.is_dir() and p not in seen:
                if (p / "package.json").exists():
                    seen.add(p)
                    paths.append(p)
    return sorted(paths)
