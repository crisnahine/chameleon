"""Unit tests for chameleon_mcp.production_ref.

Production-branch detection: given a repo root, pick the branch the repo
treats as its canonical/production line, offline-only (no network, no
ls-remote). The chain, in order:

  1. ``refs/remotes/origin/HEAD`` symref (set at clone time by git): the
     remote's declared default branch.
  2. A branch literally named ``production``/``prod`` among origin refs or
     local heads.
  3. The conventional default names ``main``/``master``/``trunk``.
  4. Nothing -> branch=None; callers (init skill) ask the user.

Conflict semantics: when the symref answer coexists with a distinct
production-named branch, or two names in the same priority group both
exist, ``conflict=True`` so the init skill confirms instead of locking
silently. Tests build tiny real git repos in tmp_path (clones of local
paths only, never the network).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from chameleon_mcp.production_ref import (
    detect_production_branch,
    resolve_production_ref,
)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _make_source_repo(root: Path, *, head: str, branches: tuple[str, ...] = ()) -> Path:
    """Init a commit-bearing repo whose HEAD symref points at ``head``."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", head)
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    for name in branches:
        _git(root, "branch", name)
    return root


def _clone(source: Path, dest: Path) -> Path:
    """file-path clone; git sets refs/remotes/origin/HEAD from the source HEAD."""
    subprocess.run(
        ["git", "clone", "-q", str(source), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


def _fetch_only_repo(source: Path, dest: Path) -> Path:
    """Repo with origin refs but NO origin/HEAD symref (git init + fetch).

    git >= 2.47 sets origin/HEAD on fetch when it is missing, so delete
    it explicitly to keep the fixture deterministic across versions.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, "init", "-q", "-b", "scratch")
    _git(dest, "config", "user.email", "t@example.com")
    _git(dest, "config", "user.name", "tester")
    _git(dest, "remote", "add", "origin", str(source))
    _git(dest, "fetch", "-q", "origin")
    subprocess.run(
        ["git", "-C", str(dest), "remote", "set-head", "origin", "--delete"],
        check=False,
        capture_output=True,
        text=True,
    )
    return dest


class TestDetectOriginHead:
    def test_symref_production_wins_clean(self, tmp_path: Path) -> None:
        source = _make_source_repo(tmp_path / "src", head="production", branches=("staging",))
        repo = _clone(source, tmp_path / "clone")
        det = detect_production_branch(repo)
        assert det.branch == "production"
        assert det.source == "origin_head"
        assert det.conflict is False
        assert det.from_origin is True

    def test_symref_any_name_accepted(self, tmp_path: Path) -> None:
        source = _make_source_repo(tmp_path / "src", head="preview")
        repo = _clone(source, tmp_path / "clone")
        det = detect_production_branch(repo)
        assert det.branch == "preview"
        assert det.source == "origin_head"

    def test_symref_plus_distinct_production_branch_is_conflict(self, tmp_path: Path) -> None:
        source = _make_source_repo(tmp_path / "src", head="main", branches=("production",))
        repo = _clone(source, tmp_path / "clone")
        det = detect_production_branch(repo)
        assert det.branch == "main"
        assert det.source == "origin_head"
        assert det.conflict is True
        assert "production" in det.candidates

    def test_dangling_origin_head_without_backing_ref_is_local(self, tmp_path: Path) -> None:
        # A dangling origin/HEAD symref (left behind by `git remote remove` or a
        # pruned remote default) resolves a branch NAME even though
        # refs/remotes/origin/<head> is gone and no remote is configured. It must
        # NOT read as origin-backed: auto-lock would otherwise pin a production_ref
        # on a now-local-only repo to a branch that can never be fetched, and
        # derivation would follow that branch instead of the working tree.
        repo = _make_source_repo(tmp_path / "repo", head="master")
        origin_dir = repo / ".git" / "refs" / "remotes" / "origin"
        origin_dir.mkdir(parents=True, exist_ok=True)
        (origin_dir / "HEAD").write_text("ref: refs/remotes/origin/master\n", encoding="utf-8")
        det = detect_production_branch(repo)
        assert det.branch == "master"
        assert det.source == "default_name"
        assert det.from_origin is False


class TestDetectNamedProduction:
    def test_origin_production_without_symref(self, tmp_path: Path) -> None:
        source = _make_source_repo(tmp_path / "src", head="trunk-x", branches=("production",))
        repo = _fetch_only_repo(source, tmp_path / "dest")
        det = detect_production_branch(repo)
        assert det.branch == "production"
        assert det.source == "named_production"
        assert det.conflict is False
        assert det.from_origin is True

    def test_local_prod_branch_only(self, tmp_path: Path) -> None:
        repo = _make_source_repo(tmp_path / "repo", head="prod")
        det = detect_production_branch(repo)
        assert det.branch == "prod"
        assert det.source == "named_production"
        # Local-only repo: no origin refs back the answer, so auto-lock
        # at init/refresh must not engage on this signal alone.
        assert det.from_origin is False

    def test_production_and_prod_both_is_conflict(self, tmp_path: Path) -> None:
        repo = _make_source_repo(tmp_path / "repo", head="production", branches=("prod",))
        det = detect_production_branch(repo)
        assert det.branch == "production"
        assert det.conflict is True
        assert "prod" in det.candidates


class TestDetectDefaultNames:
    def test_main_only(self, tmp_path: Path) -> None:
        repo = _make_source_repo(tmp_path / "repo", head="main")
        det = detect_production_branch(repo)
        assert det.branch == "main"
        assert det.source == "default_name"
        assert det.conflict is False
        assert det.from_origin is False

    def test_master_only(self, tmp_path: Path) -> None:
        repo = _make_source_repo(tmp_path / "repo", head="master")
        det = detect_production_branch(repo)
        assert det.branch == "master"
        assert det.source == "default_name"

    def test_main_and_master_is_conflict(self, tmp_path: Path) -> None:
        repo = _make_source_repo(tmp_path / "repo", head="main", branches=("master",))
        det = detect_production_branch(repo)
        assert det.branch == "main"
        assert det.conflict is True
        assert "master" in det.candidates


class TestDetectNone:
    def test_feature_branches_only(self, tmp_path: Path) -> None:
        repo = _make_source_repo(tmp_path / "repo", head="feature-x", branches=("feature-y",))
        det = detect_production_branch(repo)
        assert det.branch is None
        assert det.source == "none"

    def test_non_git_dir(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        det = detect_production_branch(plain)
        assert det.branch is None
        assert det.source == "none"
        assert det.conflict is False


class TestResolveProductionRef:
    def test_prefers_origin_over_local(self, tmp_path: Path) -> None:
        source = _make_source_repo(tmp_path / "src", head="production")
        repo = _clone(source, tmp_path / "clone")
        # Advance the local branch past origin so the two tips differ.
        (repo / "local.txt").write_text("y\n", encoding="utf-8")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "tester")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "local-only")
        origin_sha = _git(repo, "rev-parse", "origin/production")
        local_sha = _git(repo, "rev-parse", "production")
        assert origin_sha != local_sha
        resolved = resolve_production_ref(repo, "production")
        assert resolved is not None
        assert resolved.ref == "origin/production"
        assert resolved.sha == origin_sha

    def test_falls_back_to_local_branch(self, tmp_path: Path) -> None:
        repo = _make_source_repo(tmp_path / "repo", head="production")
        sha = _git(repo, "rev-parse", "production")
        resolved = resolve_production_ref(repo, "production")
        assert resolved is not None
        assert resolved.ref == "production"
        assert resolved.sha == sha

    def test_explicit_remote_ref_passthrough(self, tmp_path: Path) -> None:
        source = _make_source_repo(tmp_path / "src", head="production")
        repo = _clone(source, tmp_path / "clone")
        resolved = resolve_production_ref(repo, "origin/production")
        assert resolved is not None
        assert resolved.ref == "origin/production"
        assert len(resolved.sha) in (40, 64)

    def test_unknown_branch_returns_none(self, tmp_path: Path) -> None:
        repo = _make_source_repo(tmp_path / "repo", head="main")
        assert resolve_production_ref(repo, "production") is None

    def test_non_git_returns_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert resolve_production_ref(plain, "production") is None


class TestMaterializeProductionTree:
    def test_materializes_ref_content_not_working_tree(self, tmp_path: Path) -> None:
        from chameleon_mcp.production_ref import (
            materialize_production_tree,
            remove_production_tree,
        )

        repo = _make_source_repo(tmp_path / "repo", head="production")
        sha = _git(repo, "rev-parse", "production")
        # Diverge the working tree from the committed production state.
        (repo / "uncommitted.ts").write_text("export const x = 1\n", encoding="utf-8")
        dest = tmp_path / "data" / "prodtree" / "t1"
        tree = materialize_production_tree(repo, dest, sha)
        assert tree == dest
        assert (tree / "README.md").is_file()
        assert not (tree / "uncommitted.ts").exists()
        # A worktree checkout marks itself with a .git FILE (not a dir).
        assert (tree / ".git").is_file()
        remove_production_tree(repo, tree)
        assert not dest.exists()

    def test_bad_sha_returns_none(self, tmp_path: Path) -> None:
        from chameleon_mcp.production_ref import materialize_production_tree

        repo = _make_source_repo(tmp_path / "repo", head="main")
        dest = tmp_path / "data" / "prodtree" / "t2"
        assert materialize_production_tree(repo, dest, "0" * 40) is None
        assert not dest.exists()

    def test_non_git_returns_none(self, tmp_path: Path) -> None:
        from chameleon_mcp.production_ref import materialize_production_tree

        plain = tmp_path / "plain"
        plain.mkdir()
        dest = tmp_path / "data" / "prodtree" / "t3"
        assert materialize_production_tree(plain, dest, "0" * 40) is None

    def test_remove_tolerates_missing_tree(self, tmp_path: Path) -> None:
        from chameleon_mcp.production_ref import remove_production_tree

        repo = _make_source_repo(tmp_path / "repo", head="main")
        remove_production_tree(repo, tmp_path / "never-existed")

    def test_repo_hooks_are_not_executed(self, tmp_path: Path) -> None:
        # `git worktree add` runs the repo's post-checkout hook by default —
        # repo-controlled code executing during a static-analysis derivation.
        # Materialization must suppress hooks.
        from chameleon_mcp.production_ref import (
            materialize_production_tree,
            remove_production_tree,
        )

        repo = _make_source_repo(tmp_path / "repo", head="production")
        sha = _git(repo, "rev-parse", "production")
        proof = tmp_path / "hook-proof"
        hook = repo / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(f"#!/bin/sh\necho ran > {proof}\n", encoding="utf-8")
        hook.chmod(0o755)
        dest = tmp_path / "data" / "prodtree" / "t4"
        tree = materialize_production_tree(repo, dest, sha)
        assert tree is not None
        assert not proof.exists()
        remove_production_tree(repo, tree)

    def test_prune_skips_tree_of_live_pid(self, tmp_path: Path) -> None:
        import os

        from chameleon_mcp.production_ref import prune_stale_production_trees

        repo = _make_source_repo(tmp_path / "repo", head="main")
        container = tmp_path / "container"
        live = container / f"abc123456789-{os.getpid()}"
        dead = container / "abc123456789-999999999"
        live.mkdir(parents=True)
        dead.mkdir(parents=True)
        prune_stale_production_trees(repo, container)
        assert live.is_dir()  # creating process (this test) is alive
        assert not dead.exists()
