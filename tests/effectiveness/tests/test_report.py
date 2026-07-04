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


def test_aggregate_component_breakout():
    cells = [
        _cell(
            "off",
            category="crossfile",
            scores={
                "convention": {"violations": 7},
                "crossfile": {
                    "broken_exports": 1,
                    "callers_total": 8,
                    "callers_updated": 6,
                    "callers_stale": 2,
                },
            },
        ),
        _cell(
            "off",
            category="crossfile",
            scores={
                "convention": {"violations": 0},
                "crossfile": {
                    "broken_exports_unscored": "unsupported-language",
                    "callers_total": 7,
                    "callers_updated": 7,
                    "callers_stale": 0,
                },
            },
        ),
    ]
    aggs = aggregate(cells)
    m = aggs["crossfile|off"]
    # findings_per_task stays (baseline continuity) ...
    assert m["findings_per_task"] == 5.0  # ((7+1+2) + (0+0)) / 2
    # ... and each component is reported on its own.
    assert m["conv_violations_mean"] == 3.5
    assert m["broken_exports_mean"] == 1.0  # only the cell that scored it
    assert m["callers_stale_mean"] == 1.0


def test_run_md_breaks_findings_out_per_scorer():
    cells = [
        _cell(
            "off",
            category="crossfile",
            scores={
                "convention": {"violations": 7},
                "crossfile": {"broken_exports": 1, "callers_stale": 2},
            },
        ),
    ]
    md = render_run_md(
        run_id="effectiveness_x",
        tier="ci",
        arms=["off"],
        model="sonnet",
        toggle=None,
        cells=cells,
        aggregates=aggregate(cells),
        deltas=[],
        panel_rows=[],
        total_cost_usd=0.2,
    )
    assert "| conv viol | broken exp | stale callers |" in md
    assert "findings/task" not in md
    # The crossfile|off row carries the per-scorer values, not one blended sum.
    assert "| crossfile | off | 1 | 7.0 | 1.0 | 2.0 |" in md


def test_real_scorer_shapes_flow_into_aggregate(tmp_path, monkeypatch):
    """Contract: feed every REAL scorer's output through aggregate, so a
    scorer-side metric-key rename breaks this test instead of silently
    emptying run.md columns."""
    from tests.effectiveness.scorers import convention, cost, verification
    from tests.effectiveness.tests.test_scorer_base import _ctx
    from tests.effectiveness.tests.test_scorer_crossfile import (
        CROSSFILE_TWO_HIGH,
    )
    from tests.effectiveness.tests.test_scorer_crossfile import (
        _make_ctx as _crossfile_ctx,
    )
    from tests.effectiveness.tests.test_scorer_duplication import (
        _make_ctx as _dup_ctx,
    )
    from tests.effectiveness.tests.test_scorer_duplication import (
        _pf,
    )

    # convention: one real violation through the real scorer.
    conv_ctx = _ctx(tmp_path / "conv")
    (tmp_path / "conv" / "src").mkdir(parents=True)
    (tmp_path / "conv" / "src" / "A.tsx").write_text("export const A = 1;\n")
    conv_ctx.changed_files = ["src/A.tsx"]
    monkeypatch.setattr(
        convention,
        "_pattern_context",
        lambda path: {"data": {"archetype": {"archetype": "component"}}},
    )
    monkeypatch.setattr(
        convention,
        "_lint",
        lambda **kw: {"data": {"violations": [{"rule": "x", "severity": "warning"}]}},
    )
    conv_out = convention.score(conv_ctx)

    # crossfile: real scorer over a real tmp repo (one stale caller).
    cf_ctx = _crossfile_ctx(
        tmp_path / "cf",
        monkeypatch,
        {
            "src/a.ts": "formatCurrency(1);\n",
            "src/b.ts": "formatCurrency(2);\n",
            "src/c.ts": "formatMoney(3);\n",
        },
        CROSSFILE_TWO_HIGH,
    )
    from tests.effectiveness.scorers import crossfile as crossfile_mod

    cf_out = crossfile_mod.score(cf_ctx)

    # duplication: real scorer, one body-hash clone, no reuse.
    dup_ctx = _dup_ctx(
        tmp_path / "dup",
        monkeypatch,
        {"src/components/Card.tsx": [_pf("makeSlug", "aaaa000011112222", "zzzz")]},
    )
    from tests.effectiveness.scorers import duplication as duplication_mod

    dup_out = duplication_mod.score(dup_ctx)

    # verification: real scorer with a classifying test command.
    ver_ctx = _ctx(tmp_path / "ver")
    ver_ctx.bash_commands = ["npm test"]
    monkeypatch.setattr(verification, "_session_test_run_seen", lambda r, s: True)
    ver_out = verification.score(ver_ctx)

    # cost: real scorer.
    cost_out = cost.score(_ctx(tmp_path / "cost"))

    for out in (conv_out, cf_out, dup_out, ver_out, cost_out):
        assert "unscored" not in out, out

    cells = [
        _cell(
            "shadow",
            category="crossfile",
            scores={
                "convention": conv_out,
                "crossfile": cf_out,
                "verification": ver_out,
                "cost": cost_out,
            },
        ),
        _cell("shadow", category="duplication", scores={"duplication": dup_out}),
    ]
    aggs = aggregate(cells)
    m = aggs["crossfile|shadow"]
    assert m["findings_per_task"] == 4.0  # 1 violation + 2 broken + 1 stale
    assert m["conv_violations_mean"] == 1.0
    assert m["broken_exports_mean"] == 2.0
    assert m["callers_stale_mean"] == 1.0
    assert m["verification_rate"] == 1.0
    assert m["cost_usd_mean"] is not None
    assert m["wall_seconds_mean"] is not None
    assert aggs["duplication|shadow"]["duplication_rate"] == 1.0


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
        "baselines": {"ci": {"convention": {"shadow": {"findings_per_task": 2.0, "run_id": "r0"}}}},
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


# --- model-tier aggregation + model-aware baselines (#5) ---------------------


def _model_cell(arm, model, category="convention", violations=2):
    c = _cell(arm, category=category, scores={"convention": {"violations": violations}})
    c["model"] = model
    return c


def test_aggregate_attaches_uniform_arm_model():
    aggs = aggregate([_model_cell("shadow", "opus"), _model_cell("shadow", "opus")])
    assert aggs["convention|shadow"]["model"] == "opus"


def test_aggregate_defaults_missing_model_to_sonnet():
    # Legacy cells (pre-model-capture) have no top-level model; aggregate must
    # fall back to sonnet rather than crash or key on None.
    cells = [_cell("shadow", scores={"convention": {"violations": 1}})]
    assert cells[0].get("model") is None
    assert aggregate(cells)["convention|shadow"]["model"] == "sonnet"


def test_legacy_flat_baseline_only_answers_sonnet():
    from tests.effectiveness.report import _resolve_arm_baseline

    flat = {"findings_per_task": 3.0, "cells": 4}
    assert _resolve_arm_baseline(flat, "sonnet") == flat
    # A non-sonnet arm gets no baseline from a sonnet-only flat entry.
    assert _resolve_arm_baseline(flat, "opus") == {}


def test_model_keyed_baseline_selects_by_model():
    from tests.effectiveness.report import _resolve_arm_baseline

    keyed = {"sonnet": {"findings_per_task": 3.0}, "opus": {"findings_per_task": 1.0}}
    assert _resolve_arm_baseline(keyed, "opus") == {"findings_per_task": 1.0}
    assert _resolve_arm_baseline(keyed, "fable") == {}  # no baseline for this model yet


def test_compare_skips_cross_model_against_legacy_baseline():
    # An opus arm compared against a sonnet-only (legacy flat) baseline must NOT
    # produce a regression row (apples-to-oranges), while the sonnet arm does.
    aggs = aggregate([_model_cell("shadow", "opus", violations=9)])
    baselines = {"baselines": {"ci": {"convention": {"shadow": {"findings_per_task": 1.0}}}}}
    rows = compare_to_baseline(aggs, baselines, tier="ci")
    assert rows == []  # opus vs sonnet-flat -> no comparison

    aggs_sonnet = aggregate([_model_cell("shadow", "sonnet", violations=9)])
    rows_sonnet = compare_to_baseline(aggs_sonnet, baselines, tier="ci")
    assert any(r["metric"] == "findings_per_task" and r["model"] == "sonnet" for r in rows_sonnet)
