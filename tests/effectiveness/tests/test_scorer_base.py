"""The {metric}|{"unscored": reason} contract enforced by run_scorer."""

from __future__ import annotations

from pathlib import Path

from tests.effectiveness.scorers import PANEL_SCORER, SCORERS
from tests.effectiveness.scorers.base import ScoreContext, run_scorer, unscored
from tests.effectiveness.tasks import EffTask, TaskPack


def _ctx(tmp_path: Path) -> ScoreContext:
    task = EffTask(
        task_id="t1-x",
        tier="ci",
        fixture="ts",
        prompt="p",
        category="convention",
        scorers=("convention",),
    )
    pack = TaskPack(
        tasks=(task,),
        rubrics={},
        crossfile_targets={},
        duplication_targets={},
        setups={},
        runtime_target_resolvers={},
    )
    return ScoreContext(
        task=task,
        arm="shadow",
        repeat=1,
        worktree=tmp_path,
        baseline_sha="HEAD",
        changed_files=[],
        repo_id="0" * 64,
        session_id="sess-1",
        transcript_path=tmp_path / "t.txt",
        hook_events=[],
        bash_commands=[],
        cost_usd=0.1,
        wall_seconds=2.0,
        pack=pack,
        run_dir=tmp_path,
    )


def test_unknown_scorer_is_unscored(tmp_path):
    out = run_scorer("nope", _ctx(tmp_path))
    assert set(out) == {"unscored"}
    assert "nope" in out["unscored"]


def test_exception_becomes_unscored(tmp_path, monkeypatch):
    monkeypatch.setitem(SCORERS, "boom", lambda ctx: 1 / 0)
    out = run_scorer("boom", _ctx(tmp_path))
    assert set(out) == {"unscored"}
    assert "ZeroDivisionError" in out["unscored"]


def test_bad_shape_becomes_unscored(tmp_path, monkeypatch):
    monkeypatch.setitem(SCORERS, "bad", lambda ctx: ["not", "a", "dict"])
    out = run_scorer("bad", _ctx(tmp_path))
    assert set(out) == {"unscored"}


def test_nan_value_rejected(tmp_path, monkeypatch):
    monkeypatch.setitem(SCORERS, "nan", lambda ctx: {"x": float("nan")})
    out = run_scorer("nan", _ctx(tmp_path))
    assert set(out) == {"unscored"}


def test_valid_metrics_pass_through(tmp_path, monkeypatch):
    monkeypatch.setitem(
        SCORERS, "ok", lambda ctx: {"violations": 3, "rate": 0.5, "seen": True, "why": "x"}
    )
    out = run_scorer("ok", _ctx(tmp_path))
    assert out == {"violations": 3, "rate": 0.5, "seen": True, "why": "x"}


def test_registry_names_match_spec():
    assert set(SCORERS) == {"convention", "crossfile", "duplication", "verification", "cost"}
    assert PANEL_SCORER == "judge_panel"


def test_unscored_truncates_reason():
    out = unscored("x" * 2000)
    assert len(out["unscored"]) <= 500
