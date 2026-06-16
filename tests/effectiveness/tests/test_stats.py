"""Tests for the effectiveness statistics primitives (Wilson, kappa, bootstrap).

Expected values are hand-computed independently of the implementation.
"""

from __future__ import annotations

import math

import pytest
from tests.effectiveness.stats import (
    cohens_kappa,
    group_by_task,
    paired_bootstrap_ci,
    wilson_lower_bound,
)


def test_wilson_lower_bound_known_value():
    # 9/10 at z=1.96 -> Wilson lower bound ~= 0.596 (hand-computed).
    lb = wilson_lower_bound(9, 10)
    assert math.isclose(lb, 0.5958, abs_tol=0.002)


def test_wilson_lower_bound_perfect_below_one():
    # 40/40 stays below 1 and is high; the standard "near-1 small-n" guard.
    lb = wilson_lower_bound(40, 40)
    assert 0.89 < lb < 1.0


def test_wilson_lower_bound_zero_n():
    assert wilson_lower_bound(0, 0) == 0.0


def test_wilson_lower_bound_monotonic_in_n():
    # Same proportion, more samples -> tighter (higher) lower bound.
    assert wilson_lower_bound(9, 10) < wilson_lower_bound(90, 100)


def test_cohens_kappa_perfect():
    assert cohens_kappa([1, 1, 0, 0], [1, 1, 0, 0]) == 1.0


def test_cohens_kappa_chance_is_zero():
    # observed agreement 0.5, expected 0.5 -> kappa 0.
    assert math.isclose(cohens_kappa([1, 0, 1, 0], [1, 0, 0, 1]), 0.0, abs_tol=1e-9)


def test_cohens_kappa_below_chance_negative():
    assert cohens_kappa([1, 1, 0, 0], [0, 0, 1, 1]) < 0


def test_cohens_kappa_length_mismatch_raises():
    with pytest.raises(ValueError):
        cohens_kappa([1, 0], [1])


def test_paired_bootstrap_all_wins():
    out = paired_bootstrap_ci({"a": [1, 1], "b": [1], "c": [1, 1]})
    assert out["rate"] == 1.0
    assert out["lo"] == 1.0
    assert out["n_tasks"] == 3
    assert out["n_comparisons"] == 5


def test_paired_bootstrap_ties_center_half_lo_below_half():
    out = paired_bootstrap_ci({"a": [0.5], "b": [0.5], "c": [0.5]})
    assert out["rate"] == 0.5
    assert out["lo"] <= 0.5  # ties never clear the >0.5 causal bar


def test_paired_bootstrap_resamples_tasks_not_cells():
    # 1 task with 100 wins must NOT read as n=100; n_tasks drives the CI width.
    out = paired_bootstrap_ci({"only": [1] * 100})
    assert out["n_tasks"] == 1
    assert out["n_comparisons"] == 100
    # A single task gives a degenerate CI (lo == hi == its mean), not a tight one.
    assert out["lo"] == out["hi"] == 1.0


def test_paired_bootstrap_empty():
    out = paired_bootstrap_ci({})
    assert out["rate"] is None and out["n_tasks"] == 0


def test_group_by_task():
    rows = [
        {"task": "a", "win": 1},
        {"task": "a", "win": 0},
        {"task": "b", "win": 1},
    ]
    grouped = group_by_task(rows, task_key="task", win_key="win")
    assert grouped == {"a": [1, 0], "b": [1]}


def test_paired_preference_cis_causal_win():
    from tests.effectiveness.report import paired_preference_cis

    # 4 tasks, treatment 'shadow' wins all -> preference 1.0, lo 1.0 -> CAUSAL WIN
    rows = [
        {"task_id": f"t{i}", "pair": ("off", "shadow"), "panel_winner": "shadow"} for i in range(4)
    ]
    cis = paired_preference_cis(rows)
    assert len(cis) == 1
    c = cis[0]
    assert c["control"] == "off" and c["treatment"] == "shadow"
    assert c["rate"] == 1.0 and c["lo"] == 1.0 and c["n_tasks"] == 4


def test_paired_preference_cis_tie_and_loss_not_established():
    from tests.effectiveness.report import paired_preference_cis

    rows = [
        {"task_id": "a", "pair": ("off", "shadow"), "panel_winner": "off"},
        {"task_id": "b", "pair": ("off", "shadow"), "panel_winner": "tie"},
        {"task_id": "c", "pair": ("off", "shadow"), "panel_winner": "shadow"},
    ]
    cis = paired_preference_cis(rows)
    assert cis[0]["lo"] <= 0.5  # not a causal win
