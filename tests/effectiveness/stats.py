"""Statistics primitives for legitimate effectiveness claims.

Pure, dependency-free (stdlib only) so the eval can report confidence-bounded
numbers instead of bare percentages a skeptic dismisses. Built because the
zero-review expert red-team flagged that NO Wilson / bootstrap / kappa code
existed, so every CI the achievement criterion cited was unbacked.

- wilson_lower_bound: lower bound of the Wilson score interval for a binomial
  proportion (precision claims).
- cohens_kappa: inter-rater agreement for two binary label lists (judge-vs-golden
  calibration gate).
- paired_bootstrap_ci: CI on a paired preference rate, resampling at the TASK
  (cluster) level -- never the cell -- so repeats of one fixture cannot inflate n.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for ``successes/n``.

    Returns 0.0 for n == 0. z=1.96 ~ 95% one-sided-ish (two-sided 95%). Wilson
    (not normal-approx) because at small n and p near 1 the normal interval
    overshoots; Wilson stays in [0,1] and is the standard for precision claims.
    """
    if n <= 0:
        return 0.0
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


def cohens_kappa(a: list, b: list) -> float:
    """Cohen's kappa for two raters' label lists (any hashable labels).

    Raises ValueError on length mismatch or empty input. Returns 1.0 when both
    raters are constant and identical; 0.0 when agreement equals chance; can go
    negative below chance. This gates the LLM judge: a result is uncitable until
    kappa >= 0.6 against a hand-labeled golden set.
    """
    if len(a) != len(b):
        raise ValueError("label lists must be the same length")
    n = len(a)
    if n == 0:
        raise ValueError("need at least one labeled item")
    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    labels = set(a) | set(b)
    count_a = {lbl: a.count(lbl) / n for lbl in labels}
    count_b = {lbl: b.count(lbl) / n for lbl in labels}
    expected = sum(count_a[lbl] * count_b[lbl] for lbl in labels)
    if expected >= 1.0:
        # Both raters constant + identical -> perfect agreement by convention.
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def paired_bootstrap_ci(
    wins_by_task: dict[str, list[int]],
    *,
    resamples: int = 5000,
    ci: float = 0.95,
    seed: int = 12345,
) -> dict:
    """Bootstrap CI on a paired preference rate, resampling whole TASKS.

    ``wins_by_task`` maps a task id -> list of per-comparison outcomes for that
    task, each 1 (on-arm preferred) / 0 (off preferred) / 0.5 (tie). The
    resampling unit is the TASK (cluster bootstrap): repeats of one fixture are
    pseudo-replicates and must not count as independent n, so we resample task
    ids with replacement and average that task's comparisons. Returns
    ``{rate, lo, hi, n_tasks, n_comparisons}``. A legitimate causal preference
    claim requires lo > 0.5.
    """
    task_ids = list(wins_by_task)
    n_tasks = len(task_ids)
    n_comparisons = sum(len(v) for v in wins_by_task.values())
    if n_tasks == 0 or n_comparisons == 0:
        return {"rate": None, "lo": None, "hi": None, "n_tasks": 0, "n_comparisons": 0}

    def _task_mean(tid: str) -> float:
        vals = wins_by_task[tid]
        return sum(vals) / len(vals) if vals else 0.0

    point = sum(_task_mean(t) for t in task_ids) / n_tasks
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(resamples):
        sample = [task_ids[rng.randrange(n_tasks)] for _ in range(n_tasks)]
        means.append(sum(_task_mean(t) for t in sample) / n_tasks)
    means.sort()
    lo_idx = int((1 - ci) / 2 * resamples)
    hi_idx = min(resamples - 1, int((1 + ci) / 2 * resamples))
    return {
        "rate": point,
        "lo": means[lo_idx],
        "hi": means[hi_idx],
        "n_tasks": n_tasks,
        "n_comparisons": n_comparisons,
    }


def group_by_task(rows: list[dict], *, task_key: str, win_key: str) -> dict[str, list[int]]:
    """Helper: fold flat per-comparison rows into the wins_by_task shape."""
    out: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        out[r[task_key]].append(r[win_key])
    return dict(out)
