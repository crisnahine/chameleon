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
