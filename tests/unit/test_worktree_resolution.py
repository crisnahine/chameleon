"""resolve_profile_root maps a linked git worktree to its main worktree.

A linked worktree has a ``.git`` FILE pointing at ``<main>/.git/worktrees/<name>``
and (normally) no ``.chameleon/`` of its own. The profile and trust must resolve
against the main worktree, regardless of where the worktree lives on disk
(under the repo, a sibling, or a fully custom path). Every non-worktree case
must return the input root unchanged.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from chameleon_mcp.worktree import main_worktree_root, resolve_profile_root


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _make_main_repo(root: Path, *, with_chameleon: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@t.t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / "model.rb").write_text("class M; end\n", encoding="utf-8")
    (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    if with_chameleon:
        cham = root / ".chameleon"
        cham.mkdir(exist_ok=True)
        (cham / "profile.json").write_text('{"schema_version": 8}', encoding="utf-8")
        (cham / "COMMITTED").write_text("committed-at=1\npid=1\n", encoding="utf-8")
    return root


class TestWorktreeResolvesToMain:
    def test_sibling_worktree_resolves_to_main(self, tmp_path):
        main = _make_main_repo(tmp_path / "main")
        wt = tmp_path / "sibling-wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        # the worktree has a .git FILE and no .chameleon of its own
        assert (wt / ".git").is_file()
        assert not (wt / ".chameleon").exists()
        assert resolve_profile_root(wt) == main

    def test_custom_deep_path_worktree_resolves_to_main(self, tmp_path):
        main = _make_main_repo(tmp_path / "main")
        wt = tmp_path / "elsewhere" / "deep" / "here"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        assert resolve_profile_root(wt) == main

    def test_worktree_under_repo_resolves_to_main(self, tmp_path):
        main = _make_main_repo(tmp_path / "main")
        wt = main / ".claude" / "worktrees" / "feature"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        assert resolve_profile_root(wt) == main


class TestNonWorktreeUnchanged:
    """Every existing layout must return the input root byte-identically."""

    def test_repo_with_own_chameleon_unchanged(self, tmp_path):
        main = _make_main_repo(tmp_path / "main")
        assert resolve_profile_root(main) == main

    def test_git_directory_repo_without_chameleon_unchanged(self, tmp_path):
        root = _make_main_repo(tmp_path / "plain", with_chameleon=False)
        assert (root / ".git").is_dir()
        assert resolve_profile_root(root) == root

    def test_no_git_no_chameleon_unchanged(self, tmp_path):
        root = tmp_path / "bare-dir"
        (root / "src").mkdir(parents=True)
        assert resolve_profile_root(root) == root

    def test_worktree_with_own_chameleon_uses_itself(self, tmp_path):
        main = _make_main_repo(tmp_path / "main")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        # a worktree that DOES have its own .chameleon keeps it (fast path)
        (wt / ".chameleon").mkdir()
        (wt / ".chameleon" / "profile.json").write_text("{}", encoding="utf-8")
        assert resolve_profile_root(wt) == wt

    def test_worktree_whose_main_has_no_chameleon_unchanged(self, tmp_path):
        main = _make_main_repo(tmp_path / "main", with_chameleon=False)
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        # no profile anywhere -> nothing to inherit, return the worktree
        assert resolve_profile_root(wt) == wt


class TestMalformedPointers:
    def test_git_file_not_a_gitdir_pointer(self, tmp_path):
        root = tmp_path / "weird"
        root.mkdir()
        (root / ".git").write_text("not a pointer\n", encoding="utf-8")
        assert main_worktree_root(root / ".git") is None
        assert resolve_profile_root(root) == root

    def test_git_file_empty_gitdir(self, tmp_path):
        root = tmp_path / "weird"
        root.mkdir()
        (root / ".git").write_text("gitdir: \n", encoding="utf-8")
        assert main_worktree_root(root / ".git") is None

    def test_relative_gitdir_pointer_resolves(self, tmp_path):
        # main with a .chameleon and a real .git/worktrees/<name> layout
        main = _make_main_repo(tmp_path / "main")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        # rewrite the pointer as a relative path; git accepts both forms
        gitdir_abs = (main / ".git" / "worktrees" / "wt").resolve()
        rel = Path(os.path.relpath(gitdir_abs, wt))
        (wt / ".git").write_text(f"gitdir: {rel}\n", encoding="utf-8")
        assert resolve_profile_root(wt) == main
