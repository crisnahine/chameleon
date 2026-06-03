"""Unit tests for chameleon_mcp.bootstrap.workspace — monorepo detection.

Covers workspace/package-boundary detection (pnpm, yarn, lerna, turbo, nx),
the per-manager detection precedence, the package.json membership requirement
during glob expansion, and the related orchestrator fanout cap
(`WORKSPACE_FANOUT_CAP`, env-overridable via
`CHAMELEON_WORKSPACE_FANOUT_CAP`) that bounds the TS-monorepo
first-level scan.

The module under test reads no env vars and touches no global plugin state, but
we replicate the project's autouse isolation pattern (CHAMELEON_PLUGIN_DATA ->
tmp_path) so a stray data write can never leak into the developer's real
~/.local/share/chameleon/ directory.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import chameleon_mcp.bootstrap.orchestrator as orch
import chameleon_mcp.bootstrap.workspace as ws_module
from chameleon_mcp.bootstrap.workspace import (
    WorkspaceInfo,
    _expand_workspace_globs,
    _read_pnpm_globs,
    _read_turbo_globs,
    detect_workspace,
)

_TS_NODE_MODULES = Path(__file__).resolve().parents[2] / "mcp" / "node_modules" / "typescript"
_HAVE_TS = shutil.which("node") is not None and _TS_NODE_MODULES.is_dir()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    """Point CHAMELEON_PLUGIN_DATA at tmp_path; the module has no conn cache."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    yield


def _pkg(d: Path, body: dict | None = None) -> Path:
    """Create dir ``d`` with a package.json so glob expansion accepts it."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(json.dumps(body or {}), encoding="utf-8")
    return d


def _rel(repo: Path, info: WorkspaceInfo) -> list[str]:
    return sorted(p.relative_to(repo).as_posix() for p in info.workspace_paths)


# --------------------------------------------------------------------------- #
# WorkspaceInfo dataclass invariants
# --------------------------------------------------------------------------- #


class TestWorkspaceInfo:
    def test_has_workspaces_true_only_with_paths(self):
        info = WorkspaceInfo(is_workspace=True, manager="pnpm", workspace_paths=[Path("/a")])
        assert info.has_workspaces is True

    def test_has_workspaces_false_when_workspace_but_no_paths(self):
        info = WorkspaceInfo(is_workspace=True, manager="nx", workspace_paths=[])
        assert info.has_workspaces is False

    def test_has_workspaces_false_when_not_a_workspace(self):
        info = WorkspaceInfo(is_workspace=False, manager=None, workspace_paths=[Path("/a")])
        assert info.has_workspaces is False

    def test_default_workspace_paths_is_independent_list(self):
        a = WorkspaceInfo(is_workspace=False, manager=None)
        b = WorkspaceInfo(is_workspace=False, manager=None)
        a.workspace_paths.append(Path("/x"))
        assert b.workspace_paths == []


# --------------------------------------------------------------------------- #
# pnpm-workspace.yaml
# --------------------------------------------------------------------------- #


class TestPnpmDetection:
    def test_pnpm_expands_globs_to_packages_with_manifest(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'packages/*'\n")
        _pkg(repo / "apps/web")
        _pkg(repo / "apps/api")
        _pkg(repo / "packages/ui")
        info = detect_workspace(repo)
        assert info.is_workspace is True
        assert info.manager == "pnpm"
        assert info.has_workspaces is True
        assert _rel(repo, info) == ["apps/api", "apps/web", "packages/ui"]

    def test_glob_matched_dir_without_package_json_excluded(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
        _pkg(repo / "apps/real")
        (repo / "apps/nopkg").mkdir(parents=True)  # matched by glob, but no manifest
        info = detect_workspace(repo)
        assert _rel(repo, info) == ["apps/real"]

    def test_pnpm_with_no_matching_dirs_has_no_workspaces(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
        info = detect_workspace(repo)
        # marker present so is_workspace is True, but nothing expanded
        assert info.is_workspace is True
        assert info.manager == "pnpm"
        assert info.workspace_paths == []
        assert info.has_workspaces is False


class TestPnpmGlobParsing:
    def test_pyyaml_path_reads_packages_list(self, tmp_path: Path):
        y = tmp_path / "pnpm-workspace.yaml"
        y.write_text("packages:\n  - 'apps/*'\n  - \"packages/*\"\n")
        assert _read_pnpm_globs(y) == ["apps/*", "packages/*"]

    def test_handrolled_fallback_when_yaml_missing(self, tmp_path: Path, monkeypatch):
        # Force the PyYAML-less branch and confirm the minimal parser still works.
        monkeypatch.setattr(ws_module, "_yaml", None)
        y = tmp_path / "pnpm-workspace.yaml"
        y.write_text("# comment\npackages:\n  - 'apps/*'\n  - \"packages/*\"\nother: stop\n")
        assert _read_pnpm_globs(y) == ["apps/*", "packages/*"]

    def test_handrolled_fallback_stops_at_next_top_level_key(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ws_module, "_yaml", None)
        y = tmp_path / "pnpm-workspace.yaml"
        y.write_text("packages:\n  - 'apps/*'\ncatalog:\n  - 'should/not/appear'\n")
        assert _read_pnpm_globs(y) == ["apps/*"]

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert _read_pnpm_globs(tmp_path / "does-not-exist.yaml") == []


# --------------------------------------------------------------------------- #
# yarn workspaces (package.json)
# --------------------------------------------------------------------------- #


class TestYarnDetection:
    def test_workspaces_array_form(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({"workspaces": ["apps/*"]}))
        _pkg(repo / "apps/web")
        _pkg(repo / "apps/api")
        info = detect_workspace(repo)
        assert info.manager == "yarn"
        assert _rel(repo, info) == ["apps/api", "apps/web"]

    def test_workspaces_object_form_with_packages_key(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({"workspaces": {"packages": ["packages/*"]}}))
        _pkg(repo / "packages/core")
        info = detect_workspace(repo)
        assert info.manager == "yarn"
        assert _rel(repo, info) == ["packages/core"]

    def test_plain_package_json_is_not_a_workspace(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({"name": "single"}))
        info = detect_workspace(repo)
        assert info.is_workspace is False
        assert info.manager is None
        assert info.workspace_paths == []
        assert info.has_workspaces is False

    def test_malformed_package_json_does_not_crash(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text("{ this is not valid json ")
        info = detect_workspace(repo)
        assert info.is_workspace is False
        assert info.manager is None


# --------------------------------------------------------------------------- #
# lerna.json
# --------------------------------------------------------------------------- #


class TestLernaDetection:
    def test_lerna_without_packages_defaults_to_packages_glob(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "lerna.json").write_text(json.dumps({"version": "1.0.0"}))
        _pkg(repo / "packages/a")
        info = detect_workspace(repo)
        assert info.manager == "lerna"
        assert _rel(repo, info) == ["packages/a"]

    def test_lerna_explicit_packages(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "lerna.json").write_text(json.dumps({"packages": ["modules/*"]}))
        _pkg(repo / "modules/m1")
        info = detect_workspace(repo)
        assert info.manager == "lerna"
        assert _rel(repo, info) == ["modules/m1"]


# --------------------------------------------------------------------------- #
# turbo.json
# --------------------------------------------------------------------------- #


class TestTurboDetection:
    def test_turbo_pipeline_with_own_workspaces(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "turbo.json").write_text(json.dumps({"pipeline": {}, "workspaces": ["apps/*"]}))
        _pkg(repo / "apps/x")
        info = detect_workspace(repo)
        assert info.manager == "turbo"
        assert _rel(repo, info) == ["apps/x"]

    def test_turbo_tasks_field_also_triggers(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "turbo.json").write_text(json.dumps({"tasks": {}, "packages": ["pkg/*"]}))
        _pkg(repo / "pkg/one")
        info = detect_workspace(repo)
        assert info.manager == "turbo"
        assert _rel(repo, info) == ["pkg/one"]

    def test_turbo_without_pipeline_or_tasks_is_not_a_workspace(self, tmp_path: Path):
        # No pipeline/tasks key -> turbo branch is skipped entirely.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "turbo.json").write_text(json.dumps({"workspaces": ["apps/*"]}))
        _pkg(repo / "apps/x")
        info = detect_workspace(repo)
        assert info.is_workspace is False
        assert info.manager is None

    def test_turbo_falls_back_to_package_json_only_when_no_pkg_workspaces(self, tmp_path: Path):
        # turbo.json declares no globs; package.json has none either ->
        # turbo branch reached, ws_paths empty (no fallback source).
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "turbo.json").write_text(json.dumps({"pipeline": {}}))
        (repo / "package.json").write_text(json.dumps({"name": "root"}))
        _pkg(repo / "packages/p")
        info = detect_workspace(repo)
        assert info.manager == "turbo"
        assert info.workspace_paths == []


class TestTurboGlobParsing:
    def test_workspaces_list_coerces_int_members(self):
        assert _read_turbo_globs({"workspaces": ["apps/*", 1]}) == ["apps/*", "1"]

    def test_packages_list_used_when_no_workspaces(self):
        assert _read_turbo_globs({"packages": ["pkg/*"]}) == ["pkg/*"]

    def test_nested_dict_packages(self):
        assert _read_turbo_globs({"workspaces": {"packages": ["w/*"]}}) == ["w/*"]

    def test_no_glob_fields_returns_empty(self):
        assert _read_turbo_globs({"pipeline": {}}) == []


# --------------------------------------------------------------------------- #
# nx.json
# --------------------------------------------------------------------------- #


class TestNxDetection:
    def test_nx_reads_workspace_json_projects_and_drops_missing(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "nx.json").write_text(json.dumps({}))
        (repo / "workspace.json").write_text(
            json.dumps({"projects": {"web": "apps/web", "gone": "apps/missing"}})
        )
        (repo / "apps/web").mkdir(parents=True)  # exists; "gone" path does not
        info = detect_workspace(repo)
        assert info.manager == "nx"
        assert _rel(repo, info) == ["apps/web"]

    def test_nx_heuristic_scans_apps_libs_packages(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "nx.json").write_text(json.dumps({}))
        (repo / "apps/a").mkdir(parents=True)
        (repo / "apps/b").mkdir(parents=True)
        (repo / "libs/c").mkdir(parents=True)
        info = detect_workspace(repo)
        assert info.manager == "nx"
        # heuristic scan does NOT require package.json in each dir
        assert _rel(repo, info) == ["apps/a", "apps/b", "libs/c"]

    def test_nx_with_nothing_is_workspace_but_no_paths(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "nx.json").write_text(json.dumps({}))
        info = detect_workspace(repo)
        assert info.is_workspace is True
        assert info.manager == "nx"
        assert info.workspace_paths == []
        assert info.has_workspaces is False


# --------------------------------------------------------------------------- #
# Detection precedence
# --------------------------------------------------------------------------- #


class TestPrecedence:
    def test_pnpm_wins_over_yarn(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'pn/*'\n")
        (repo / "package.json").write_text(json.dumps({"workspaces": ["yn/*"]}))
        _pkg(repo / "pn/a")
        _pkg(repo / "yn/b")
        info = detect_workspace(repo)
        assert info.manager == "pnpm"
        assert _rel(repo, info) == ["pn/a"]

    def test_yarn_package_json_wins_over_turbo(self, tmp_path: Path):
        # package.json workspaces are checked (precedence #2) before turbo (#4);
        # so a repo with both reports manager="yarn", not "turbo".
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "turbo.json").write_text(json.dumps({"pipeline": {}}))
        (repo / "package.json").write_text(json.dumps({"workspaces": ["packages/*"]}))
        _pkg(repo / "packages/p")
        info = detect_workspace(repo)
        assert info.manager == "yarn"
        assert _rel(repo, info) == ["packages/p"]

    def test_no_markers_returns_single_package(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        info = detect_workspace(repo)
        assert info.is_workspace is False
        assert info.manager is None
        assert info.workspace_paths == []
        assert info.has_workspaces is False


# --------------------------------------------------------------------------- #
# _expand_workspace_globs
# --------------------------------------------------------------------------- #


class TestExpandWorkspaceGlobs:
    def test_sorts_and_dedups_and_skips_negation(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _pkg(repo / "apps/z")
        _pkg(repo / "apps/a")
        _pkg(repo / "apps/m")
        # "apps/*/" (trailing slash) and "apps/*" both match the same dirs ->
        # deduped; "!apps/excluded" negation glob is skipped.
        out = _expand_workspace_globs(repo, ["apps/*/", "!apps/excluded", "apps/*"])
        assert [p.relative_to(repo).as_posix() for p in out] == ["apps/a", "apps/m", "apps/z"]

    def test_dot_and_dotdot_globs_rejected(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _pkg(repo / "apps/a")
        out = _expand_workspace_globs(repo, [".", "..", ""])
        assert out == []

    def test_only_dirs_with_package_json_kept(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _pkg(repo / "packages/withpkg")
        (repo / "packages/nopkg").mkdir(parents=True)
        # a file matching the glob is also ignored (not a dir)
        (repo / "packages/file.txt").write_text("x")
        out = _expand_workspace_globs(repo, ["packages/*"])
        assert [p.relative_to(repo).as_posix() for p in out] == ["packages/withpkg"]

    def test_brace_expansion_matches_each_alternative(self, tmp_path: Path):
        # pnpm/turbo accept shell-style brace expansion; Path.glob() does not.
        repo = tmp_path / "repo"
        repo.mkdir()
        _pkg(repo / "packages/a")
        _pkg(repo / "packages/b")
        _pkg(repo / "packages/c")
        out = _expand_workspace_globs(repo, ["packages/{a,b,c}"])
        assert [p.relative_to(repo).as_posix() for p in out] == [
            "packages/a",
            "packages/b",
            "packages/c",
        ]

    def test_brace_expansion_with_trailing_wildcard(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _pkg(repo / "apps/web/client")
        _pkg(repo / "services/api/server")
        out = _expand_workspace_globs(repo, ["{apps,services}/*/*"])
        assert [p.relative_to(repo).as_posix() for p in out] == [
            "apps/web/client",
            "services/api/server",
        ]


# --------------------------------------------------------------------------- #
# Glob diagnostics: surfaced failures + zero-match warnings
# --------------------------------------------------------------------------- #


class TestExpandWorkspaceGlobsDiagnostics:
    def test_brace_expansion_flows_through_detect_workspace(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/{a,b,c}'\n")
        _pkg(repo / "packages/a")
        _pkg(repo / "packages/b")
        _pkg(repo / "packages/c")
        info = detect_workspace(repo)
        assert info.has_workspaces is True
        assert _rel(repo, info) == ["packages/a", "packages/b", "packages/c"]


class TestWorkspaceGlobWarnings:
    def test_failed_glob_collected(self, tmp_path: Path):
        from chameleon_mcp.bootstrap.workspace import expand_workspace_globs_with_diagnostics

        repo = tmp_path / "repo"
        repo.mkdir()
        _pkg(repo / "apps/web")
        # "apps/*" matches; the absolute glob raises inside Path.glob ->
        # recorded as a failed-glob warning instead of vanishing.
        result = expand_workspace_globs_with_diagnostics(repo, ["apps/*", "/abs/glob"])
        assert [p.relative_to(repo).as_posix() for p in result.paths] == ["apps/web"]
        assert any("/abs/glob" in w for w in result.glob_warnings)

    def test_zero_match_glob_recorded(self, tmp_path: Path):
        from chameleon_mcp.bootstrap.workspace import expand_workspace_globs_with_diagnostics

        repo = tmp_path / "repo"
        repo.mkdir()
        # "missing/*" matches nothing on disk -> a zero-match warning so a
        # misconfigured workspace pattern is visible, not silent.
        result = expand_workspace_globs_with_diagnostics(repo, ["missing/*"])
        assert result.paths == []
        assert any("missing/*" in w for w in result.glob_warnings)

    def test_matched_dir_without_package_json_recorded(self, tmp_path: Path):
        from chameleon_mcp.bootstrap.workspace import expand_workspace_globs_with_diagnostics

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "apps/docs").mkdir(parents=True)  # matches glob, no package.json
        result = expand_workspace_globs_with_diagnostics(repo, ["apps/*"])
        assert result.paths == []
        # Directory matched but lacked a package.json -> surfaced so the user
        # can fix the misconfigured package.
        assert any("apps/docs" in p for p in result.potential_workspace_paths)

    def test_clean_globs_produce_no_warnings(self, tmp_path: Path):
        from chameleon_mcp.bootstrap.workspace import expand_workspace_globs_with_diagnostics

        repo = tmp_path / "repo"
        repo.mkdir()
        _pkg(repo / "apps/web")
        result = expand_workspace_globs_with_diagnostics(repo, ["apps/*"])
        assert [p.relative_to(repo).as_posix() for p in result.paths] == ["apps/web"]
        assert result.glob_warnings == []
        assert result.potential_workspace_paths == []

    def test_warnings_surfaced_on_workspace_info(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'missing/*'\n")
        _pkg(repo / "apps/web")
        (repo / "missing").mkdir()  # exists but no children match missing/*
        info = detect_workspace(repo)
        assert _rel(repo, info) == ["apps/web"]
        assert any("missing/*" in w for w in info.glob_warnings)


# --------------------------------------------------------------------------- #
# Orchestrator fanout cap (_WORKSPACE_FANOUT_CAP)
# --------------------------------------------------------------------------- #


def _ts_monorepo(repo: Path, parent: str, n: int) -> None:
    """Root pkg.json with no TS signal + n TS workspaces under ``parent``."""
    (repo / "package.json").write_text(json.dumps({"scripts": {"build": "turbo run build"}}))
    for i in range(n):
        d = repo / parent / f"app{i:02d}"
        d.mkdir(parents=True)
        (d / "tsconfig.json").write_text("{}")


class TestFanoutCap:
    def test_all_ts_workspaces_found_under_cap(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _ts_monorepo(repo, "apps", 4)
        roots, capped = orch._detect_workspace_ts_monorepo(repo)
        assert capped is False
        assert roots == ["apps/app00", "apps/app01", "apps/app02", "apps/app03"]

    def test_cap_truncates_sorted_entries_and_sets_flag(self, tmp_path: Path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        _ts_monorepo(repo, "apps", 5)
        # The live cap is read from _thresholds at call time, so set the
        # documented env override rather than the back-compat module constant.
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "3")
        roots, capped = orch._detect_workspace_ts_monorepo(repo)
        assert capped is True
        # entries are sorted before truncation -> first 3 by name survive
        assert roots == ["apps/app00", "apps/app01", "apps/app02"]

    def test_entries_equal_to_cap_do_not_trip_flag(self, tmp_path: Path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        _ts_monorepo(repo, "apps", 3)
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "3")
        roots, capped = orch._detect_workspace_ts_monorepo(repo)
        assert capped is False  # cap fires only when len(entries) > cap
        assert roots == ["apps/app00", "apps/app01", "apps/app02"]

    def test_root_tsconfig_short_circuits_as_single_repo(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({}))
        (repo / "tsconfig.json").write_text("{}")
        d = repo / "apps/a"
        d.mkdir(parents=True)
        (d / "tsconfig.json").write_text("{}")
        assert orch._detect_workspace_ts_monorepo(repo) == ([], False)

    def test_root_pkg_with_ts_token_short_circuits(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({"devDependencies": {"typescript": "5"}}))
        d = repo / "apps/a"
        d.mkdir(parents=True)
        (d / "tsconfig.json").write_text("{}")
        assert orch._detect_workspace_ts_monorepo(repo) == ([], False)

    def test_no_root_package_json_returns_empty(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert orch._detect_workspace_ts_monorepo(repo) == ([], False)

    def test_non_ts_workspaces_excluded_from_results(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({}))
        ts = repo / "packages/tsone"
        ts.mkdir(parents=True)
        (ts / "tsconfig.json").write_text("{}")
        plain = repo / "packages/plain"
        plain.mkdir(parents=True)
        (plain / "package.json").write_text(json.dumps({"name": "x"}))
        roots, capped = orch._detect_workspace_ts_monorepo(repo)
        assert roots == ["packages/tsone"]
        assert capped is False

    def test_env_override_changes_fanout_cap(self, tmp_path: Path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        _ts_monorepo(repo, "apps", 5)
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "2")
        roots, capped = orch._detect_workspace_ts_monorepo(repo)
        # The override is honored: the scan truncates to 2 and flags it.
        assert capped is True
        assert roots == ["apps/app00", "apps/app01"]


class TestIsTsWorkspace:
    def test_tsconfig_wins(self, tmp_path: Path):
        d = tmp_path / "a"
        d.mkdir()
        (d / "tsconfig.json").write_text("{}")
        assert orch._is_ts_workspace(d) is True

    def test_package_json_ts_token(self, tmp_path: Path):
        d = tmp_path / "b"
        d.mkdir()
        (d / "package.json").write_text(json.dumps({"dependencies": {"vite": "5"}}))
        assert orch._is_ts_workspace(d) is True

    def test_plain_package_json_is_not_ts(self, tmp_path: Path):
        d = tmp_path / "c"
        d.mkdir()
        (d / "package.json").write_text(json.dumps({"name": "plain"}))
        assert orch._is_ts_workspace(d) is False

    def test_empty_dir_is_not_ts(self, tmp_path: Path):
        d = tmp_path / "d"
        d.mkdir()
        assert orch._is_ts_workspace(d) is False


# --------------------------------------------------------------------------- #
# Persisted coordination metadata: workspace_roots survives a profile reload
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
class TestWorkspaceRootsPersistence:
    def test_workspace_roots_written_to_profile_json(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
        repo = tmp_path / "repo"
        repo.mkdir()
        # Turborepo shape: the root package.json declares no TS dependency, and
        # each workspace under apps/ carries its own package.json (so the root
        # extractor defers to the per-workspace fan-out). The TS-monorepo scan
        # then populates workspace_roots, which must persist for the reload path.
        (repo / "package.json").write_text(json.dumps({"name": "root"}))
        for name in ("web", "api"):
            ws = repo / "apps" / name
            ws.mkdir(parents=True)
            (ws / "package.json").write_text(json.dumps({"devDependencies": {"typescript": "5"}}))
            (ws / "tsconfig.json").write_text("{}")
            (ws / "main.ts").write_text("export const x = 1;\n")

        report = orch.bootstrap_repo(repo)
        assert report.workspace_roots == ["apps/api", "apps/web"]

        profile_json = json.loads((repo / ".chameleon" / "profile.json").read_text())
        assert profile_json["workspace"]["workspace_roots"] == ["apps/api", "apps/web"]
