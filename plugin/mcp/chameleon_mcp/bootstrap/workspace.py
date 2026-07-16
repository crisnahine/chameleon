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

from chameleon_mcp.bootstrap.discovery import _expand_brace_groups

try:
    import yaml as _yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — defensive; PyYAML is a hard dep.
    _yaml = None  # type: ignore[assignment]


@dataclass
class GlobExpansion:
    """Result of expanding workspace globs, with diagnostics.

    ``glob_warnings`` records globs that raised inside Path.glob or matched
    nothing usable, so a misconfigured workspace pattern is visible instead
    of silently dropping packages. ``potential_workspace_paths`` lists dirs
    that matched a glob but lacked a package.json, so the user can fix a
    package missing its manifest.
    """

    paths: list[Path] = field(default_factory=list)
    glob_warnings: list[str] = field(default_factory=list)
    potential_workspace_paths: list[str] = field(default_factory=list)


@dataclass
class WorkspaceInfo:
    """Detected workspace structure (or single-package if no workspace markers)."""

    is_workspace: bool
    manager: str | None
    workspace_paths: list[Path] = field(default_factory=list)
    """Sub-package roots (e.g., apps/web, packages/ui). Each is a candidate for
    its own per-workspace .chameleon/ profile."""

    glob_warnings: list[str] = field(default_factory=list)
    """Globs that raised during expansion or matched zero usable packages.

    Surfaced so a misconfigured workspace pattern (brace syntax a glob engine
    rejects, a typo'd directory, a package missing its manifest) is reported
    instead of silently expanding to nothing.
    """

    potential_workspace_paths: list[str] = field(default_factory=list)
    """Repo-relative dirs that matched a glob but had no package.json.

    These look like intended workspace packages but were excluded for lacking
    a manifest. Surfaced so the user can add the missing package.json.
    """

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
        pnpm_globs = _read_pnpm_globs(pnpm_workspace)
        if pnpm_globs:
            exp = expand_workspace_globs_with_diagnostics(repo_root, pnpm_globs)
            return WorkspaceInfo(
                is_workspace=True,
                manager="pnpm",
                workspace_paths=exp.paths,
                glob_warnings=exp.glob_warnings,
                potential_workspace_paths=exp.potential_workspace_paths,
            )
        # No `packages:` key at all (as opposed to one whose glob matched zero
        # dirs, handled above): modern pnpm (9/10) commonly repurposes this
        # file for global settings (minimumReleaseAge, allowBuilds, overrides,
        # onlyBuiltDependencies, patchedDependencies) with no packages
        # declared. That shape is a single package, not a pnpm workspace, so
        # fall through to the other markers instead of reporting is_workspace
        # for a manager with zero resolvable packages.

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
                exp = expand_workspace_globs_with_diagnostics(repo_root, globs)
                return WorkspaceInfo(
                    is_workspace=True,
                    manager="yarn",
                    workspace_paths=exp.paths,
                    glob_warnings=exp.glob_warnings,
                    potential_workspace_paths=exp.potential_workspace_paths,
                )

    lerna_json = repo_root / "lerna.json"
    if lerna_json.exists():
        try:
            lerna = json.loads(lerna_json.read_text(errors="replace"))
        except json.JSONDecodeError:
            lerna = {}
        packages = lerna.get("packages") or ["packages/*"]
        if isinstance(packages, list):
            exp = expand_workspace_globs_with_diagnostics(repo_root, [str(p) for p in packages])
            return WorkspaceInfo(
                is_workspace=True,
                manager="lerna",
                workspace_paths=exp.paths,
                glob_warnings=exp.glob_warnings,
                potential_workspace_paths=exp.potential_workspace_paths,
            )

    turbo_json = repo_root / "turbo.json"
    if turbo_json.exists():
        try:
            turbo = json.loads(turbo_json.read_text(errors="replace"))
        except json.JSONDecodeError:
            turbo = {}
        if "pipeline" in turbo or "tasks" in turbo:
            turbo_globs = _read_turbo_globs(turbo)
            exp = GlobExpansion()
            if turbo_globs:
                exp = expand_workspace_globs_with_diagnostics(repo_root, turbo_globs)
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
                    exp = expand_workspace_globs_with_diagnostics(repo_root, pkg_globs)

            return WorkspaceInfo(
                is_workspace=True,
                manager="turbo",
                workspace_paths=exp.paths,
                glob_warnings=exp.glob_warnings,
                potential_workspace_paths=exp.potential_workspace_paths,
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


def expand_workspace_globs_with_diagnostics(repo_root: Path, globs: list[str]) -> GlobExpansion:
    """Expand workspace globs and collect diagnostics about failures.

    Returns a GlobExpansion carrying the resolved package paths plus two
    diagnostic lists: globs that raised or matched nothing usable, and dirs
    that matched a glob but lacked a package.json. Surfacing these prevents a
    misconfigured workspace pattern from silently dropping packages.
    """
    paths: list[Path] = []
    seen: set[Path] = set()
    warnings: list[str] = []
    potential: list[str] = []
    potential_seen: set[str] = set()

    for raw_glob in globs:
        if raw_glob.startswith("!"):
            continue
        glob = raw_glob.rstrip("/")
        if not glob or glob in (".", ".."):
            continue

        # Brace expansion ("packages/{a,b}") is standard in pnpm/turbo configs
        # but Path.glob treats braces as literal characters, so expand first.
        expanded = _expand_brace_groups(glob)

        glob_matched_any = False
        glob_kept_any = False
        glob_raised = False
        for pattern in expanded:
            try:
                matches = list(repo_root.glob(pattern))
            except (ValueError, IndexError, NotImplementedError, OSError) as exc:
                # Absolute patterns raise NotImplementedError; malformed
                # patterns raise ValueError/IndexError. Record and continue so
                # one bad glob never aborts the whole workspace fan-out.
                glob_raised = True
                warnings.append(f"{raw_glob!r}: invalid glob ({exc})")
                continue
            for p in matches:
                if not p.is_dir():
                    continue
                glob_matched_any = True
                if (p / "package.json").exists():
                    if p not in seen:
                        seen.add(p)
                        paths.append(p)
                    glob_kept_any = True
                else:
                    rel = _rel_label(p, repo_root)
                    if rel not in potential_seen:
                        potential_seen.add(rel)
                        potential.append(rel)

        if glob_raised:
            # The invalid-glob warning above already explains the failure.
            continue
        if not glob_matched_any:
            warnings.append(f"{raw_glob!r}: matched no directories")
        elif not glob_kept_any:
            warnings.append(f"{raw_glob!r}: matched directories but none had a package.json")

    return GlobExpansion(
        paths=sorted(paths),
        glob_warnings=warnings,
        potential_workspace_paths=sorted(potential),
    )


def _rel_label(p: Path, repo_root: Path) -> str:
    """Repo-relative POSIX label for a path, falling back to the absolute path."""
    try:
        return p.relative_to(repo_root).as_posix()
    except ValueError:
        return str(p)
