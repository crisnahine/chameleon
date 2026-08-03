"""A worker that dies mid-act must turn the run red.

The gate run this guard was written for reported 35 PASS off 20 acts, 9 of whose
workers had ended on a deferred tool without executing it. Two things let that
happen: an act promotes a SKIP phase to PASS on a transcript cross-check, which
a dead worker's transcript can still satisfy, and the runner only ever set
any_failed on FAIL/ERROR, so a run of pure SKIPs still exited 0.

Both halves are pinned here: every spawning act reports its session's end-state,
and the runner turns an abnormal end-state into ERROR rows plus a non-zero exit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.journey import runner
from tests.journey.acts.act_base import ActResult
from tests.journey.harness.checkpoints import PhaseOutcome
from tests.journey.harness.context import build_context

_ACTS_DIR = Path(runner.__file__).resolve().parent / "acts"
# The preflight act spawns no worker, so it has no session end-state to report.
_NON_SPAWNING_ACTS = {"act_00_preflight.py", "act_base.py"}


def test_every_spawning_act_reports_its_session_end_state() -> None:
    """An act that omits the field defaults to "" and reads as a clean run, so
    the guard erodes silently one act at a time without this check."""
    missing = []
    for path in sorted(_ACTS_DIR.glob("act_*.py")):
        if path.name in _NON_SPAWNING_ACTS:
            continue
        source = path.read_text(encoding="utf-8")
        if "terminal_reason=session.terminal_reason" not in source:
            missing.append(path.name)
    assert not missing, f"acts not reporting their session end-state: {missing}"


def test_every_spawning_act_is_covered_by_that_check() -> None:
    """The exemption set is a literal, so an act renamed into it would opt out
    of the guard unnoticed."""
    names = {p.name for p in _ACTS_DIR.glob("act_*.py")}
    assert _NON_SPAWNING_ACTS <= names
    assert len(names - _NON_SPAWNING_ACTS) >= 20


def _run_one_stub_act(monkeypatch, tmp_path: Path, act_result: ActResult) -> tuple[int, list[dict]]:
    """Drive _run_acts over a single act module that returns act_result."""
    ctx = build_context(
        plugin_root=tmp_path / "plugin",
        results_root=tmp_path / "results",
        run_prefix="journey_test",
        repo_root=tmp_path,
    )
    args = argparse.Namespace(max_budget_usd=100.0)

    class _StubModule:
        @staticmethod
        def run(_ctx):
            return act_result

    real_import_module = runner.importlib.import_module

    def fake_import_module(name, *rest):
        if name.startswith("tests.journey.acts."):
            return _StubModule
        return real_import_module(name, *rest)

    monkeypatch.setattr(runner.importlib, "import_module", fake_import_module)
    rc = runner._run_acts(ctx, args, [("02_init_flow", "stub", 1.0, [1, 2])])
    results = json.loads((ctx.run_dir / "run.json").read_text(encoding="utf-8"))
    return rc, results["results"]


def test_a_deferred_worker_turns_reported_passes_into_errors(monkeypatch, tmp_path) -> None:
    rc, results = _run_one_stub_act(
        monkeypatch,
        tmp_path,
        ActResult(
            act_id="02_init_flow",
            cost_usd=0.21,
            phase_outcomes=[
                PhaseOutcome(phase=1, status="PASS", notes="promoted from SKIP by cross-check"),
                PhaseOutcome(phase=2, status="SKIP", notes="no checkpoint"),
            ],
            terminal_reason="tool_deferred",
        ),
    )
    assert rc == 1
    assert [r["status"] for r in results] == ["ERROR", "ERROR"]
    assert "tool_deferred" in results[0]["notes"]
    # The act's own verdict survives in the notes rather than being discarded.
    assert "PASS" in results[0]["notes"]
    assert "SKIP" in results[1]["notes"]


def test_a_turn_capped_worker_that_finished_every_phase_keeps_its_outcomes(
    monkeypatch, tmp_path
) -> None:
    """The turn cap is a budget fact, not a lost result.

    An act that emitted every phase it declared holds the whole result it came
    for; voiding those outcomes because the session then ran out of turns
    discards work the worker demonstrably did, and fails a release gate for a
    reason unrelated to what it tests.
    """
    rc, results = _run_one_stub_act(
        monkeypatch,
        tmp_path,
        ActResult(
            act_id="02_init_flow",
            cost_usd=0.30,
            phase_outcomes=[
                PhaseOutcome(phase=1, status="PASS", notes=""),
                PhaseOutcome(phase=2, status="PASS", notes=""),
            ],
            terminal_reason="max_turns",
        ),
    )
    assert rc == 0
    assert [r["status"] for r in results] == ["PASS", "PASS"]


def test_a_turn_capped_worker_missing_a_phase_still_errors(monkeypatch, tmp_path) -> None:
    """The complement: a phase with no events lands as SKIP, and SKIP alone
    never fails a run, so an act that ran out of turns mid-way has to be caught
    here or the gate goes green on partial work."""
    rc, results = _run_one_stub_act(
        monkeypatch,
        tmp_path,
        ActResult(
            act_id="02_init_flow",
            cost_usd=0.30,
            phase_outcomes=[
                PhaseOutcome(phase=1, status="PASS", notes=""),
                PhaseOutcome(phase=2, status="SKIP", notes="phase not attempted"),
            ],
            terminal_reason="max_turns",
        ),
    )
    assert rc == 1
    assert [r["status"] for r in results] == ["ERROR", "ERROR"]
    assert "max_turns" in results[0]["notes"]


def test_a_completed_worker_keeps_its_reported_outcomes(monkeypatch, tmp_path) -> None:
    rc, results = _run_one_stub_act(
        monkeypatch,
        tmp_path,
        ActResult(
            act_id="02_init_flow",
            cost_usd=0.12,
            phase_outcomes=[
                PhaseOutcome(phase=1, status="PASS", notes=""),
                PhaseOutcome(phase=2, status="PASS", notes=""),
            ],
            terminal_reason="completed",
        ),
    )
    assert rc == 0
    assert [r["status"] for r in results] == ["PASS", "PASS"]
