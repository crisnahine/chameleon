"""G0: canonical recency uses git commit time with smooth decay.

The legacy _file_recency_weight was a binary mtime step: a file within a 90-day
window doubled its selection vote, else 1.0. On a fresh clone every file shares
the clone-time mtime, so recency contributed nothing and selection fell to the
demote tiebreak then the path string -- which mispicks a mid-migration repo
where the NEW idiom is the cluster minority (typicality favors the legacy
majority). These tests pin the fix: the weight decays off the file's last git
commit time (which survives a fresh clone), falling back to the old mtime step
only when git is unavailable or the file is untracked.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap import canonical
from chameleon_mcp.bootstrap.canonical import (
    RECENCY_WEIGHT_MULTIPLIER,
    _build_commit_time_map,
    _file_recency_weight,
    select_canonicals,
)
from chameleon_mcp.bootstrap.clustering import cluster_files
from chameleon_mcp.extractors._base import ParsedFile

_HAS_GIT = shutil.which("git") is not None
_GIT = pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")


# --- pure decay units (no git needed) --------------------------------------------


def test_recent_commit_outranks_old_commit_at_equal_mtime():
    """THE fresh-clone bug, expressed at the weight seam: two files with the same
    reference clock (equal mtimes) but different commit times -> the recently
    committed one outranks. The legacy code, keyed only on mtime, tied them."""
    now = 1_000_000_000.0
    recent = _file_recency_weight(Path("x"), now=now, commit_epoch=now - 86400)  # 1 day old
    old = _file_recency_weight(Path("x"), now=now, commit_epoch=now - 730 * 86400)  # ~2 years old
    assert recent > old
    assert recent == pytest.approx(RECENCY_WEIGHT_MULTIPLIER, abs=0.05)
    assert old == pytest.approx(1.0, abs=0.05)


def test_commit_decay_is_monotonic_in_age():
    now = 1_000_000_000.0
    weights = [
        _file_recency_weight(Path("x"), now=now, commit_epoch=now - d * 86400)
        for d in (0, 10, 45, 90, 365)
    ]
    assert weights == sorted(weights, reverse=True)
    assert weights[0] == pytest.approx(RECENCY_WEIGHT_MULTIPLIER)


def test_future_commit_time_clamps_to_most_recent():
    # A commit AHEAD of the clock (cross-machine / CI skew is routine) must be
    # treated as most-recent (full boost), never penalized to 1.0 -- penalizing
    # the newest file inverts recency and flips selection across refreshes.
    now = 1_000_000_000.0
    assert (
        _file_recency_weight(Path("x"), now=now, commit_epoch=now + 10_000)
        == RECENCY_WEIGHT_MULTIPLIER
    )
    # The mtime fallback keeps the stricter future -> 1.0 guard (a future mtime is
    # same-machine clock weirdness, not cross-machine commit skew); that path is
    # covered by test_future_mtime_gets_no_recency_boost in
    # test_review_fix_bootstrap_helpers.py.


def test_true_half_life_halves_the_boost():
    # The name/docstring say "half-life": the boost above 1.0 must HALVE every
    # half_life_days (2**-1), not e-fold (exp gives ~0.37, which would be wrong).
    now = 1_000_000_000.0
    hl = 45.0
    at_1 = _file_recency_weight(
        Path("x"), now=now, commit_epoch=now - hl * 86400, half_life_days=hl
    )
    at_2 = _file_recency_weight(
        Path("x"), now=now, commit_epoch=now - 2 * hl * 86400, half_life_days=hl
    )
    boost = RECENCY_WEIGHT_MULTIPLIER - 1.0
    assert at_1 == pytest.approx(1.0 + boost * 0.5)  # one half-life -> half the boost
    assert at_2 == pytest.approx(1.0 + boost * 0.25)  # two half-lives -> a quarter


def test_half_life_is_configurable():
    now = 1_000_000_000.0
    fast = _file_recency_weight(
        Path("x"), now=now, commit_epoch=now - 45 * 86400, half_life_days=10
    )
    slow = _file_recency_weight(
        Path("x"), now=now, commit_epoch=now - 45 * 86400, half_life_days=90
    )
    # A shorter half-life decays faster, so the same-age file scores lower.
    assert fast < slow


def test_mtime_fallback_unchanged_when_no_commit_epoch(tmp_path):
    """With commit_epoch=None the function is the legacy mtime step, so the
    existing fresh-file boost and old-file no-boost behavior is preserved."""
    f = tmp_path / "recent.ts"
    f.write_text("x")
    assert (
        _file_recency_weight(f, now=time.time() + 1.0, commit_epoch=None)
        == RECENCY_WEIGHT_MULTIPLIER
    )
    assert (
        _file_recency_weight(f, now=time.time() + canonical._RECENCY_WINDOW_SECONDS + 10_000) == 1.0
    )


# --- git integration -------------------------------------------------------------


def _git(repo: Path, *args: str, at: int | None = None) -> None:
    env = None
    if at is not None:
        stamp = f"{at} +0000"
        import os

        env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")


@_GIT
def test_build_commit_time_map_uses_most_recent_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    two_years_ago = int(time.time()) - 730 * 86400
    (repo / "a.rb").write_text("class A; end\n")
    _git(repo, "add", "a.rb")
    _git(repo, "commit", "-qm", "old", at=two_years_ago)
    # A second commit touching a.rb again, recent -> the map must carry the newer.
    (repo / "a.rb").write_text("class A; def x; end; end\n")
    (repo / "b.rb").write_text("class B; end\n")
    recent = int(time.time()) - 3600
    _git(repo, "add", "a.rb", "b.rb")
    _git(repo, "commit", "-qm", "new", at=recent)
    # An untracked file must be absent from the map (caller mtime-falls-back).
    (repo / "untracked.rb").write_text("class U; end\n")

    cmap, source = _build_commit_time_map(repo)
    assert source == "git"
    assert cmap is not None
    assert cmap["a.rb"] == recent  # newest commit for a.rb, not the 2yr-old one
    assert cmap["b.rb"] == recent
    assert "untracked.rb" not in cmap


@_GIT
def test_git_unavailable_falls_back_to_mtime(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    cmap, source = _build_commit_time_map(not_a_repo)
    assert cmap is None
    assert source != "git"

    (not_a_repo / "app").mkdir()
    a = not_a_repo / "app" / "a_svc.rb"
    a.write_text("class A; def call; 1; end; end\n")
    b = not_a_repo / "app" / "b_svc.rb"
    b.write_text("class B; def call; 2; end; end\n")
    result = cluster_files([_pf(a), _pf(b)], not_a_repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, not_a_repo)
    assert sel.selections, "mtime fallback must still select a witness when git is absent"


def test_timeout_falls_back_to_mtime(tmp_path, monkeypatch):
    # A stuck git-log walk (huge-history monolith) must degrade to mtime, not hang
    # or crash bootstrap.
    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git log", timeout=1)

    monkeypatch.setattr(canonical.subprocess, "run", _raise_timeout)
    cmap, source = _build_commit_time_map(tmp_path)
    assert cmap is None
    assert source == "git-error"


def test_nonpositive_half_life_is_no_decay(tmp_path):
    # A misconfigured half-life must not divide-by-zero; it collapses to no-decay
    # (full multiplier for any past commit).
    now = 1_000_000_000.0
    for hl in (0.0, -5.0):
        assert (
            _file_recency_weight(
                Path("x"), now=now, commit_epoch=now - 500 * 86400, half_life_days=hl
            )
            == RECENCY_WEIGHT_MULTIPLIER
        )


def _pf(path: Path) -> ParsedFile:
    return ParsedFile(
        path=path,
        content_first_200_bytes="",
        top_level_node_kinds=("ClassNode",),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    )


@_GIT
def test_select_canonicals_prefers_recent_commit_over_path_tiebreak(tmp_path):
    """DoD: same-signature cluster, uniform (fresh-clone) mtimes. The recently
    committed file has a lexicographically LATER name, so the legacy path
    tiebreak would pick an old file; commit-time recency now overrides it and
    selects the recent witness."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    svc = repo / "app" / "services"
    svc.mkdir(parents=True)
    # Same structure (same re-extracted signature => equal typicality), so recency
    # is the deciding sort key above the path string.
    (svc / "a_old.rb").write_text("class AOld\n  def call; 1; end\nend\n")
    (svc / "b_old.rb").write_text("class BOld\n  def call; 2; end\nend\n")
    two_years_ago = int(time.time()) - 730 * 86400
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "legacy", at=two_years_ago)

    # The new idiom lands last, named to LOSE the path tiebreak (z_ sorts last).
    (svc / "z_new.rb").write_text("class ZNew\n  def call; 3; end\nend\n")
    recent = int(time.time()) - 3600
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "new idiom", at=recent)

    # Fresh-clone simulation: make every mtime identical so mtime carries no
    # signal (the legacy code would then fall to the path tiebreak == a_old.rb).
    stamp = time.time()
    import os

    for f in svc.glob("*.rb"):
        os.utime(f, (stamp, stamp))

    pfs = [_pf(p) for p in sorted(svc.glob("*.rb"))]
    result = cluster_files(pfs, repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, repo)
    witnesses = {s.witness_path.name for s in sel.selections.values()}
    assert "z_new.rb" in witnesses, f"recent commit should win; got {witnesses}"
    assert "a_old.rb" not in witnesses


@_GIT
def test_recent_abstract_base_still_loses_to_older_concrete(tmp_path):
    """Continuous commit-time recency must NOT defeat the demote guard: a Rails
    abstract base (application_record.rb) committed more recently than a concrete
    model is still a hollow 'mirror this' witness and must lose. With the old
    mtime step this was masked because within-window files tied on weight and
    demote decided; continuous decay would rank the recent base first unless
    demote sits ABOVE recency in the sort."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    models = repo / "app" / "models"
    models.mkdir(parents=True)
    # Concrete model, committed long ago.
    (models / "user.rb").write_text("class User < ApplicationRecord\n  def call; 1; end\nend\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "concrete", at=int(time.time()) - 730 * 86400)
    # Abstract base, touched by a recent refactor.
    (models / "application_record.rb").write_text(
        "class ApplicationRecord < ActiveRecord::Base\n  def call; 2; end\nend\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "recent base", at=int(time.time()) - 3600)

    pfs = [_pf(p) for p in sorted(models.glob("*.rb"))]
    result = cluster_files(pfs, repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, repo)
    witnesses = {s.witness_path.name for s in sel.selections.values()}
    assert "user.rb" in witnesses, f"concrete sibling should win; got {witnesses}"
    assert "application_record.rb" not in witnesses


def test_kill_switch_skips_the_git_walk(tmp_path, monkeypatch):
    """CHAMELEON_CANONICAL_GIT_RECENCY=0 forces the legacy mtime step and never
    spawns the git walk (an intentional opt-out, not a degradation)."""

    def _boom(_root):
        raise AssertionError("git walk must not run when the kill switch is set")

    monkeypatch.setattr(canonical, "_build_commit_time_map", _boom)
    monkeypatch.setenv("CHAMELEON_CANONICAL_GIT_RECENCY", "0")

    repo = tmp_path / "repo"
    svc = repo / "app" / "services"
    svc.mkdir(parents=True)
    a = svc / "a_svc.rb"
    a.write_text("class A; def call; 1; end; end\n")
    b = svc / "b_svc.rb"
    b.write_text("class B; def call; 2; end; end\n")
    result = cluster_files([_pf(a), _pf(b)], repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, repo)
    assert sel.selections, "mtime path must still select a witness with git recency off"


@_GIT
def test_map_keys_are_workspace_relative_in_a_monorepo_subdir(tmp_path):
    # repo_root is a workspace SUBDIR of a larger git repo: --relative must emit
    # keys relative to that subdir (aligned with the consumer's _rel_posix lookup)
    # and must not leak a sibling package's files into this workspace's map.
    root = tmp_path / "mono"
    (root / "packages" / "a" / "src").mkdir(parents=True)
    (root / "packages" / "b" / "src").mkdir(parents=True)
    _init_repo(root)
    (root / "packages" / "a" / "src" / "widget.ts").write_text("export const a = 1\n")
    (root / "packages" / "b" / "src" / "gadget.ts").write_text("export const b = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init", at=int(time.time()) - 3600)

    ws = root / "packages" / "a"
    cmap, source = _build_commit_time_map(ws)
    assert source == "git"
    assert list(cmap) == ["src/widget.ts"], f"keys must be workspace-relative; got {list(cmap)}"
    assert canonical._rel_posix(ws / "src" / "widget.ts", ws) in cmap
    assert not any("gadget" in k for k in cmap), "sibling package leaked into the workspace map"


@_GIT
def test_empty_subtree_yields_empty_map_not_none(tmp_path):
    # A tracked repo whose target subdir has no committed files: the walk succeeds
    # with an empty map (source "git"), distinct from git-unavailable (None). Files
    # in it are then untracked -> mtime fallback per file, not a whole-pass degrade.
    root = tmp_path / "repo"
    (root / "committed").mkdir(parents=True)
    _init_repo(root)
    (root / "committed" / "x.rb").write_text("class X; end\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init", at=int(time.time()) - 3600)
    empty = root / "later"
    empty.mkdir()

    cmap, source = _build_commit_time_map(empty)
    assert source == "git"
    assert cmap == {}


@_GIT
def test_unicode_path_survives_the_walk(tmp_path):
    # core.quotePath=false keeps a non-ASCII path un-escaped so its map key matches
    # the on-disk relative path the consumer looks up.
    repo = tmp_path / "repo"
    _init_repo(repo)
    name = "café_service.rb"
    (repo / name).write_text("class Cafe; end\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unicode", at=int(time.time()) - 3600)

    cmap, source = _build_commit_time_map(repo)
    assert source == "git"
    assert name in cmap, f"unicode path missing from map; keys={list(cmap)}"
