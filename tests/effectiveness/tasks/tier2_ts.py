"""Tier-2 (full) TypeScript tasks against CHAMELEON_TEST_TS_REPO.

PACK GROWTH MARKER: this pack intentionally ships 4 tasks (one per category).
It grows to ~12 at full-run time by adding tasks pointed at real conventions
of the chosen env repo — written then, against that repo, because honest
prompts cannot be authored against a repo this file has never seen. Do not
add placeholder tasks here.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.effectiveness.tasks import EffTask

TASKS = [
    EffTask(
        task_id="t2-ts-convention-feature-slice",
        tier="full",
        fixture="env-ts",
        prompt=(
            "Add a small feature the way this codebase would: a utility that "
            "formats a relative timestamp ('3 minutes ago', '2 days ago') and "
            "one usage of it in an existing component of your choice. Match "
            "the repo's file placement, naming, import style, and export "
            "conventions exactly. Run the repo's checks when done."
        ),
        category="convention",
        scorers=("convention", "duplication", "verification", "cost"),
        max_turns=16,
    ),
    EffTask(
        task_id="t2-ts-crossfile-rename",
        tier="full",
        fixture="env-ts",
        prompt=(
            "Rename the function {function} (defined in {module}) to "
            "{new_name} across the entire repo: definition, every import, and "
            "every call site. Keep behavior identical. Run the repo's checks "
            "when done."
        ),
        category="crossfile",
        scorers=("crossfile", "convention", "verification", "cost"),
        max_turns=16,
    ),
    EffTask(
        task_id="t2-ts-duplication-helper",
        tier="full",
        fixture="env-ts",
        prompt=(
            "Several places in this repo format money/amounts for display. "
            "Pick one component or view that formats an amount inline and fix "
            "it to use the repo's existing shared helper for that, if one "
            "exists; otherwise introduce one in the conventional location and "
            "use it. Keep the diff minimal. Run the repo's checks when done."
        ),
        category="duplication",
        scorers=("duplication", "convention", "verification", "cost"),
        max_turns=16,
    ),
    EffTask(
        task_id="t2-ts-verification-regression",
        tier="full",
        fixture="env-ts",
        prompt=(
            "Pick the smallest pure utility function in this repo that has an "
            "existing test, make a focused improvement to its edge-case "
            "handling (document which edge case in the code), and prove the "
            "change by running the repo's test suite for that area."
        ),
        category="verification",
        scorers=("verification", "convention", "cost"),
        max_turns=16,
    ),
]


def _resolve_ts_crossfile_target(repo_root: Path) -> dict | None:
    """Deterministic: lexicographically first (module, function) with >= 3
    recorded callers in the env repo's committed calls_index. Read directly
    (not via load_calls_index) so resolution needs no trust state."""
    artifact = Path(repo_root) / ".chameleon" / "calls_index.json"
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    callees = data.get("callees")
    if not isinstance(callees, dict):
        return None
    for module in sorted(callees):
        by_name = callees[module]
        if not isinstance(by_name, dict):
            continue
        for function in sorted(by_name):
            body = by_name[function]
            if isinstance(body, dict) and isinstance(body.get("total"), int):
                if body["total"] >= 3 and function.isidentifier():
                    return {
                        "module": module,
                        "function": function,
                        "new_name": f"{function}Renamed",
                    }
    return None


RUNTIME_TARGET_RESOLVERS = {
    "t2-ts-crossfile-rename": _resolve_ts_crossfile_target,
}
