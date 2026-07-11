"""Merge split-arm study_multiconv_ab.py outputs and compute the coded bar.

The campaign runs each arm as its own process (parallel wall-clock); this
reporter concatenates their row outputs, prints per-arm/per-language mean
conformance, and computes the repo's coded bar
(tests.effectiveness.stats.paired_bootstrap_ci) for chameleon vs every
baseline arm present — overall and per language.

Usage:
    PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_multiconv_report.py \\
        run_off.json run_stale.json run_full.json run_cham.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

from tests.effectiveness.stats import paired_bootstrap_ci


def _load_rows(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        data = json.load(open(p))
        rows.extend(data.get("rows") or [])
    return rows


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    rows = _load_rows(sys.argv[1:])
    arms = sorted({r["arm"] for r in rows})
    langs = sorted({r["lang"] for r in rows})

    print("=== per-arm mean conformance (0..1) ===")
    for arm in arms:
        ar = [r for r in rows if r["arm"] == arm]
        parts = "  ".join(
            f"{lg}={_mean([r['score'] for r in ar if r['lang'] == lg]):.2f}"
            for lg in langs
            if [r for r in ar if r["lang"] == lg]
        )
        print(f"  {arm:13} overall={_mean([r['score'] for r in ar]):.2f}  n={len(ar)}  {parts}")

    by_key: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        by_key[(r["lang"], r["task"])][r["arm"]] = r

    actives = [a for a in arms if a.startswith("chameleon")]
    if not actives:
        print("no chameleon arm; bar not computed")
        return 0

    print("\n=== coded bar: paired_bootstrap_ci (lo must clear 0.5) ===")
    out: dict = {}
    for active in actives:
        for base in [a for a in arms if not a.startswith("chameleon")]:
            for scope in ["all", *langs]:
                wins: dict[str, list[float]] = {}
                for (lang, task), per_arm in sorted(by_key.items()):
                    if scope != "all" and lang != scope:
                        continue
                    a, b = per_arm.get(active), per_arm.get(base)
                    if a is None or b is None:
                        continue
                    w = (
                        1.0
                        if a["score"] > b["score"]
                        else (0.5 if a["score"] == b["score"] else 0.0)
                    )
                    wins[f"{lang}-{task}"] = [w]
                ci = paired_bootstrap_ci(wins)
                if ci["rate"] is None:
                    continue
                met = ci["lo"] > 0.5
                tag = f"{active}_vs_{base}" + ("" if scope == "all" else f"_{scope}")
                out[tag] = ci
                print(
                    f"  {active} vs {base:13} [{scope:3}]: rate={ci['rate']:.3f} "
                    f"CI[{ci['lo']:.3f}, {ci['hi']:.3f}] n={ci['n_tasks']} "
                    f"-> {'BAR MET' if met else 'not met'}"
                )
    print()
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
