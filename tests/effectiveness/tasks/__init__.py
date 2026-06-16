"""Task model + registry for the effectiveness eval.

A task is a frozen EffTask (shape fixed by the spec). Packs are plain modules
exposing:
  TASKS: list[EffTask]                       (required)
  RUBRICS: dict[task_id, callable]           (optional; worktree: Path -> dict)
  CROSSFILE_TARGETS: dict[task_id, dict]     (optional;
                                              {"module","function","new_name","old_needle"};
                                              old_needle is the qualified call form the
                                              staleness grep matches, e.g. "formatMoney(")
  DUPLICATION_TARGETS: dict[task_id, dict]   (optional; {"existing_name","existing_file","needle"})
  SETUPS: dict[name, callable]               (optional; worktree: Path -> None)
  RUNTIME_TARGET_RESOLVERS: dict[task_id, callable]  (optional; repo_root -> dict | None,
                                              tier-full crossfile tasks only)

collect_tasks() imports every pack, validates, and returns the flat task list;
load_packs() additionally merges the aux dicts.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Callable

VALID_TIERS = ("ci", "full", "dup")
VALID_FIXTURES = ("ts", "rails", "env-ts", "env-ruby")
# tier "dup" reuses the env-pointed real repos (same fixtures as full); it is a
# separate tier so the large duplication-reuse corpus runs in isolation from the
# 8 tier-full tasks for the powered causal A/B.
TIER_FIXTURES = {
    "ci": ("ts", "rails"),
    "full": ("env-ts", "env-ruby"),
    "dup": ("env-ts", "env-ruby"),
}
VALID_CATEGORIES = ("convention", "crossfile", "duplication", "verification")

_PACK_MODULES = (
    "tests.effectiveness.tasks.tier1_ts",
    "tests.effectiveness.tasks.tier1_rails",
    "tests.effectiveness.tasks.tier2_ts",
    "tests.effectiveness.tasks.tier2_rails",
    "tests.effectiveness.tasks.tier3_dup_ts",
    "tests.effectiveness.tasks.tier3_dup_rb",
)


@dataclasses.dataclass(frozen=True)
class EffTask:
    task_id: str  # "t1-convention-fetch-helper"
    tier: str  # "ci" | "full"
    fixture: str  # "ts" | "rails" (tier ci) | "env-ts" | "env-ruby" (tier full)
    prompt: str  # the exact user prompt, identical across arms
    category: str  # convention | crossfile | duplication | verification
    scorers: tuple[str, ...]  # scorer names from the registry
    repeats: int = 1
    max_turns: int = 12
    setup: str | None = None  # optional callable name: pre-task repo mutation


class TaskValidationError(Exception):
    pass


def validate_task(task: EffTask, known_scorers: set[str], seen_ids: set[str]) -> None:
    if not task.task_id or not isinstance(task.task_id, str):
        raise TaskValidationError(f"task_id must be a non-empty string: {task!r}")
    if task.task_id in seen_ids:
        raise TaskValidationError(f"duplicate task_id {task.task_id!r}")
    if task.tier not in VALID_TIERS:
        raise TaskValidationError(f"{task.task_id}: tier must be one of {VALID_TIERS}")
    if task.fixture not in TIER_FIXTURES[task.tier]:
        raise TaskValidationError(
            f"{task.task_id}: fixture {task.fixture!r} invalid for tier {task.tier!r} "
            f"(allowed: {TIER_FIXTURES[task.tier]})"
        )
    if task.category not in VALID_CATEGORIES:
        raise TaskValidationError(f"{task.task_id}: category must be one of {VALID_CATEGORIES}")
    if not task.prompt or not task.prompt.strip():
        raise TaskValidationError(f"{task.task_id}: prompt must be non-empty")
    if not task.scorers:
        raise TaskValidationError(f"{task.task_id}: at least one scorer required")
    unknown = [s for s in task.scorers if s not in known_scorers]
    if unknown:
        raise TaskValidationError(f"{task.task_id}: unknown scorer(s) {unknown}")
    if task.repeats < 1:
        raise TaskValidationError(f"{task.task_id}: repeats must be >= 1")
    if task.max_turns < 1:
        raise TaskValidationError(f"{task.task_id}: max_turns must be >= 1")


@dataclasses.dataclass(frozen=True)
class TaskPack:
    tasks: tuple[EffTask, ...]
    rubrics: dict[str, Callable]
    crossfile_targets: dict[str, dict]
    duplication_targets: dict[str, dict]
    setups: dict[str, Callable]
    runtime_target_resolvers: dict[str, Callable]


def _known_scorers() -> set[str]:
    from tests.effectiveness.scorers import PANEL_SCORER, SCORERS

    return set(SCORERS) | {PANEL_SCORER}


def load_packs() -> TaskPack:
    tasks: list[EffTask] = []
    rubrics: dict[str, Callable] = {}
    crossfile: dict[str, dict] = {}
    duplication: dict[str, dict] = {}
    setups: dict[str, Callable] = {}
    resolvers: dict[str, Callable] = {}
    known = _known_scorers()
    seen: set[str] = set()
    for mod_name in _PACK_MODULES:
        mod = importlib.import_module(mod_name)
        for task in getattr(mod, "TASKS", []):
            validate_task(task, known, seen)
            seen.add(task.task_id)
            tasks.append(task)
        rubrics.update(getattr(mod, "RUBRICS", {}))
        crossfile.update(getattr(mod, "CROSSFILE_TARGETS", {}))
        duplication.update(getattr(mod, "DUPLICATION_TARGETS", {}))
        setups.update(getattr(mod, "SETUPS", {}))
        resolvers.update(getattr(mod, "RUNTIME_TARGET_RESOLVERS", {}))
    for task in tasks:
        if task.setup is not None and task.setup not in setups:
            raise TaskValidationError(
                f"{task.task_id}: setup {task.setup!r} not in any pack's SETUPS"
            )
    return TaskPack(
        tasks=tuple(tasks),
        rubrics=rubrics,
        crossfile_targets=crossfile,
        duplication_targets=duplication,
        setups=setups,
        runtime_target_resolvers=resolvers,
    )


def collect_tasks() -> list[EffTask]:
    return list(load_packs().tasks)
