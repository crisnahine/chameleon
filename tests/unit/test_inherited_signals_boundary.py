"""Workspace-signal inheritance must not escape the repository.

A repo with no package.json / tsconfig.json / Gemfile / *.gemspec of its own
walked up to four ancestors looking for one, and on a match read its tool
configs from that ancestor INSTEAD of itself. The walk had no VCS or $HOME
boundary, so a repo four levels under $HOME inherited from the stray
package.json a global `npm install -g` leaves there, and its own ruff config was
silently discarded -- every Python column in the matrix shipped an empty
rules.json while its pyproject.toml plainly declared line-length.

The intent (a sub-package inside a JS monorepo should inherit that monorepo's
tooling) is sound, so the fix bounds the walk rather than removing it: never
cross out of the enclosing git repository, and never treat $HOME as a project.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.bootstrap.orchestrator import _inherited_signals_root


def _repo(root: Path, *, git: bool = False, pkg: bool = False, gemfile: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if git:
        (root / ".git").mkdir(exist_ok=True)
    if pkg:
        (root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    if gemfile:
        (root / "Gemfile").write_text('source "https://rubygems.org"', encoding="utf-8")
    return root


def test_does_not_inherit_across_the_git_boundary(tmp_path):
    # outer/package.json is OUTSIDE the repo; project/ is its own git root.
    outer = _repo(tmp_path / "outer", pkg=True)
    project = _repo(outer / "project", git=True)
    assert _inherited_signals_root(project) is None


def test_does_not_inherit_from_home(tmp_path, monkeypatch):
    # The real-world case: a stray package.json in $HOME from `npm install -g`.
    home = _repo(tmp_path / "home", pkg=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = _repo(home / "Documents" / "Projects" / "qa" / "py-plain")
    assert _inherited_signals_root(project) is None


def test_still_inherits_from_a_real_monorepo_ancestor(tmp_path):
    # The case the walk exists for: a sub-package inside the SAME git repo as a
    # JS monorepo root must still inherit that root's tooling.
    mono = _repo(tmp_path / "mono", git=True, pkg=True)
    sub = _repo(mono / "services" / "api")
    assert _inherited_signals_root(sub) == mono


def test_inherits_from_a_gemfile_monorepo_ancestor(tmp_path):
    mono = _repo(tmp_path / "mono", git=True, gemfile=True)
    sub = _repo(mono / "packages" / "worker")
    assert _inherited_signals_root(sub) == mono


def test_repo_with_its_own_markers_never_inherits(tmp_path):
    mono = _repo(tmp_path / "mono", git=True, pkg=True)
    sub = _repo(mono / "web", pkg=True)
    assert _inherited_signals_root(sub) is None


def test_walk_remains_bounded_in_depth(tmp_path):
    # A matching ancestor further than the existing 4-level budget is still not
    # inherited, inside the git repo or not.
    mono = _repo(tmp_path / "mono", git=True, pkg=True)
    deep = _repo(mono / "a" / "b" / "c" / "d" / "e")
    assert _inherited_signals_root(deep) is None
