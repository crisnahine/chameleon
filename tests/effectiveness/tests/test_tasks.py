"""EffTask model + registry validation."""

from __future__ import annotations

import pytest
from tests.effectiveness.tasks import (
    EffTask,
    TaskValidationError,
    collect_tasks,
    load_packs,
    validate_task,
)

KNOWN_SCORERS = {"convention", "crossfile", "duplication", "verification", "judge_panel", "cost"}


def _task(**over):
    base = dict(
        task_id="t1-x",
        tier="ci",
        fixture="ts",
        prompt="do something",
        category="convention",
        scorers=("convention", "cost"),
    )
    base.update(over)
    return EffTask(**base)


def test_valid_task_passes():
    validate_task(_task(), KNOWN_SCORERS, set())


def test_duplicate_id_rejected():
    with pytest.raises(TaskValidationError, match="duplicate"):
        validate_task(_task(), KNOWN_SCORERS, {"t1-x"})


def test_bad_tier_fixture_combo_rejected():
    with pytest.raises(TaskValidationError, match="fixture"):
        validate_task(_task(tier="ci", fixture="env-ts"), KNOWN_SCORERS, set())
    with pytest.raises(TaskValidationError, match="fixture"):
        validate_task(_task(tier="full", fixture="ts"), KNOWN_SCORERS, set())


def test_unknown_scorer_rejected():
    with pytest.raises(TaskValidationError, match="scorer"):
        validate_task(_task(scorers=("nope",)), KNOWN_SCORERS, set())


def test_empty_prompt_rejected():
    with pytest.raises(TaskValidationError, match="prompt"):
        validate_task(_task(prompt="  "), KNOWN_SCORERS, set())


def test_collect_tasks_returns_validated_registry():
    tasks = collect_tasks()
    ids = [t.task_id for t in tasks]
    assert len(ids) == len(set(ids))
    assert all(t.tier in ("ci", "full") for t in tasks)
    # Tier-ci target from the spec: 8 tasks, 2 per category.
    ci = [t for t in tasks if t.tier == "ci"]
    assert len(ci) == 8


def test_load_packs_setups_resolve():
    pack = load_packs()
    for task in pack.tasks:
        if task.setup is not None:
            assert task.setup in pack.setups, f"{task.task_id} names unknown setup {task.setup}"
