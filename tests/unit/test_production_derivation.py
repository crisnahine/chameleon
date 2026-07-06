"""Production-ref derivation: init/refresh analyze the locked production
branch's tree, regardless of which branch is checked out.

These drive the REAL tools.bootstrap_repo / tools.refresh_repo against tiny
git repos (real node ts_dump parse, no mocks) and pin:

  - a locked production_ref makes bootstrap derive from the production
    tree, not the checkout (feature-branch files invisible to the profile)
  - the explicit bootstrap production_ref param locks + persists
  - auto-lock engages only for origin-backed detection (clones), never for
    local-only repos (the entire existing fixture fleet keeps working-tree
    semantics)
  - refresh under a lock is tip-SHA based: noop when the production tip is
    unchanged (working-tree churn is irrelevant), full re-derive when the
    tip moves
  - refresh migrates old profiles (no production_ref in config): detects,
    persists, re-derives
  - the materialized worktree is always cleaned up

Repos live under tmp_path, so CHAMELEON_ALLOW_TMP_REPO=1 (the documented
test-suite opt-out) and CHAMELEON_PLUGIN_DATA isolation apply.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp import tools


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import index_db
    from chameleon_mcp.profile import loader as _loader

    # index_db caches a module-level connection that ignores the env once
    # opened; drop it so each test gets a db under its own tmp data dir.
    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()
    yield
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


_SERVICE_BODY = """export class {name}Service {{
  run(input: string): string {{
    return input.trim()
  }}
}}
"""

_LEGACY_BODY = """export function legacy{name}(value: number): number {{
  return value * {mult}
}}
"""


def _make_production_repo(root: Path) -> Path:
    """Git repo on branch `production` with committed TS services."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "production")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "package.json").write_text('{"name": "fixture", "private": true}\n', encoding="utf-8")
    (root / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}\n', encoding="utf-8")
    # Mirror the common deployment: the profile is per-checkout local state,
    # so `git add -A` in later test steps must never capture it.
    (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
    for name in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"):
        p = root / "src" / "services" / f"{name.lower()}Service.ts"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_SERVICE_BODY.format(name=name), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "production baseline")
    return root


def _diverge_on_feature(root: Path) -> None:
    """Checkout a feature branch and commit files production does not have."""
    _git(root, "checkout", "-q", "-b", "feature-x")
    for i, name in enumerate(("One", "Two", "Three")):
        p = root / "src" / "legacy" / f"legacy{name}.ts"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_LEGACY_BODY.format(name=name, mult=i + 2), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feature-only files")


def _lock_config(root: Path, branch: str) -> None:
    cham = root / ".chameleon"
    cham.mkdir(exist_ok=True)
    (cham / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.8.0", "production_ref": branch}),
        encoding="utf-8",
    )


def _exports_paths(root: Path) -> set[str]:
    data = json.loads((root / ".chameleon" / "exports_index.json").read_text(encoding="utf-8"))
    # keys are repo-relative POSIX paths
    return set(data.get("files", data).keys()) if isinstance(data, dict) else set()


def _profile_json(root: Path) -> dict:
    return json.loads((root / ".chameleon" / "profile.json").read_text(encoding="utf-8"))


def _config_json(root: Path) -> dict:
    return json.loads((root / ".chameleon" / "config.json").read_text(encoding="utf-8"))


class TestBootstrapDerivesFromProductionTree:
    def test_locked_config_derives_from_production_not_checkout(self, tmp_path: Path) -> None:
        repo = _make_production_repo(tmp_path / "repo")
        prod_sha = _git(repo, "rev-parse", "production")
        _diverge_on_feature(repo)
        _lock_config(repo, "production")

        env = tools.bootstrap_repo(str(repo))
        data = env["data"]
        assert data["status"] == "success"

        prod_block = data["production_ref"]
        assert prod_block["locked"] is True
        assert prod_block["branch"] == "production"
        assert prod_block["sha"] == prod_sha

        prof = _profile_json(repo)
        assert prof["derivation_source"]["sha"] == prod_sha
        assert prof["derivation_source"]["branch"] == "production"

        listed = json.dumps(
            json.loads((repo / ".chameleon" / "function_catalog.json").read_text(encoding="utf-8"))
        )
        assert "alphaService.ts" in listed or "services" in listed
        assert "legacyOne" not in listed

        # materialized tree cleaned up
        data_dir = Path(tools.plugin_data_dir()) if hasattr(tools, "plugin_data_dir") else None
        if data_dir is not None:
            leftovers = list(data_dir.glob("*/prodtree/*"))
            assert leftovers == []

    def test_explicit_param_locks_and_persists(self, tmp_path: Path) -> None:
        repo = _make_production_repo(tmp_path / "repo")
        prod_sha = _git(repo, "rev-parse", "production")
        _diverge_on_feature(repo)

        env = tools.bootstrap_repo(str(repo), production_ref="production")
        data = env["data"]
        assert data["status"] == "success"
        assert data["production_ref"]["locked"] is True
        assert _config_json(repo)["production_ref"] == "production"
        assert _profile_json(repo)["derivation_source"]["sha"] == prod_sha

    def test_local_only_repo_does_not_autolock(self, tmp_path: Path) -> None:
        repo = _make_production_repo(tmp_path / "repo")
        # rename to a default-y name so detection has a hit, but local-only
        _git(repo, "branch", "-m", "production", "main")
        _diverge_on_feature(repo)

        env = tools.bootstrap_repo(str(repo))
        data = env["data"]
        assert data["status"] == "success"
        assert data["production_ref"]["locked"] is False
        cfg = _config_json(repo) if (repo / ".chameleon" / "config.json").is_file() else {}
        assert "production_ref" not in cfg
        # working-tree derivation: feature-only files ARE in the profile
        listed = (repo / ".chameleon" / "function_catalog.json").read_text(encoding="utf-8")
        assert "legacyOne" in listed

    def test_unresolvable_lock_falls_back_to_working_tree(self, tmp_path: Path) -> None:
        repo = _make_production_repo(tmp_path / "repo")
        _diverge_on_feature(repo)
        _lock_config(repo, "branch-that-never-existed")

        env = tools.bootstrap_repo(str(repo))
        data = env["data"]
        assert data["status"] == "success"
        block = data["production_ref"]
        assert block["locked"] is False
        assert "did not resolve" in block["note"]
        # Working-tree derivation: the feature-branch files made it in.
        listed = (repo / ".chameleon" / "function_catalog.json").read_text(encoding="utf-8")
        assert "legacyOne" in listed
        assert "derivation_source" not in _profile_json(repo)

    def test_persist_preserves_unknown_config_keys(self, tmp_path: Path) -> None:
        repo = _make_production_repo(tmp_path / "repo")
        cham = repo / ".chameleon"
        cham.mkdir(exist_ok=True)
        (cham / "config.json").write_text(
            json.dumps({"future_feature_key": {"x": 1}, "auto_rename": False}),
            encoding="utf-8",
        )
        tools._persist_production_ref(repo, "production")
        cfg = _config_json(repo)
        assert cfg["production_ref"] == "production"
        assert cfg["future_feature_key"] == {"x": 1}
        assert cfg["auto_rename"] is False

    def test_clone_autolocks_from_origin_head(self, tmp_path: Path) -> None:
        source = _make_production_repo(tmp_path / "src")
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(source), str(clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        env = tools.bootstrap_repo(str(clone))
        data = env["data"]
        assert data["status"] == "success"
        assert data["production_ref"]["locked"] is True
        assert data["production_ref"]["branch"] == "production"
        assert _config_json(clone)["production_ref"] == "production"


class TestRefreshUnderLock:
    def _bootstrapped_locked_repo(self, tmp_path: Path) -> Path:
        repo = _make_production_repo(tmp_path / "repo")
        _diverge_on_feature(repo)
        _lock_config(repo, "production")
        env = tools.bootstrap_repo(str(repo))
        assert env["data"]["status"] == "success"
        return repo

    def test_noop_when_tip_unchanged_despite_worktree_churn(self, tmp_path: Path) -> None:
        repo = self._bootstrapped_locked_repo(tmp_path)
        churn = repo / "src" / "legacy" / "legacyOne.ts"
        churn.write_text(churn.read_text(encoding="utf-8") + "\n// churn\n", encoding="utf-8")

        env = tools.refresh_repo(str(repo))
        data = env["data"]
        assert data["status"] == "noop"
        assert "production" in data["reason"]

    def test_rederives_when_production_tip_moves(self, tmp_path: Path) -> None:
        repo = self._bootstrapped_locked_repo(tmp_path)
        _git(repo, "checkout", "-q", "production")
        p = repo / "src" / "services" / "etaService.ts"
        p.write_text(_SERVICE_BODY.format(name="Eta"), encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "new production service")
        new_sha = _git(repo, "rev-parse", "production")
        _git(repo, "checkout", "-q", "feature-x")

        env = tools.refresh_repo(str(repo))
        data = env["data"]
        assert data["status"] in ("success", "partial_refresh")
        assert _profile_json(repo)["derivation_source"]["sha"] == new_sha
        listed = (repo / ".chameleon" / "function_catalog.json").read_text(encoding="utf-8")
        assert "etaService" in listed or "EtaService" in listed

    def test_migration_locks_old_profile_on_refresh(self, tmp_path: Path) -> None:
        source = _make_production_repo(tmp_path / "src")
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(source), str(clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        env = tools.bootstrap_repo(str(clone))
        assert env["data"]["status"] == "success"

        # Simulate a pre-feature profile: strip the lock + provenance.
        cfg_path = clone / ".chameleon" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.pop("production_ref", None)
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        prof_path = clone / ".chameleon" / "profile.json"
        prof = json.loads(prof_path.read_text(encoding="utf-8"))
        prof.pop("derivation_source", None)
        prof_path.write_text(json.dumps(prof, indent=2, sort_keys=True), encoding="utf-8")

        env = tools.refresh_repo(str(clone))
        data = env["data"]
        assert data["status"] in ("success", "partial_refresh")
        assert _config_json(clone)["production_ref"] == "production"
        assert "derivation_source" in _profile_json(clone)

    def test_deleted_lock_branch_falls_back_without_crash(self, tmp_path: Path) -> None:
        repo = self._bootstrapped_locked_repo(tmp_path)
        _git(repo, "branch", "-D", "production")

        env = tools.refresh_repo(str(repo))
        assert env["data"]["status"] in ("noop", "success", "partial_refresh")
        # The unresolvable lock is surfaced, not silently dropped.
        if env["data"]["status"] == "noop":
            assert env["data"]["production_ref"]["resolvable"] is False

    def test_explicit_null_optout_blocks_autolock_and_migration(self, tmp_path: Path) -> None:
        source = _make_production_repo(tmp_path / "src")
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(source), str(clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        cham = clone / ".chameleon"
        cham.mkdir(exist_ok=True)
        (cham / "config.json").write_text(
            json.dumps({"$schema": "chameleon-config-0.9.0", "production_ref": None}),
            encoding="utf-8",
        )
        env = tools.bootstrap_repo(str(clone))
        assert env["data"]["status"] == "success"
        assert env["data"]["production_ref"]["locked"] is False
        assert _config_json(clone)["production_ref"] is None

        env = tools.refresh_repo(str(clone))
        assert env["data"]["status"] in ("noop", "success", "partial_refresh")
        # Migration must respect the explicit opt-out.
        assert _config_json(clone)["production_ref"] is None

    def test_pinned_noop_yields_to_fresh_idioms(self, tmp_path: Path) -> None:
        import time as _time

        repo = self._bootstrapped_locked_repo(tmp_path)
        idioms = repo / ".chameleon" / "idioms.md"
        _time.sleep(1.1)  # ensure mtime advances past the index snapshot
        idioms.write_text(
            idioms.read_text(encoding="utf-8") + "\n- ALWAYS use the ApiClient wrapper\n",
            encoding="utf-8",
        )
        env = tools.refresh_repo(str(repo))
        # A taught idiom must trigger a re-derive (folding it into summary +
        # trust snapshot), not be swallowed by the tip-unchanged noop.
        assert env["data"]["status"] in ("success", "partial_refresh")
        assert "ApiClient" in (repo / ".chameleon" / "idioms.md").read_text(encoding="utf-8")


class TestWorkspaceMappingUnderPinning:
    def test_monorepo_workspaces_write_to_real_checkout(self, tmp_path: Path) -> None:
        root = tmp_path / "mono"
        root.mkdir(parents=True)
        _git(root, "init", "-q", "-b", "production")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "tester")
        _git(root, "config", "commit.gpgsign", "false")
        (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
        (root / "package.json").write_text(
            json.dumps({"name": "mono", "private": True, "workspaces": ["packages/*"]}),
            encoding="utf-8",
        )
        (root / "tsconfig.json").write_text("{}", encoding="utf-8")
        (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n", encoding="utf-8")
        for pkg in ("alpha", "beta"):
            pdir = root / "packages" / pkg
            (pdir / "src" / "services").mkdir(parents=True)
            (pdir / "package.json").write_text(json.dumps({"name": pkg}), encoding="utf-8")
            (pdir / "tsconfig.json").write_text("{}", encoding="utf-8")
            for name in ("One", "Two", "Three", "Four", "Five", "Six"):
                (pdir / "src" / "services" / f"{pkg}{name}Service.ts").write_text(
                    _SERVICE_BODY.format(name=f"{pkg.title()}{name}"), encoding="utf-8"
                )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "mono baseline")
        _git(root, "checkout", "-q", "-b", "feature-y")
        _lock_config(root, "production")

        env = tools.bootstrap_repo(str(root))
        data = env["data"]
        assert data["status"] in ("success", "success_workspaces_only")
        assert data["production_ref"]["locked"] is True

        for ws in data.get("workspaces") or []:
            if ws.get("status") != "success":
                continue
            # Identity + write paths must point at the REAL checkout, and the
            # internal analysis_root must not leak into the envelope.
            assert "analysis_root" not in ws
            assert str(root) in ws["repo_root"]
            assert "prodtree" not in ws["repo_root"]
            assert Path(ws["repo_root"], ".chameleon", "profile.json").is_file()
        # No worktree paths anywhere in the envelope.
        assert "prodtree" not in json.dumps(data)


class TestSurfacing:
    def _locked_repo_with_moved_tip(self, tmp_path: Path) -> tuple[Path, str]:
        repo = _make_production_repo(tmp_path / "repo")
        _diverge_on_feature(repo)
        _lock_config(repo, "production")
        env = tools.bootstrap_repo(str(repo))
        assert env["data"]["status"] == "success"
        _git(repo, "checkout", "-q", "production")
        p = repo / "src" / "services" / "thetaService.ts"
        p.write_text(_SERVICE_BODY.format(name="Theta"), encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "tip move")
        new_sha = _git(repo, "rev-parse", "production")
        _git(repo, "checkout", "-q", "feature-x")
        return repo, new_sha

    def test_drift_status_reports_tip_moved(self, tmp_path: Path) -> None:
        from chameleon_mcp.profile.trust import grant_trust

        repo, new_sha = self._locked_repo_with_moved_tip(tmp_path)
        # Trust the profile so the freshness ladder reaches the production
        # rung (no-trust outranks tip-moved, by design).
        grant_trust(tools._compute_repo_id(repo), repo / ".chameleon")
        env = tools.get_drift_status(str(repo))
        block = env["data"]["production_ref"]
        assert block["tip_moved"] is True
        assert block["tip_sha"] == new_sha
        assert block["commits_ahead"] == 1
        assert "production" in env["data"]["recommended_action"]

    def test_drift_status_quiet_when_tip_unchanged(self, tmp_path: Path) -> None:
        repo = _make_production_repo(tmp_path / "repo")
        _diverge_on_feature(repo)
        _lock_config(repo, "production")
        env = tools.bootstrap_repo(str(repo))
        assert env["data"]["status"] == "success"
        env = tools.get_drift_status(str(repo))
        block = env["data"]["production_ref"]
        assert block.get("tip_moved") is not True
        assert block["resolvable"] is True

    def test_detect_repo_reports_lock_state(self, tmp_path: Path) -> None:
        repo = _make_production_repo(tmp_path / "repo")
        _lock_config(repo, "production")
        env = tools.detect_repo(str(repo / "package.json"))
        pb = env["data"]["production_branch"]
        assert pb["locked"] is True
        assert pb["branch"] == "production"

    def test_detect_repo_reports_detection_when_unlocked(self, tmp_path: Path) -> None:
        repo = _make_production_repo(tmp_path / "repo")
        (repo / ".chameleon").mkdir(exist_ok=True)
        env = tools.detect_repo(str(repo / "package.json"))
        pb = env["data"]["production_branch"]
        assert pb["locked"] is False
        assert pb["branch"] == "production"
        assert pb["from_origin"] is False

    def test_production_banner_fires_on_moved_tip(self, tmp_path: Path, monkeypatch) -> None:
        from chameleon_mcp import hook_helper

        repo, new_sha = self._locked_repo_with_moved_tip(tmp_path)
        banner = hook_helper._production_tip_banner(repo)
        assert banner is not None
        assert new_sha[:12] in banner
        assert "/chameleon-refresh" in banner
        # TTL marker suppresses an immediate repeat
        assert hook_helper._production_tip_banner(repo) is None


class TestSymlinkedDataDir:
    """Symlinked data-dir components must not poison committed artifacts.

    The extractors emit fully RESOLVED file paths, so the prodtree scan
    root must be resolved too. When CHAMELEON_PLUGIN_DATA traverses a
    symlink (macOS /tmp -> /private/tmp, a linked ~/.local/share), an
    unresolved scan root makes every relative_to() fail, clustering falls
    back to absolute paths, and the profile commits garbage buckets that
    no per-edit lookup ever matches — while bootstrap still reports
    success. These tests pin the contract from the outside: committed
    buckets are repo-relative and no artifact ever carries a prodtree
    path fragment, regardless of data-dir symlinks or the deriving PID.
    """

    def _symlinked_data_dir(self, tmp_path: Path, monkeypatch) -> Path:
        data_real = tmp_path / "data_real"
        data_real.mkdir()
        data_link = tmp_path / "data_link"
        data_link.symlink_to(data_real, target_is_directory=True)
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_link))
        return data_link

    def _origin_backed_clone(self, tmp_path: Path) -> Path:
        """Clone of a local source repo: origin-backed, so the production
        ref auto-locks and derivation runs through the prodtree."""
        source = _make_production_repo(tmp_path / "src")
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(source), str(clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        return clone

    def _assert_no_prodtree_fragments(self, repo: Path) -> None:
        """No committed artifact may carry a prodtree path fragment."""
        for artifact in sorted((repo / ".chameleon").rglob("*")):
            if not artifact.is_file():
                continue
            text = artifact.read_text(encoding="utf-8", errors="replace")
            assert "prodtree" not in text, f"prodtree fragment in {artifact.name}"

    def test_buckets_stay_repo_relative_through_symlinked_data_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._symlinked_data_dir(tmp_path, monkeypatch)
        clone = self._origin_backed_clone(tmp_path)

        env = tools.bootstrap_repo(str(clone))
        data = env["data"]
        assert data["status"] == "success"
        assert data["production_ref"]["locked"] is True

        arch = json.loads((clone / ".chameleon" / "archetypes.json").read_text(encoding="utf-8"))
        archetypes = arch["archetypes"]
        assert archetypes, "expected at least one archetype from the service fixture"
        for name, body in archetypes.items():
            pattern = body.get("paths_pattern", "")
            assert not pattern.startswith("/"), (name, pattern)
            assert "prodtree" not in pattern, (name, pattern)
            assert "private" not in pattern, (name, pattern)
            assert pattern.startswith("src"), (name, pattern)
            for sub in body.get("sub_buckets") or {}:
                assert not sub.startswith("/"), (name, sub)
                assert "prodtree" not in sub, (name, sub)

        self._assert_no_prodtree_fragments(clone)

        # The whole point of repo-relative buckets: a real checkout file
        # resolves to a live archetype at edit time. get_archetype now trust-gates
        # like every sibling read tool, so grant trust first -- this also exercises
        # that the trust record resolves THROUGH the symlinked data dir.
        from chameleon_mcp.profile.trust import grant_trust

        grant_trust(tools._compute_repo_id(clone), clone / ".chameleon")
        target = clone / "src" / "services" / "alphaService.ts"
        res = tools.get_archetype(str(clone), str(target))["data"]
        assert res["archetype"] is not None
        assert res["match_quality"] != "none"

    def test_refresh_is_idempotent_and_pid_free_under_symlinked_data_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import os as _os

        self._symlinked_data_dir(tmp_path, monkeypatch)
        clone = self._origin_backed_clone(tmp_path)
        assert tools.bootstrap_repo(str(clone))["data"]["status"] == "success"

        env = tools.refresh_repo(str(clone), force=True)
        assert env["data"]["status"] == "success"
        first = json.loads((clone / ".chameleon" / "archetypes.json").read_text(encoding="utf-8"))

        # Re-derive under a DIFFERENT pid: the prodtree dirname embeds the
        # deriving PID, and committed keys must never depend on it.
        real_pid = _os.getpid()

        class _OsProxy:
            def __getattr__(self, name):
                return getattr(_os, name)

            @staticmethod
            def getpid() -> int:
                return real_pid + 1

        monkeypatch.setattr(tools, "os", _OsProxy())
        env = tools.refresh_repo(str(clone), force=True)
        assert env["data"]["status"] == "success"
        second = json.loads((clone / ".chameleon" / "archetypes.json").read_text(encoding="utf-8"))

        first.pop("generation", None)
        second.pop("generation", None)
        assert first == second

        self._assert_no_prodtree_fragments(clone)
