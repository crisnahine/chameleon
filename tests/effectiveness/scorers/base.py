"""Shared scorer contract.

Every scorer is a callable (ScoreContext) -> dict. The dict is EITHER
{"unscored": "<reason>"} OR a flat {metric: value} map whose values are
int/float/bool/str (finite floats only). run_scorer enforces this so a buggy
scorer can never fabricate a number or crash a run: any violation collapses
to an unscored-with-reason result.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.effectiveness.tasks import EffTask, TaskPack

_REASON_CAP = 500


@dataclasses.dataclass
class ScoreContext:
    """Everything a scorer may look at for one (task, arm, repeat) cell."""

    task: EffTask
    arm: str
    repeat: int
    worktree: Path  # final tree after the session
    baseline_sha: str  # HEAD commit before the session ran
    changed_files: list[str]  # repo-relative paths the session changed (sorted)
    repo_id: str
    session_id: str  # from the transcript; "" when extraction failed
    transcript_path: Path
    hook_events: list  # harness HookEvent list
    bash_commands: list[str]  # Bash tool_use commands from the transcript
    cost_usd: float
    wall_seconds: float
    pack: TaskPack
    run_dir: Path


def unscored(reason: str) -> dict:
    return {"unscored": str(reason)[:_REASON_CAP]}


def _valid_value(v: Any) -> bool:
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, str):
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    return False


def run_scorer(name: str, ctx: ScoreContext) -> dict:
    from tests.effectiveness.scorers import SCORERS

    fn: Callable | None = SCORERS.get(name)
    if fn is None:
        return unscored(f"unknown scorer {name!r}")
    try:
        out = fn(ctx)
    except Exception as exc:  # noqa: BLE001 - fail open by contract
        return unscored(f"{type(exc).__name__}: {exc}")
    if not isinstance(out, dict) or not out:
        return unscored(f"scorer {name!r} returned non-dict or empty result")
    if set(out) == {"unscored"}:
        return unscored(out["unscored"])
    if "unscored" in out:
        # EITHER/OR contract: a scorer may not hedge with metrics AND an
        # unscored marker — an ambiguous row would poison aggregation.
        return unscored(f"scorer {name!r} mixed 'unscored' with metrics")
    bad = [k for k, v in out.items() if not isinstance(k, str) or not _valid_value(v)]
    if bad:
        return unscored(f"scorer {name!r} produced non-scalar/non-finite metric(s): {bad}")
    return out
