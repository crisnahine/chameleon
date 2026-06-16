"""Post-hoc Claude-judge panel over a completed run's saved diffs.

Drives judge_panel.run_panel on each task's off-vs-shadow patch pair (no cell
re-runs), then reports the paired cluster-bootstrap CI on the judge's preference
for the chameleon (shadow) arm. A causal preference requires the CI lower bound
> 0.5. Controlled Claude judge (both orderings per vote, 3 votes); the validity
rests on order-control + the deterministic scorer reported alongside, not family.

Usage: PYTHONPATH=. mcp/.venv/bin/python tests/run_posthoc_panel.py <results_dir>
"""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path

from tests.effectiveness.report import paired_preference_cis
from tests.effectiveness.scorers.judge_panel import run_panel

_PAT = re.compile(r"^(?P<task>.+)__(?P<arm>[^_]+(?:~[^_]+)?)__r\d+\.patch$")


def main() -> int:
    rd = Path(sys.argv[1])
    diffs_dir = rd / "diffs"
    by_task: dict[str, dict[str, str]] = {}
    for p in sorted(diffs_dir.glob("*.patch")):
        m = _PAT.match(p.name)
        if not m:
            continue
        by_task.setdefault(m.group("task"), {})[m.group("arm")] = p.read_text(
            encoding="utf-8", errors="replace"
        )

    pairs = [(t, d) for t, d in by_task.items() if "off" in d and "shadow" in d]
    print(f"task-pairs with off+shadow diffs: {len(pairs)}")
    rng = random.Random(20260616)
    panel_rows: list[dict] = []
    for i, (task, d) in enumerate(pairs):
        res = run_panel(
            task_id=task,
            pair=("off", "shadow"),
            diffs={"off": d["off"], "shadow": d["shadow"]},
            run_dir=rd,
            rng=rng,
        )
        if "unscored" in res:
            print(f"[{i + 1}/{len(pairs)}] {task}: unscored ({res['unscored']})")
            continue
        panel_rows.append({"task_id": task, "pair": ("off", "shadow"), **res})
        print(
            f"[{i + 1}/{len(pairs)}] {task}: winner={res['panel_winner']} "
            f"votes={res.get('panel_votes_for_shadow', 0)}-{res.get('panel_votes_for_off', 0)}"
        )

    cis = paired_preference_cis(panel_rows)
    print("\n=== PANEL PAIRED CI (off vs shadow, n=task-pairs) ===")
    for c in cis:
        lo = c["lo"]
        verdict = "CAUSAL WIN (lo>0.5)" if (lo is not None and lo > 0.5) else "NOT established"
        print(
            f"  {c['control']} vs {c['treatment']}: preference={c['rate']:.3f} "
            f"95% CI=[{c['lo']:.3f}, {c['hi']:.3f}] n_tasks={c['n_tasks']} -> {verdict}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
