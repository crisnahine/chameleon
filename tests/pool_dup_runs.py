"""Pool the deterministic duplication outcome across >=1 dup-tier runs and report
the paired cluster-bootstrap CI (off vs shadow), resampled by TASK.

Each task's per-arm value = the mean "duplicated" rate over all its samples
across the given runs (so a second pass tightens the per-task estimate). The
paired statistic is per-task (off_rate - shadow_rate); a causal reduction is
established iff the bootstrap 95% CI lower bound > 0.

Usage: PYTHONPATH=. plugin/mcp/.venv/bin/python tests/pool_dup_runs.py <run_dir> [<run_dir> ...]
"""

from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path


def _duplicated(cell: dict):
    s = (cell.get("scores") or {}).get("duplication") or {}
    if not isinstance(s.get("body_hash_duplicates"), int):
        return None
    return (s["body_hash_duplicates"] > 0) or (s.get("reuse_credit") is False)


def main() -> int:
    run_dirs = [Path(a) for a in sys.argv[1:]]
    if not run_dirs:
        print("usage: pool_dup_runs.py <run_dir> [<run_dir> ...]")
        return 2
    # task -> arm -> list of duplicated bools across all runs
    samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for rd in run_dirs:
        doc = json.loads((rd / "run.json").read_text())
        for c in doc.get("cells", []):
            if c.get("status") != "ok":
                continue
            du = _duplicated(c)
            if du is None:
                continue
            samples[c["task_id"]][c["arm"]].append(1.0 if du else 0.0)

    paired = [t for t, a in samples.items() if a.get("off") and a.get("shadow")]
    print(f"runs pooled: {len(run_dirs)} | paired tasks (both arms sampled): {len(paired)}")
    if not paired:
        return 1

    off_rate = {t: sum(samples[t]["off"]) / len(samples[t]["off"]) for t in paired}
    sh_rate = {t: sum(samples[t]["shadow"]) / len(samples[t]["shadow"]) for t in paired}
    diff = {t: off_rate[t] - sh_rate[t] for t in paired}

    o = sum(off_rate.values()) / len(paired)
    s = sum(sh_rate.values()) / len(paired)
    point = sum(diff.values()) / len(paired)
    print(f"pooled off duplicated rate:    {o:.3f}")
    print(f"pooled shadow duplicated rate: {s:.3f}")

    rng = random.Random(99)
    tids = list(paired)
    means = []
    for _ in range(10000):
        samp = [tids[rng.randrange(len(tids))] for _ in tids]
        means.append(sum(diff[t] for t in samp) / len(tids))
    means.sort()
    lo, hi = means[250], means[9750]
    print(f"\nPAIRED MEAN REDUCTION (off - shadow): {point:+.3f}  95% CI=[{lo:+.3f}, {hi:+.3f}]")
    print(f"VERDICT: {'CAUSAL WIN (lo>0)' if lo > 0 else 'NOT established (CI includes 0)'}")

    # Per-task preference (shadow strictly reused-more than off this task)
    wins = sum(1 for t in paired if diff[t] > 0)
    losses = sum(1 for t in paired if diff[t] < 0)
    ties = len(paired) - wins - losses
    print(f"\nper-task: chameleon-better {wins}, off-better {losses}, tie {ties}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
