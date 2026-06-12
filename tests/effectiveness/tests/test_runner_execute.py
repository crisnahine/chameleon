"""Execution loop with stubbed spawn/bootstrap/worktrees: budget abort,
error accounting, exit codes, panel triggering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.effectiveness import runner
from tests.effectiveness.tasks import EffTask


class _FakeSession:
    def __init__(self, cost=0.05):
        self.cost_usd = cost
        self.hook_events = []
        self.transcript_path = Path("/dev/null")
        self.returncode = 0
        self.tool_uses = []
        self.session_id = "sess-1"
        self.result_text = ""
        self.bash_commands = ["npm test"]


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Stub every external effect; record calls."""
    calls = {"spawned": [], "prepared": []}

    task = EffTask(
        task_id="t1-stub-conv",
        tier="ci",
        fixture="ts",
        prompt="p",
        category="convention",
        scorers=("cost",),
    )
    monkeypatch.setattr(runner, "_collect_tasks", lambda: [task])
    monkeypatch.setattr(runner, "_preflight", lambda args, tasks: None)
    monkeypatch.setattr(
        runner,
        "_prepare_fixtures",
        lambda ctx, tasks: {"ts": tmp_path / "fixture_repo"},
    )

    def fake_prepare_cell(**kw):
        calls["prepared"].append(kw)
        dest = kw["dest"]
        dest.mkdir(parents=True, exist_ok=True)
        return "deadbeef"

    monkeypatch.setattr(runner, "_prepare_cell", lambda **kw: fake_prepare_cell(**kw))
    monkeypatch.setattr(runner, "_grant_trust", lambda p: "0" * 64)
    monkeypatch.setattr(runner, "_changed_files", lambda wt, sha: [])
    monkeypatch.setattr(runner, "_session_diff", lambda wt, sha: "diff")

    def fake_spawn(**kw):
        calls["spawned"].append(kw)
        return _FakeSession()

    monkeypatch.setattr(runner, "_spawn", lambda **kw: fake_spawn(**kw))
    return calls, tmp_path


def test_happy_path_writes_outputs_and_exits_zero(wired):
    calls, tmp_path = wired
    rc = runner.main(
        [
            "--arms",
            "off,shadow",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "5",
        ]
    )
    assert rc == 0
    assert len(calls["spawned"]) == 2  # 1 task x 2 arms
    run_dirs = list((tmp_path / "results").glob("effectiveness_*"))
    assert len(run_dirs) == 1
    doc = json.loads((run_dirs[0] / "run.json").read_text())
    assert len(doc["cells"]) == 2
    assert doc["errors"] == 0
    assert (run_dirs[0] / "run.md").is_file()


def test_session_error_recorded_not_fatal(wired, monkeypatch):
    calls, tmp_path = wired

    bad = _FakeSession()
    bad.returncode = -1  # timeout shape
    seq = [bad, _FakeSession()]
    monkeypatch.setattr(runner, "_spawn", lambda **kw: seq.pop(0))

    rc = runner.main(
        [
            "--arms",
            "off,shadow",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "5",
        ]
    )
    assert rc == 0  # >= 1 cell ran ok
    doc = json.loads(next((tmp_path / "results").glob("effectiveness_*/run.json")).read_text())
    statuses = sorted(c["status"] for c in doc["cells"])
    assert statuses == ["error", "ok"]
    assert doc["errors"] == 1


def test_all_cells_failing_exits_nonzero(wired, monkeypatch):
    calls, tmp_path = wired

    def explode(**kw):
        raise RuntimeError("worktree creation failed")

    monkeypatch.setattr(runner, "_prepare_cell", explode)
    rc = runner.main(
        [
            "--arms",
            "off",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "5",
        ]
    )
    assert rc == 1  # harness-level: no cell ran


def _wire_env_cell(monkeypatch, tmp_path):
    """Wire one tier-full cell against a stand-in env repo path."""
    task = EffTask(
        task_id="t2-stub-env",
        tier="full",
        fixture="env-ts",
        prompt="p",
        category="convention",
        scorers=("cost",),
    )
    env_repo = tmp_path / "real_env_repo"
    monkeypatch.setattr(runner, "_collect_tasks", lambda: [task])
    monkeypatch.setattr(runner, "_preflight", lambda args, tasks: None)
    monkeypatch.setattr(runner, "_prepare_fixtures", lambda ctx, tasks: {"env-ts": env_repo})

    def fake_prepare_cell(**kw):
        kw["dest"].mkdir(parents=True, exist_ok=True)
        return "deadbeef"

    monkeypatch.setattr(runner, "_prepare_cell", lambda **kw: fake_prepare_cell(**kw))
    monkeypatch.setattr(runner, "_grant_trust", lambda p: "0" * 64)
    monkeypatch.setattr(runner, "_changed_files", lambda wt, sha: [])
    monkeypatch.setattr(runner, "_session_diff", lambda wt, sha: "diff")
    monkeypatch.setattr(runner, "_spawn", lambda **kw: _FakeSession())
    removed = []
    monkeypatch.setattr(runner, "_remove_worktree", lambda repo, dest: removed.append((repo, dest)))
    return env_repo, removed


def test_env_repo_worktree_removed_on_success(monkeypatch, tmp_path, capsys):
    env_repo, removed = _wire_env_cell(monkeypatch, tmp_path)
    rc = runner.main(
        [
            "--tier",
            "full",
            "--arms",
            "off",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "5",
        ]
    )
    assert rc == 0
    assert len(removed) == 1
    repo, dest = removed[0]
    assert repo == env_repo
    assert dest.name == "t2-stub-env__off__r1"
    assert "worktree prune" not in capsys.readouterr().err


def test_env_repo_worktree_kept_on_error_with_prune_reminder(monkeypatch, tmp_path, capsys):
    env_repo, removed = _wire_env_cell(monkeypatch, tmp_path)
    bad = _FakeSession()
    bad.returncode = -1
    monkeypatch.setattr(runner, "_spawn", lambda **kw: bad)
    rc = runner.main(
        [
            "--tier",
            "full",
            "--arms",
            "off",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "5",
        ]
    )
    assert rc == 1  # no ok cell
    assert removed == []  # error worktree kept for forensics
    err = capsys.readouterr().err
    assert "worktree prune" in err
    assert str(env_repo) in err


def test_ci_fixture_worktrees_are_retained(wired, monkeypatch):
    calls, tmp_path = wired
    removed = []
    monkeypatch.setattr(runner, "_remove_worktree", lambda repo, dest: removed.append(dest))
    rc = runner.main(
        [
            "--arms",
            "off,shadow",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "5",
        ]
    )
    assert rc == 0
    assert removed == []  # committed-fixture clones keep their forensic record


def test_trust_granted_exactly_once_per_cell(monkeypatch, tmp_path):
    # Real prepare_cell against a real seed repo: the runner must be the
    # ONLY trust grantor (the worktree layer used to double-grant).
    from tests.journey.harness.fixtures import setup_fixture

    seed = tmp_path / "seed"
    (seed / ".chameleon").mkdir(parents=True)
    (seed / ".chameleon" / "config.json").write_text("{}\n")
    (seed / "src").mkdir()
    (seed / "src" / "a.ts").write_text("export const a = 1;\n")
    repo, _ = setup_fixture("fix", seed, tmp_path / "working")

    task = EffTask(
        task_id="t1-stub-conv",
        tier="ci",
        fixture="ts",
        prompt="p",
        category="convention",
        scorers=("cost",),
    )
    monkeypatch.setattr(runner, "_collect_tasks", lambda: [task])
    monkeypatch.setattr(runner, "_preflight", lambda args, tasks: None)
    monkeypatch.setattr(runner, "_prepare_fixtures", lambda ctx, tasks: {"ts": repo})
    grants = []
    monkeypatch.setattr(runner, "_grant_trust", lambda p: (grants.append(p), "0" * 64)[1])
    monkeypatch.setattr(runner, "_spawn", lambda **kw: _FakeSession())
    rc = runner.main(
        [
            "--arms",
            "off,shadow",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "5",
        ]
    )
    assert rc == 0
    assert len(grants) == 2  # one per cell, no double grant


def test_panel_judges_last_repeat_diff_per_arm(wired, monkeypatch):
    # Pinned semantics: with repeats > 1 the panel reads the LAST repeat's
    # diff for each arm (one representative diff per arm, not a census).
    calls, tmp_path = wired
    monkeypatch.setattr(runner, "_session_diff", lambda wt, sha: f"diff::{wt.name}")
    captured = {}

    def fake_panel(*, task_id, pair, diffs, run_dir):
        captured[task_id] = dict(diffs)
        return {
            "panel_winner": "tie",
            "panel_votes_total": 3,
            "panel_votes_valid": 2,
            "panel_cost_usd": 0.0,
        }

    monkeypatch.setattr(runner, "run_panel", fake_panel)
    rc = runner.main(
        [
            "--arms",
            "off,shadow",
            "--repeats",
            "2",
            "--panel",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "8",
        ]
    )
    assert rc == 0
    diffs = captured["t1-stub-conv"]
    assert diffs["off"].endswith("__r2")
    assert diffs["shadow"].endswith("__r2")


def test_mid_run_budget_abort_marks_skipped(wired, monkeypatch):
    calls, tmp_path = wired
    monkeypatch.setattr(runner, "EST_CELL_USD", 1.0)
    expensive = _FakeSession(cost=3.0)
    monkeypatch.setattr(runner, "_spawn", lambda **kw: expensive)
    rc = runner.main(
        [
            "--arms",
            "off,shadow,enforce",
            "--results-dir",
            str(tmp_path / "results"),
            "--max-budget-usd",
            "4",
        ]
    )
    assert rc == 0
    doc = json.loads(next((tmp_path / "results").glob("effectiveness_*/run.json")).read_text())
    statuses = [c["status"] for c in doc["cells"]]
    assert "skipped" in statuses and "ok" in statuses
    skipped = [c for c in doc["cells"] if c["status"] == "skipped"]
    assert all("budget" in c["reason"] for c in skipped)
