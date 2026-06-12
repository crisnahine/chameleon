"""Aggregation, baseline deltas with the 20% banner, output emission."""

from __future__ import annotations

import json

from tests.effectiveness.report import (
    aggregate,
    compare_to_baseline,
    render_run_md,
    write_outputs,
)


def _cell(arm, category="convention", status="ok", scores=None):
    return {
        "task_id": f"t1-{category}-x",
        "category": category,
        "fixture": "ts",
        "arm": arm,
        "repeat": 1,
        "status": status,
        "reason": None if status == "ok" else "boom",
        "session": {
            "session_id": "s",
            "cost_usd": 0.2,
            "wall_seconds": 30.0,
            "returncode": 0,
            "transcript": "t.txt",
            "baseline_sha": "abc",
            "model": "sonnet",
        },
        "scores": scores or {},
    }


def test_aggregate_findings_and_rates():
    cells = [
        _cell(
            "off",
            scores={
                "convention": {"violations": 4},
                "verification": {"test_cmd_in_transcript": False},
                "cost": {"cost_usd": 0.30, "wall_seconds": 60.0},
            },
        ),
        _cell(
            "off",
            scores={
                "convention": {"violations": 2},
                "verification": {"test_cmd_in_transcript": True},
                "cost": {"cost_usd": 0.10, "wall_seconds": 20.0},
            },
        ),
        _cell(
            "shadow",
            scores={
                "convention": {"violations": 1},
                "verification": {"test_cmd_in_transcript": True},
                "cost": {"cost_usd": 0.25, "wall_seconds": 50.0},
            },
        ),
    ]
    aggs = aggregate(cells)
    assert aggs["convention|off"]["findings_per_task"] == 3.0
    assert aggs["convention|off"]["verification_rate"] == 0.5
    assert aggs["convention|off"]["cost_usd_mean"] == 0.2
    assert aggs["convention|shadow"]["findings_per_task"] == 1.0


def test_aggregate_excludes_errors_and_unscored():
    cells = [
        _cell("off", scores={"convention": {"violations": 4}}),
        _cell("off", status="error"),
        _cell("off", scores={"convention": {"unscored": "no archetype"}}),
    ]
    aggs = aggregate(cells)
    assert aggs["convention|off"]["cells"] == 2  # ok cells; error excluded
    assert aggs["convention|off"]["findings_per_task"] == 4.0  # unscored excluded from mean


def test_duplication_rate_definition():
    cells = [
        _cell(
            "off",
            category="duplication",
            scores={
                "duplication": {
                    "added_functions": 1,
                    "body_hash_duplicates": 1,
                    "reuse_credit": False,
                }
            },
        ),
        _cell(
            "off",
            category="duplication",
            scores={
                "duplication": {
                    "added_functions": 0,
                    "body_hash_duplicates": 0,
                    "reuse_credit": True,
                }
            },
        ),
    ]
    aggs = aggregate(cells)
    assert aggs["duplication|off"]["duplication_rate"] == 0.5


def test_baseline_regression_banner_direction_aware():
    aggs = {
        "convention|shadow": {
            "cells": 2,
            "findings_per_task": 3.0,
            "verification_rate": 0.5,
            "duplication_rate": None,
            "cost_usd_mean": 0.2,
            "wall_seconds_mean": 30.0,
        },
    }
    baselines = {
        "schema_version": 1,
        "baselines": {
            "ci": {
                "convention": {
                    "shadow": {
                        "findings_per_task": 2.0,
                        "verification_rate": 1.0,
                        "run_id": "r0",
                    }
                }
            }
        },
    }
    rows = compare_to_baseline(aggs, baselines, tier="ci")
    by_metric = {r["metric"]: r for r in rows}
    # findings 2.0 -> 3.0 = +50% on a lower-is-better metric: regression
    assert by_metric["findings_per_task"]["regression"] is True
    # verification 1.0 -> 0.5 = -50% on a higher-is-better metric: regression
    assert by_metric["verification_rate"]["regression"] is True


def test_small_worsening_not_flagged():
    aggs = {
        "convention|shadow": {
            "cells": 1,
            "findings_per_task": 2.2,
            "verification_rate": None,
            "duplication_rate": None,
            "cost_usd_mean": None,
            "wall_seconds_mean": None,
        }
    }
    baselines = {
        "schema_version": 1,
        "baselines": {
            "ci": {"convention": {"shadow": {"findings_per_task": 2.0, "run_id": "r0"}}}
        },
    }
    rows = compare_to_baseline(aggs, baselines, tier="ci")
    assert rows[0]["regression"] is False  # +10% within tolerance


def test_run_md_shows_errors_and_banner(tmp_path):
    cells = [
        _cell("off", scores={"convention": {"violations": 1}}),
        _cell("shadow", status="error"),
    ]
    aggs = aggregate(cells)
    deltas = [
        {
            "category": "convention",
            "arm": "off",
            "metric": "findings_per_task",
            "baseline": 0.5,
            "current": 1.0,
            "delta_pct": 100.0,
            "regression": True,
        }
    ]
    md = render_run_md(
        run_id="effectiveness_x",
        tier="ci",
        arms=["off", "shadow"],
        model="sonnet",
        toggle=None,
        cells=cells,
        aggregates=aggs,
        deltas=deltas,
        panel_rows=[],
        total_cost_usd=0.5,
    )
    assert "errors: 1" in md
    assert "REGRESSION" in md
    assert "boom" in md  # error reason surfaced, never silently dropped


def test_write_outputs_shapes(tmp_path):
    cells = [_cell("off", scores={"convention": {"violations": 1}})]
    run_doc = {
        "run_id": "effectiveness_x",
        "tier": "ci",
        "arms": ["off"],
        "model": "sonnet",
        "toggle": None,
        "cells": cells,
        "panel": [],
        "aggregates": aggregate(cells),
        "baseline_deltas": [],
        "errors": 0,
        "total_cost_usd": 0.2,
    }
    write_outputs(tmp_path, run_doc)
    loaded = json.loads((tmp_path / "run.json").read_text())
    assert loaded["run_id"] == "effectiveness_x"
    assert (tmp_path / "run.md").is_file()
