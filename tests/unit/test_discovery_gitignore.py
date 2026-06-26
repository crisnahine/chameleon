"""Discovery must not profile gitignored files.

A gitignored file (local secret, scratch output, build artifact in a dir the
hardcoded denylist does not cover) is not part of the committed codebase, so its
path and export symbol names must not be catalogued in the derived profile. The
filter reports only files that are BOTH untracked AND match a gitignore rule, so
tracked source (even matching a loose pattern) and untracked-but-not-ignored new
files are still profiled. On a non-git tree the filter is a no-op (fail-open).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from chameleon_mcp.bootstrap.discovery import discover_files


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _names(paths: list[Path]) -> set[str]:
    return {p.name for p in paths}


def test_gitignored_file_excluded_from_discovery(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("src/local-secrets.ts\n", encoding="utf-8")
    (repo / "src" / "real.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (repo / "src" / "local-secrets.ts").write_text("export const SECRET = 1;\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    found = _names(discover_files(repo, glob="**/*.ts"))
    assert "real.ts" in found
    assert "local-secrets.ts" not in found, "a gitignored file must not be discovered"


def test_tracked_file_matching_pattern_is_kept(tmp_path: Path) -> None:
    # A file force-tracked despite matching a gitignore rule is part of the repo
    # and must stay discoverable (check-ignore is silent on tracked paths).
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("*.gen.ts\n", encoding="utf-8")
    (repo / "src" / "keep.gen.ts").write_text("export const a = 1;\n", encoding="utf-8")
    _git(repo, "add", "-f", "src/keep.gen.ts", ".gitignore")
    _git(repo, "commit", "-m", "init")

    found = _names(discover_files(repo, glob="**/*.ts"))
    assert "keep.gen.ts" in found, "a tracked file matching a pattern must be kept"


def test_untracked_unignored_file_is_kept(tmp_path: Path) -> None:
    # Uncommitted new source that is NOT gitignored must still be profiled.
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (repo / "src" / "committed.ts").write_text("export const a = 1;\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    # brand-new, never added, not ignored
    (repo / "src" / "wip.ts").write_text("export const b = 2;\n", encoding="utf-8")

    found = _names(discover_files(repo, glob="**/*.ts"))
    assert "wip.ts" in found, "untracked, un-ignored new source must be profiled"


def test_non_git_tree_keeps_all_files(tmp_path: Path) -> None:
    # No .git -> the filter must fail open and keep everything.
    repo = tmp_path / "plain"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (repo / "src" / "b.ts").write_text("export const b = 2;\n", encoding="utf-8")

    found = _names(discover_files(repo, glob="**/*.ts"))
    assert found == {"a.ts", "b.ts"}, "non-git tree must keep all files (fail-open)"
