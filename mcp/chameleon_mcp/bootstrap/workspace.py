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

try:  # PyYAML ships transitively (detect-secrets) and is declared as a direct dep.
    import yaml as _yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — defensive; PyYAML is a hard dep.
    _yaml = None  # type: ignore[assignment]


@dataclass
class WorkspaceInfo:
    """Detected workspace structure (or single-package if no workspace markers)."""

    is_workspace: bool
    manager: str | None  # "pnpm" | "yarn" | "lerna" | "turbo" | "nx" | None
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
    # 1. pnpm
    pnpm_workspace = repo_root / "pnpm-workspace.yaml"
    if pnpm_workspace.exists():
        return WorkspaceInfo(
            is_workspace=True,
            manager="pnpm",
            workspace_paths=_expand_workspace_globs(repo_root, _read_pnpm_globs(pnpm_workspace)),
        )

    # 2. yarn workspaces
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

    # 3. lerna
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

    # 4. turbo (only if pipeline/tasks exists; turbo can also be added to single-package repos)
    turbo_json = repo_root / "turbo.json"
    if turbo_json.exists():
        try:
            turbo = json.loads(turbo_json.read_text(errors="replace"))
        except json.JSONDecodeError:
            turbo = {}
        if "pipeline" in turbo or "tasks" in turbo:
            # Turbo monorepo. Modern turbo (>=1.10) lets you declare workspace
            # roots directly in turbo.json via `workspaces` / `packages`. Older
            # versions assume `package.json` workspaces exist. We try the
            # turbo.json field first, then fall back to package.json.
            turbo_globs = _read_turbo_globs(turbo)
            ws_paths: list[Path] = []
            if turbo_globs:
                ws_paths = _expand_workspace_globs(repo_root, turbo_globs)
            elif package_json.exists():
                # Re-read package.json defensively even if the yarn branch
                # above didn't see workspaces (the field could exist alongside
                # turbo without yarn's hoisting flag).
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

    # 5. nx
    nx_json = repo_root / "nx.json"
    if nx_json.exists():
        # Nx workspaces declared in `workspace.json` or `apps/`+`libs/` convention
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
        # Fallback to apps/+libs/ if workspace.json absent or empty
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

    # No workspace markers detected
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
        # Fall through to the legacy parser if PyYAML gave us something
        # unexpected (e.g. a top-level scalar).

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
                # Top-level key after packages — exit list
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
        # Strip negation patterns (yarn's "!packages/excluded")
        if glob.startswith("!"):
            continue
        # Trailing slash normalization
        glob = glob.rstrip("/")
        # Reject empty / pure-"." / pure-".." entries before passing them
        # to Path.glob. Python 3.11's pathlib raises IndexError("tuple
        # index out of range") inside `_make_selector` on an empty pattern
        # tuple, and "." / ".." are degenerate-but-legal workspace entries
        # in some real-world manifests (e.g., mastodon's
        # `"workspaces": [".", "streaming"]` declares the repo root as a
        # workspace). Treat "." as "the repo root is itself a workspace"
        # — already handled by the orchestrator's root pass, so skip here.
        if not glob or glob in (".", ".."):
            continue
        try:
            matches = list(repo_root.glob(glob))
        except (ValueError, IndexError):
            # Defensive: malformed glob shouldn't crash bootstrap.
            continue
        for p in matches:
            if p.is_dir() and p not in seen:
                # Workspace must contain a package.json to be a real package
                if (p / "package.json").exists():
                    seen.add(p)
                    paths.append(p)
    return sorted(paths)
