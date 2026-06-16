"""Tier-2 (full) Rails tasks against CHAMELEON_TEST_RUBY_REPO.

PACK GROWTH MARKER: this pack intentionally ships 4 tasks (one per category).
It grows to ~12 at full-run time by adding tasks pointed at real conventions
of the chosen env repo — written then, against that repo. Do not add
placeholder tasks here.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.effectiveness.tasks import EffTask

TASKS = [
    EffTask(
        task_id="t2-rails-convention-action",
        tier="full",
        fixture="env-ruby",
        prompt=(
            "Add a small business action the way this codebase would: "
            "archiving a record of an existing model of your choice (sets an "
            "archived_at timestamp, refuses double-archive). Implement it "
            "where this repo implements business actions, matching its "
            "structure and naming exactly. Run the repo's checks when done."
        ),
        category="convention",
        scorers=("convention", "duplication", "verification", "cost"),
        max_turns=30,
    ),
    EffTask(
        task_id="t2-rails-crossfile-rename",
        tier="full",
        fixture="env-ruby",
        prompt=(
            "Rename the method {function} (defined in {module}) to {new_name} "
            "across the entire repo: definition and every call site. Keep "
            "behavior identical. Run the repo's checks when done."
        ),
        category="crossfile",
        scorers=("crossfile", "convention", "verification", "cost"),
        max_turns=30,
    ),
    EffTask(
        task_id="t2-rails-duplication-helper",
        tier="full",
        fixture="env-ruby",
        prompt=(
            "Several places in this repo normalize or sanitize user-facing "
            "strings inline. Pick one and fix it to use the repo's existing "
            "shared helper if one exists; otherwise introduce one in the "
            "conventional location and use it. Keep the diff minimal. Run the "
            "repo's checks when done."
        ),
        category="duplication",
        scorers=("duplication", "convention", "verification", "cost"),
        max_turns=30,
    ),
    EffTask(
        task_id="t2-rails-verification-regression",
        tier="full",
        fixture="env-ruby",
        prompt=(
            "Pick the smallest pure service or helper in this repo that has "
            "an existing spec, make a focused improvement to its edge-case "
            "handling (document which edge case in the code), and prove the "
            "change by running the relevant specs."
        ),
        category="verification",
        scorers=("verification", "convention", "cost"),
        max_turns=30,
    ),
]


def _resolve_ruby_crossfile_target(repo_root: Path) -> dict | None:
    """Deterministic: first (module, method) with >= 3 recorded callers."""
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
                if body["total"] >= 3 and function.replace("_", "").isalnum():
                    # Constant name from the file basename, mirroring Rails
                    # autoload naming; the qualified "Constant.method" form is
                    # what the staleness grep matches (the bare method name
                    # may appear in prose). Matches namespaced call sites too:
                    # "Orders::CreateOrder.call" contains "CreateOrder.call".
                    constant = "".join(p.capitalize() for p in Path(module).stem.split("_"))
                    return {
                        "module": module,
                        "function": function,
                        "new_name": f"{function}_renamed",
                        "old_needle": f"{constant}.{function}",
                    }
    return None


RUNTIME_TARGET_RESOLVERS = {
    "t2-rails-crossfile-rename": _resolve_ruby_crossfile_target,
}
