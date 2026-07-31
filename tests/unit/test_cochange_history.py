"""F7: framework-agnostic historical co-change from git history.

The curated cochange table only knows a few framework pairs (Rails model ->
migration, etc.). This mines the repo's OWN commit history for files that
change together: if editing A has historically meant editing B (B present in
>= min_ratio of A's commits, with min_support commits), and a change touches A
but not B, that is a deterministic, zero-LLM omission signal. These tests pin
the miner, the strong-partner threshold, the omission query, and fail-open.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from chameleon_mcp import hook_helper
from chameleon_mcp.cochange_history import (
    COCHANGE_HISTORY_FILENAME,
    load_cochange_history,
    mine_cochange_history,
    missing_partners,
)
from chameleon_mcp.enforcement import EnforcementState

_HAS_GIT = shutil.which("git") is not None
_GIT = pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")


def _git(repo: Path, *args: str, at: int | None = None) -> None:
    env = None
    if at is not None:
        stamp = f"{at} +0000"
        env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    )


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, files: dict[str, str], msg: str, when: int) -> None:
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg, at=when)


@_GIT
def test_mines_strong_partners_and_ignores_independent_file(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    # a.py and its test change together in 5 of 6 of a.py's commits.
    for i in range(5):
        _commit(repo, {"a.py": f"v{i}", "test_a.py": f"tv{i}"}, f"pair {i}", base + i * 86400)
    _commit(repo, {"a.py": "solo"}, "a alone once", base + 6 * 86400)
    # unrelated.py changes on its own, never with a.py.
    for i in range(4):
        _commit(repo, {"unrelated.py": f"u{i}"}, f"unrel {i}", base + (10 + i) * 86400)

    index = mine_cochange_history(repo, min_support=3, min_ratio=0.5)
    assert index is not None
    partners = {p["partner"] for p in index["partners"].get("a.py", [])}
    assert "test_a.py" in partners  # strong partner: 5/6 of a.py's commits
    # unrelated.py never co-changed with a.py, so it is not a partner.
    assert "unrelated.py" not in partners
    assert not index["partners"].get("unrelated.py")  # no strong partner


@_GIT
def test_below_support_or_ratio_is_not_a_partner(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    # a.py has 10 commits; b.py co-changed in only 2 (ratio 0.2, below 0.5).
    for i in range(2):
        _commit(repo, {"a.py": f"v{i}", "b.py": f"b{i}"}, f"pair {i}", base + i * 86400)
    for i in range(8):
        _commit(repo, {"a.py": f"solo{i}"}, f"solo {i}", base + (5 + i) * 86400)

    index = mine_cochange_history(repo, min_support=3, min_ratio=0.5)
    partners = {p["partner"] for p in index["partners"].get("a.py", [])}
    assert "b.py" not in partners  # 2/10 ratio and 2 < min_support both fail


@_GIT
def test_bulk_commit_is_skipped_as_noise(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    # A single huge commit touching many files must NOT make them all partners.
    bulk = {f"f{i}.py": "x" for i in range(40)}
    _commit(repo, bulk, "mass reformat", base)
    # Then a.py and b.py genuinely co-change a few times.
    for i in range(4):
        _commit(repo, {"a.py": f"v{i}", "b.py": f"b{i}"}, f"pair {i}", base + (1 + i) * 86400)

    index = mine_cochange_history(repo, min_support=3, min_ratio=0.5, max_files_per_commit=30)
    # The bulk commit was skipped, so no f*.py cross-partnerships leaked in.
    assert not index["partners"].get("f0.py")
    # The genuine a.py<->b.py pair still surfaces.
    assert "b.py" in {p["partner"] for p in index["partners"].get("a.py", [])}


@_GIT
def test_missing_partners_omission_query(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    for i in range(5):
        _commit(repo, {"a.py": f"v{i}", "test_a.py": f"t{i}"}, f"pair {i}", base + i * 86400)

    index = mine_cochange_history(repo, min_support=3, min_ratio=0.5)
    # Touch a.py but not its partner -> flagged.
    miss = missing_partners(index, ["a.py"])
    assert any(m["source"] == "a.py" and m["partner"] == "test_a.py" for m in miss)
    # Touch both -> nothing missing.
    assert missing_partners(index, ["a.py", "test_a.py"]) == []
    # Touch an unknown file -> nothing.
    assert missing_partners(index, ["unknown.py"]) == []


@_GIT
def test_deleted_partner_is_pruned(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    for i in range(5):
        _commit(repo, {"a.py": f"v{i}", "gone.py": f"g{i}"}, f"pair {i}", base + i * 86400)
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-qm", "remove gone", at=base + 6 * 86400)

    index = mine_cochange_history(repo, min_support=3, min_ratio=0.5)
    # gone.py no longer exists in the tree, so it must not be surfaced as a partner
    # to chase (it would be an un-actionable false omission).
    partners = {p["partner"] for p in index["partners"].get("a.py", [])}
    assert "gone.py" not in partners


def test_git_unavailable_fails_open(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert mine_cochange_history(not_a_repo) is None
    # A None / empty index makes the omission query trivially empty, never crash.
    assert missing_partners(None, ["a.py"]) == []
    assert missing_partners({"partners": {}}, ["a.py"]) == []


@_GIT
def test_persist_and_load_round_trip(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    for i in range(5):
        _commit(repo, {"a.py": f"v{i}", "test_a.py": f"t{i}"}, f"pair {i}", base + i * 86400)

    index = mine_cochange_history(repo, min_support=3, min_ratio=0.5)
    out = tmp_path / "cochange_history.json"
    out.write_text(json.dumps(index))
    loaded = load_cochange_history(out)
    assert loaded is not None
    assert "test_a.py" in {p["partner"] for p in loaded["partners"].get("a.py", [])}
    # Missing file -> None, no raise.
    assert load_cochange_history(tmp_path / "nope.json") is None


# --- Stop consumer (_cochange_history_advisory_lines) ----------------------------

_RID = "r" * 64


def _consumer_repo(tmp_path, monkeypatch, *, partner_on_disk: bool = True) -> Path:
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.py").write_text("x")
    if partner_on_disk:
        (repo / "app" / "test_a.py").write_text("t")
    # v2 index: keys are top-relative and `root` records the work-tree top the
    # consumer relativizes the turn's edited files against.
    index = {
        "schema": 2,
        "root": str(repo),
        "partners": {"app/a.py": [{"partner": "app/test_a.py", "co": 5, "of": 6, "ratio": 0.833}]},
    }
    pd = tmp_path / "pd" / _RID
    pd.mkdir(parents=True)
    (pd / COCHANGE_HISTORY_FILENAME).write_text(json.dumps(index))
    return repo


def _state(repo, *rels) -> EnforcementState:
    st = EnforcementState()
    st.files = {str(repo / r) for r in rels}
    return st


def test_advisory_flags_untouched_partner(tmp_path, monkeypatch):
    repo = _consumer_repo(tmp_path, monkeypatch)
    lines = hook_helper._cochange_history_advisory_lines(
        repo_root=repo,
        repo_id=_RID,
        state=_state(repo, "app/a.py"),  # edited a.py, not its partner
        cfg=SimpleNamespace(mode="shadow"),
    )
    text = "\n".join(lines)
    assert "app/a.py usually changes with app/test_a.py" in text
    assert "83%" in text


def test_advisory_silent_when_partner_also_touched(tmp_path, monkeypatch):
    repo = _consumer_repo(tmp_path, monkeypatch)
    lines = hook_helper._cochange_history_advisory_lines(
        repo_root=repo,
        repo_id=_RID,
        state=_state(repo, "app/a.py", "app/test_a.py"),  # both touched
        cfg=SimpleNamespace(mode="shadow"),
    )
    assert lines == []


def test_advisory_dedups_once_per_session(tmp_path, monkeypatch):
    repo = _consumer_repo(tmp_path, monkeypatch)
    st = _state(repo, "app/a.py")
    cfg = SimpleNamespace(mode="shadow")
    first = hook_helper._cochange_history_advisory_lines(
        repo_root=repo, repo_id=_RID, state=st, cfg=cfg
    )
    assert first  # shown once
    second = hook_helper._cochange_history_advisory_lines(
        repo_root=repo, repo_id=_RID, state=st, cfg=cfg
    )
    assert second == []  # same pairing not re-shown this session


def test_advisory_kill_switch_and_off_mode(tmp_path, monkeypatch):
    repo = _consumer_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CHAMELEON_COCHANGE_HISTORY", "0")
    assert (
        hook_helper._cochange_history_advisory_lines(
            repo_root=repo,
            repo_id=_RID,
            state=_state(repo, "app/a.py"),
            cfg=SimpleNamespace(mode="shadow"),
        )
        == []
    )
    monkeypatch.delenv("CHAMELEON_COCHANGE_HISTORY")
    assert (
        hook_helper._cochange_history_advisory_lines(
            repo_root=repo,
            repo_id=_RID,
            state=_state(repo, "app/a.py"),
            cfg=SimpleNamespace(mode="off"),
        )
        == []
    )


def test_advisory_no_index_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.py").write_text("x")
    # No index persisted for this repo_id -> silent, no crash.
    assert (
        hook_helper._cochange_history_advisory_lines(
            repo_root=repo,
            repo_id=_RID,
            state=_state(repo, "app/a.py"),
            cfg=SimpleNamespace(mode="shadow"),
        )
        == []
    )


def test_advisory_contains_tampered_traversal_partner(tmp_path, monkeypatch):
    # The plugin-data index is off the trust surface (not HMAC-signed), so a third
    # local user could tamper it to inject a ../ traversal partner. The consumer
    # must contain the path -> never stat or surface an out-of-repo file (which
    # would be an existence oracle).
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.py").write_text("x")
    index = {
        "schema": 2,
        "root": str(repo),
        "partners": {
            "app/a.py": [{"partner": "../../../../../../etc/hosts", "co": 9, "of": 9, "ratio": 1.0}]
        },
    }
    pd = tmp_path / "pd" / _RID
    pd.mkdir(parents=True)
    (pd / COCHANGE_HISTORY_FILENAME).write_text(json.dumps(index))
    lines = hook_helper._cochange_history_advisory_lines(
        repo_root=repo,
        repo_id=_RID,
        state=_state(repo, "app/a.py"),
        cfg=SimpleNamespace(mode="shadow"),
    )
    assert lines == []  # traversal partner contained out; no out-of-repo path surfaced
    assert not any("etc/hosts" in ln for ln in lines)


def test_advisory_skips_partner_deleted_since_bootstrap(tmp_path, monkeypatch):
    # The index is only rebuilt at bootstrap/refresh, so it may still name a partner
    # since deleted. The consumer re-checks existence and never nags to edit a gone
    # file (the stale-partner hole the miner's build-time prune cannot cover).
    repo = _consumer_repo(tmp_path, monkeypatch, partner_on_disk=False)
    lines = hook_helper._cochange_history_advisory_lines(
        repo_root=repo,
        repo_id=_RID,
        state=_state(repo, "app/a.py"),
        cfg=SimpleNamespace(mode="shadow"),
    )
    assert lines == []


@_GIT
def test_persist_helper_to_consumer_end_to_end(tmp_path, monkeypatch):
    # The whole pipeline: the real bootstrap persist helper mines + writes the
    # plugin-data index, and the Stop consumer loads + matches an edit. This is the
    # wiring the production-ref keying bug hid in (keys/root/repo_id must all align).
    from chameleon_mcp.bootstrap.orchestrator import _persist_cochange_history_to_plugin_data

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    for i in range(5):
        _commit(repo, {"app/a.py": f"v{i}", "app/test_a.py": f"t{i}"}, f"p{i}", base + i * 86400)
    rid = "e" * 64
    _persist_cochange_history_to_plugin_data(repo, rid)

    text = "\n".join(
        hook_helper._cochange_history_advisory_lines(
            repo_root=repo,
            repo_id=rid,
            state=_state(repo, "app/a.py"),
            cfg=SimpleNamespace(mode="shadow"),
        )
    )
    assert "app/a.py usually changes with app/test_a.py" in text


@_GIT
def test_miner_keys_are_top_relative_from_a_subdir(tmp_path):
    # Mining from a monorepo SUBDIR must still key top-relative so a repo's
    # workspaces share ONE global index (under the shared repo_id) instead of
    # overwriting each other with colliding workspace-relative keys.
    repo = tmp_path / "mono"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    for i in range(5):
        _commit(
            repo,
            {"packages/a/x.py": f"v{i}", "packages/a/x_test.py": f"t{i}"},
            f"p{i}",
            base + i * 86400,
        )
    index = mine_cochange_history(repo / "packages" / "a", min_support=3, min_ratio=0.5)
    assert index is not None
    assert index["schema"] == 2
    assert Path(index["root"]).samefile(repo)  # the work-tree top, not the subdir
    # Keys are top-relative (packages/a/...), globally unique across workspaces.
    assert "packages/a/x.py" in index["partners"]
    assert "packages/a/x_test.py" in {p["partner"] for p in index["partners"]["packages/a/x.py"]}


# --- "untouched" means unmodified on disk, not merely unrecorded --------------


def _git_consumer_repo(tmp_path, monkeypatch) -> Path:
    """A committed git work tree carrying the same a.py -> test_a.py pairing.

    The seed is dated a week back because it stands for the repo's HISTORY, which
    predates the session. Committed at "now" it would instead be a commit of the
    pair made moments before Stop -- the shape a turn that commits its own work
    produces, and indistinguishable from it at the git level.
    """
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.py").write_text("x")
    (repo / "app" / "test_a.py").write_text("t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed", at=int(time.time()) - 7 * 86400)
    index = {
        "schema": 2,
        "root": str(repo),
        "partners": {"app/a.py": [{"partner": "app/test_a.py", "co": 5, "of": 6, "ratio": 0.833}]},
    }
    pd = tmp_path / "pd" / _RID
    pd.mkdir(parents=True)
    (pd / COCHANGE_HISTORY_FILENAME).write_text(json.dumps(index))
    return repo


def _advisory(repo) -> str:
    return "\n".join(
        hook_helper._cochange_history_advisory_lines(
            repo_root=repo,
            repo_id=_RID,
            state=_state(repo, "app/a.py"),
            cfg=SimpleNamespace(mode="shadow"),
        )
    )


@_GIT
def test_partner_dirty_in_worktree_is_not_nudged(tmp_path, monkeypatch):
    """The version-bump shape: a SCRIPT rewrote the partner, so it never entered
    the recorder's edit set (that parses the Bash command string, and a script's
    own writes leave no trace there) -- but it IS changed, so there is nothing to
    nudge about."""
    repo = _git_consumer_repo(tmp_path, monkeypatch)
    (repo / "app" / "test_a.py").write_text("t modified by a script")
    assert _advisory(repo) == ""


@_GIT
def test_partner_clean_in_worktree_is_still_nudged(tmp_path, monkeypatch):
    """The control: an untouched partner must still be surfaced, or the guard
    above has simply silenced the advisory."""
    repo = _git_consumer_repo(tmp_path, monkeypatch)
    text = _advisory(repo)
    assert "app/a.py usually changes with app/test_a.py" in text


@_GIT
def test_staged_partner_counts_as_touched(tmp_path, monkeypatch):
    repo = _git_consumer_repo(tmp_path, monkeypatch)
    (repo / "app" / "test_a.py").write_text("t staged")
    _git(repo, "add", "app/test_a.py")
    assert _advisory(repo) == ""


def test_non_git_tree_keeps_nudging_rather_than_going_quiet(tmp_path, monkeypatch):
    """Fail OPEN, not closed. git cannot answer outside a work tree, and a
    suppressed nudge is the omission this check exists to catch -- so an
    unanswerable check must not read as 'nothing to report'."""
    repo = _consumer_repo(tmp_path, monkeypatch)
    assert "app/a.py usually changes with app/test_a.py" in _advisory(repo)


def test_worktree_modified_rels_returns_none_not_empty_when_git_cannot_answer(tmp_path):
    """None and set() must stay distinguishable: set() would mean 'nothing is
    modified' and suppress nothing, but only by accident. The caller branches on
    `is not None`, so the two are not interchangeable."""
    from chameleon_mcp.stop.advisories import _worktree_modified_rels

    assert _worktree_modified_rels(tmp_path) is None


# --- a partner this turn COMMITTED is touched, not forgotten ------------------


def _write_index(tmp_path, repo, partner: str = "app/test_a.py") -> None:
    """Persist the a.py -> partner pairing for the Stop consumer to read."""
    index = {
        "schema": 2,
        "root": str(repo),
        "partners": {"app/a.py": [{"partner": partner, "co": 5, "of": 6, "ratio": 0.833}]},
    }
    pd = tmp_path / "pd" / _RID
    pd.mkdir(parents=True, exist_ok=True)
    (pd / COCHANGE_HISTORY_FILENAME).write_text(json.dumps(index))


@_GIT
def test_partner_committed_with_the_source_is_not_nudged(tmp_path, monkeypatch):
    """The regression: a turn that ends by COMMITTING both files leaves a clean
    tree, so the uncommitted delta sees neither and the partner reads as
    untouched. Committed is the most touched a file gets -- nudging to go edit
    the file the user just committed alongside this one is pure noise."""
    repo = _git_consumer_repo(tmp_path, monkeypatch)
    (repo / "app" / "a.py").write_text("x edited")
    (repo / "app" / "test_a.py").write_text("t edited")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "edit the pair together")
    assert _advisory(repo) == ""


@_GIT
def test_partner_left_out_of_the_commit_is_still_nudged(tmp_path, monkeypatch):
    """The control: committing the SOURCE must not blanket-silence the check.
    A partner absent from that commit is exactly the omission to report."""
    repo = _git_consumer_repo(tmp_path, monkeypatch)
    (repo / "app" / "a.py").write_text("x edited")
    _git(repo, "add", "app/a.py")
    _git(repo, "commit", "-qm", "edit the source alone")
    assert "app/a.py usually changes with app/test_a.py" in _advisory(repo)


@_GIT
def test_commit_outside_the_recency_window_is_not_this_turn(tmp_path, monkeypatch):
    """A pair committed together weeks ago is history, not this turn's work. On a
    low-traffic repo the commit cap alone still reaches that commit, so the
    window is what keeps a stale pairing from suppressing a live omission."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    repo = tmp_path / "repo"
    _init(repo)
    long_ago = int(time.time()) - 30 * 86400
    _commit(repo, {"app/a.py": "x", "app/test_a.py": "t"}, "paired, weeks ago", long_ago)
    _write_index(tmp_path, repo)
    (repo / "app" / "a.py").write_text("x edited now")
    assert "app/a.py usually changes with app/test_a.py" in _advisory(repo)


@_GIT
def test_commit_beyond_the_walk_bound_is_not_read(tmp_path, monkeypatch):
    """The walk is capped so the Stop hot path never traverses history. Past the
    cap a commit is simply not seen, which nudges (fail open), never suppresses."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    monkeypatch.setenv("CHAMELEON_COCHANGE_RECENT_COMMIT_MAX", "1")
    repo = tmp_path / "repo"
    _init(repo)
    now = int(time.time())
    _commit(repo, {"app/a.py": "x", "app/test_a.py": "t"}, "the pair", now)
    _commit(repo, {"app/a.py": "x again"}, "the source alone", now)
    _write_index(tmp_path, repo)
    assert "app/a.py usually changes with app/test_a.py" in _advisory(repo)


@_GIT
def test_commit_the_turn_had_no_part_in_does_not_suppress(tmp_path, monkeypatch):
    """A colleague's commit arriving through a `git pull` lands inside the window
    but is not this session's work. A commit counts only when it also carries a
    file the turn's own recorder saw, so a partner nobody here touched still
    surfaces as the omission it is."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    repo = tmp_path / "repo"
    _init(repo)
    now = int(time.time())
    _commit(repo, {"app/a.py": "x"}, "seed", now)
    _commit(repo, {"app/test_a.py": "t from upstream"}, "pulled in", now)
    _write_index(tmp_path, repo)
    (repo / "app" / "a.py").write_text("x edited now")
    assert "app/a.py usually changes with app/test_a.py" in _advisory(repo)


@_GIT
def test_committed_partner_with_a_hostile_path_is_still_matched(tmp_path, monkeypatch):
    """NUL framing, not line framing: a name holding a space, a quote or a
    backslash must still match the index key. A path that fails to parse reads as
    'not committed', which is the direction that produces the WRONG nudge."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    repo = tmp_path / "repo"
    _init(repo)
    partner = 'app/we "ird\\test.py'
    now = int(time.time())
    _commit(repo, {"app/a.py": "x", partner: "t"}, "seed", now)
    _write_index(tmp_path, repo, partner=partner)
    _commit(repo, {"app/a.py": "x edited", partner: "t edited"}, "edit the pair", now)
    assert _advisory(repo) == ""


def test_recent_commit_rels_fails_open_to_an_empty_set(tmp_path):
    """Empty, never None: the caller UNIONS this into the already-touched set, so
    an unanswerable git adds nothing and the work-tree-only behavior stands."""
    from chameleon_mcp.stop.advisories import _recent_commit_rels

    assert _recent_commit_rels(tmp_path, {"app/a.py"}) == set()


@_GIT
def test_recent_commit_rels_needs_a_recorded_edit_to_attribute_a_commit(tmp_path):
    from chameleon_mcp.stop.advisories import _recent_commit_rels

    repo = tmp_path / "repo"
    _init(repo)
    _commit(repo, {"app/a.py": "x", "app/test_a.py": "t"}, "the pair", int(time.time()))
    # Nothing recorded this turn -> no commit can be attributed to it.
    assert _recent_commit_rels(repo, set()) == set()
    # One recorded file attributes the whole commit, partner included.
    assert _recent_commit_rels(repo, {"app/a.py"}) == {"app/a.py", "app/test_a.py"}


@_GIT
def test_rebased_old_work_does_not_pass_as_this_turns_commit(tmp_path):
    """--since filters on COMMITTER date, which a rebase rewrites to now.

    Replaying a branch would otherwise present week-old work as this turn's and
    silence a live omission -- the costlier direction for this advisory. The
    author date survives the replay, so it is what decides.
    """
    from chameleon_mcp.stop.advisories import _recent_commit_rels

    repo = tmp_path / "repo"
    _init(repo)
    old = int(time.time()) - 5 * 86400
    _commit(repo, {"app/base.py": "b"}, "base", old - 86400)
    _git(repo, "checkout", "-qb", "feat")
    _commit(repo, {"app/a.py": "x", "app/test_a.py": "t"}, "the pair", old)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"app/base.py": "b2"}, "advance", int(time.time()))
    _git(repo, "checkout", "-q", "feat")
    _git(repo, "rebase", "-q", "main")

    # Committer date is now; author date is 5 days old and rules.
    assert _recent_commit_rels(repo, {"app/a.py"}) == set()


@_GIT
def test_a_bulk_sweep_commit_cannot_silence_every_nudge(tmp_path):
    """The miner refuses to LEARN from a commit over this cap because a mass
    sweep co-occurs everything; trusting one to SUPPRESS would be the same error
    pointed the other way, and would mute the advisory for the whole window."""
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.stop.advisories import _recent_commit_rels

    repo = tmp_path / "repo"
    _init(repo)
    cap = threshold_int("COCHANGE_HISTORY_MAX_FILES_PER_COMMIT")
    sweep = {f"app/f{i}.py": "v" for i in range(cap + 5)}
    sweep.update({"app/a.py": "x", "app/test_a.py": "t"})
    _commit(repo, sweep, "reformat everything", int(time.time()))

    assert _recent_commit_rels(repo, {"app/a.py"}) == set()


@_GIT
def test_a_path_beginning_with_a_newline_survives_the_z_framing(tmp_path):
    """Git prefixes the header/name-list separator onto a commit's FIRST path
    token only. Stripping a leading newline from every token instead renames any
    LATER path that genuinely starts with one -- and a mangled path can collide
    with a real index key rather than merely failing to match.

    Ordering is load-bearing: git lists paths byte-sorted, so the tab name sorts
    ahead of the newline one and takes the separator, leaving "\nlead.py" as a
    later token whose leading newline is its own.
    """
    from chameleon_mcp.stop.advisories import _recent_commit_rels

    repo = tmp_path / "repo"
    _init(repo)
    _commit(
        repo,
        {"\ttab.py": "x", "\nlead.py": "y", "a.py": "z"},
        "hostile names",
        int(time.time()),
    )

    got = _recent_commit_rels(repo, {"a.py"})
    assert "\nlead.py" in got, sorted(repr(g) for g in got)
    assert "lead.py" not in got, "a stripped path can collide with a real index key"
    assert "\ttab.py" in got, sorted(repr(g) for g in got)
