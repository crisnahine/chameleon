"""Cost-charged quality: per-arm turn overhead + cost-adjusted lift rows.

An effectiveness claim must net the judged lift against dollars, wall time,
and turns. These rows are informational (inform, never block), like the 20%
regression banner.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.effectiveness.report import (
    arm_overhead,
    cost_adjusted_rows,
    paired_preference_cis,
    render_run_md,
)


def _cell(
    arm,
    *,
    category="convention",
    status="ok",
    reason=None,
    cost=0.2,
    wall=30.0,
    num_turns=None,
    result_subtype=None,
    task_id=None,
):
    session = {
        "session_id": "s",
        "cost_usd": cost,
        "wall_seconds": wall,
        "returncode": 0 if status == "ok" else 1,
        "transcript": "t.txt",
        "baseline_sha": "abc",
        "model": "sonnet",
    }
    if num_turns is not None:
        session["num_turns"] = num_turns
    if result_subtype is not None:
        session["result_subtype"] = result_subtype
    return {
        "task_id": task_id or f"t1-{category}-x",
        "category": category,
        "fixture": "ts",
        "arm": arm,
        "repeat": 1,
        "status": status,
        "reason": reason,
        "session": session,
        "scores": {},
    }


def _two_arm_cells():
    """off: 2 ok cells (turns 10/20, $0.4/$0.6, wall 50/70).
    shadow: 2 ok cells (turns 22/26, $0.9/$1.1, wall 170/190) + 1 cell that
    died at the turn cap (error_max_turns, excluded from every mean)."""
    return [
        _cell("off", num_turns=10, cost=0.4, wall=50.0, task_id="tA"),
        _cell("off", num_turns=20, cost=0.6, wall=70.0, task_id="tB"),
        _cell("shadow", num_turns=22, cost=0.9, wall=170.0, task_id="tA"),
        _cell("shadow", num_turns=26, cost=1.1, wall=190.0, task_id="tB"),
        _cell(
            "shadow",
            status="error",
            reason="session returncode 1 (error_max_turns)",
            cost=0.97,
            wall=240.0,
            num_turns=31,
            result_subtype="error_max_turns",
            task_id="tC",
        ),
    ]


def _panel_rows():
    # rate for shadow over off = mean(1.0, 0.5) = 0.75
    return [
        {
            "task_id": "tA",
            "pair": ["off", "shadow"],
            "panel_winner": "shadow",
            "panel_votes_valid": 3,
            "panel_cost_usd": 0.1,
        },
        {
            "task_id": "tB",
            "pair": ["off", "shadow"],
            "panel_winner": "tie",
            "panel_votes_valid": 3,
            "panel_cost_usd": 0.1,
        },
    ]


def _render(cells, panel_rows):
    return render_run_md(
        run_id="effectiveness_x",
        tier="full",
        arms=["off", "shadow"],
        model="sonnet",
        toggle=None,
        cells=cells,
        aggregates={},
        deltas=[],
        panel_rows=panel_rows,
        total_cost_usd=4.17,
    )


# --- arm_overhead ------------------------------------------------------------


def test_arm_overhead_means_over_ok_cells_only():
    rollup = arm_overhead(_two_arm_cells())
    assert rollup["off"]["turns_mean"] == 15.0
    assert rollup["off"]["cost_usd_mean"] == 0.5
    assert rollup["off"]["wall_seconds_mean"] == 60.0
    assert rollup["off"]["error_max_turns"] == 0
    # The truncated shadow cell is counted, never averaged in.
    assert rollup["shadow"]["turns_mean"] == 24.0
    assert rollup["shadow"]["cost_usd_mean"] == 1.0
    assert rollup["shadow"]["wall_seconds_mean"] == 180.0
    assert rollup["shadow"]["error_max_turns"] == 1


def test_arm_overhead_without_turn_data_is_none_not_zero():
    rollup = arm_overhead([_cell("off")])  # legacy cell: no num_turns recorded
    assert rollup["off"]["turns_mean"] is None
    assert rollup["off"]["error_max_turns"] == 0


# --- cost_adjusted_rows -------------------------------------------------------


def test_cost_adjusted_exact_arithmetic():
    cells = _two_arm_cells()
    prefs = paired_preference_cis(_panel_rows())
    rows = cost_adjusted_rows(prefs, arm_overhead(cells))
    assert len(rows) == 1
    r = rows[0]
    assert r["control"] == "off" and r["treatment"] == "shadow"
    assert r["preference"] == 0.75
    # (0.75 - 0.5) / (1.0 - 0.5)
    assert r["lift_per_dollar"] == 0.5
    # (0.75 - 0.5) / ((180.0 - 60.0) / 60)
    assert r["lift_per_wall_minute"] == 0.125


def test_cost_adjusted_guards_non_costlier_treatment():
    cells = [
        _cell("off", num_turns=10, cost=1.0, wall=200.0, task_id="tA"),
        _cell("shadow", num_turns=12, cost=1.0, wall=100.0, task_id="tA"),
    ]
    prefs = paired_preference_cis(
        [
            {
                "task_id": "tA",
                "pair": ["off", "shadow"],
                "panel_winner": "shadow",
                "panel_votes_valid": 3,
                "panel_cost_usd": 0.1,
            }
        ]
    )
    rows = cost_adjusted_rows(prefs, arm_overhead(cells))
    # Equal cost (delta 0) and cheaper wall (delta < 0): both denominators <= 0.
    assert rows[0]["lift_per_dollar"] is None
    assert rows[0]["lift_per_wall_minute"] is None


# --- run.md rendering ---------------------------------------------------------


def test_run_md_per_arm_turns_and_max_turns_rows():
    md = _render(_two_arm_cells(), _panel_rows())
    assert "turns_mean" in md
    assert "error_max_turns" in md
    assert "| off | 2 | 15.0 | 0 | 0.5 | 60.0 |" in md
    assert "| shadow | 2 | 24.0 | 1 | 1.0 | 180.0 |" in md


def test_run_md_cost_adjusted_section_exact_numbers():
    md = _render(_two_arm_cells(), _panel_rows())
    assert "## Cost-adjusted lift (advisory, never blocking)" in md
    assert "| off | shadow | 0.750 | 0.5000 | 0.1250 |" in md


def test_run_md_cost_adjusted_denominator_guard():
    cells = [
        _cell("off", num_turns=10, cost=1.0, wall=200.0, task_id="tA"),
        _cell("shadow", num_turns=12, cost=0.5, wall=100.0, task_id="tA"),
    ]
    panel = [
        {
            "task_id": "tA",
            "pair": ["off", "shadow"],
            "panel_winner": "shadow",
            "panel_votes_valid": 3,
            "panel_cost_usd": 0.1,
        }
    ]
    md = _render(cells, panel)
    assert md.count("n/a (arm B not costlier)") == 2  # dollar AND wall columns


def test_run_md_cost_adjusted_without_judged_preference():
    md = _render(_two_arm_cells(), [])
    assert "## Cost-adjusted lift (advisory, never blocking)" in md
    assert "n/a (no judged preference)" in md
    # The per-arm overhead rows still render without a panel.
    assert "| shadow | 2 | 24.0 | 1 | 1.0 | 180.0 |" in md


# --- runner records num_turns + result subtype from the transcript ------------


def test_runner_result_meta_reads_terminal_result_event(tmp_path):
    from tests.effectiveness.runner import _result_meta

    transcript = tmp_path / "cell.txt"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "assistant", "message": {}}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "error_max_turns",
                        "is_error": True,
                        "num_turns": 31,
                        "total_cost_usd": 0.97,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    assert _result_meta(transcript) == {"num_turns": 31, "result_subtype": "error_max_turns"}


def test_runner_result_meta_fails_open(tmp_path):
    from tests.effectiveness.runner import _result_meta

    assert _result_meta(tmp_path / "missing.txt") == {}
    garbled = tmp_path / "garbled.txt"
    garbled.write_text("not json\n{broken\n", encoding="utf-8")
    assert _result_meta(garbled) == {}


def test_runner_records_turns_into_run_json(monkeypatch, tmp_path):
    """End to end through _execute: the cell's session row carries num_turns,
    and a max-turns death is named in the error reason."""
    from tests.effectiveness import runner
    from tests.effectiveness.tasks import EffTask

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
        runner, "_prepare_fixtures", lambda ctx, tasks: {"ts": tmp_path / "fixture_repo"}
    )

    def fake_prepare_cell(**kw):
        kw["dest"].mkdir(parents=True, exist_ok=True)
        return "deadbeef"

    monkeypatch.setattr(runner, "_prepare_cell", lambda **kw: fake_prepare_cell(**kw))
    monkeypatch.setattr(runner, "_grant_trust", lambda p: "0" * 64)
    monkeypatch.setattr(runner, "_changed_files", lambda wt, sha: [])
    monkeypatch.setattr(runner, "_session_diff", lambda wt, sha: "diff")

    class _Session:
        def __init__(self, returncode):
            self.cost_usd = 0.05
            self.hook_events = []
            self.returncode = returncode
            self.tool_uses = []
            self.session_id = "sess-1"
            self.result_text = ""
            self.bash_commands = []

    results = [
        ("success", 12, 0),
        ("error_max_turns", 31, 1),
    ]

    def fake_spawn(**kw):
        subtype, turns, returncode = results.pop(0)
        Path(kw["transcript_path"]).write_text(
            json.dumps({"type": "result", "subtype": subtype, "num_turns": turns}) + "\n",
            encoding="utf-8",
        )
        return _Session(returncode)

    monkeypatch.setattr(runner, "_spawn", lambda **kw: fake_spawn(**kw))
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
    doc = json.loads(next((tmp_path / "results").glob("effectiveness_*/run.json")).read_text())
    by_arm = {c["arm"]: c for c in doc["cells"]}
    assert by_arm["off"]["session"]["num_turns"] == 12
    assert by_arm["off"]["session"]["result_subtype"] == "success"
    assert by_arm["shadow"]["status"] == "error"
    assert by_arm["shadow"]["session"]["num_turns"] == 31
    assert by_arm["shadow"]["session"]["result_subtype"] == "error_max_turns"
    assert "error_max_turns" in by_arm["shadow"]["reason"]
