"""Effectiveness eval runner.

Usage:
  PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner --list
  PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner --dry-run
  PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner \
      --tier ci --arms off,shadow --max-budget-usd 8

Exit codes: 0 = ran (scores may still be bad — advisory by design),
1 = harness-level failure (budget, preflight, no cells ran), 2 = usage error.
CI never invokes this module; only tests/effectiveness/tests/ runs there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.effectiveness.arms import ArmError, parse_arms  # noqa: E402
from tests.effectiveness.tasks import collect_tasks  # noqa: E402

# Per-session cost ceiling used for budget projection (tier-ci tasks cap at
# 12 turns on sonnet; observed journey acts of similar size run $0.15-0.30).
EST_CELL_USD = 0.30
EST_VOTE_USD = 0.05


def _collect_tasks():
    """Seam for tests: the CLI tests stub the registry here."""
    return collect_tasks()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tests.effectiveness.runner")
    p.add_argument("--list", action="store_true", help="List tasks and exit")
    p.add_argument("--dry-run", action="store_true", help="Print the cell plan; no spawn")
    p.add_argument("--tier", choices=["ci", "full"], default="ci")
    p.add_argument("--tasks", default="", help="Comma-separated task ids (default: whole tier)")
    p.add_argument("--arms", default="off,shadow", help="Comma-separated: off,shadow,enforce")
    p.add_argument(
        "--toggle", default=None, help="enforcement.<key> to pair-flip from the base arm"
    )
    p.add_argument("--repeats", type=int, default=None, help="Override per-task repeats")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--panel", action="store_true", help="Run the blind pairwise judge panel")
    p.add_argument("--max-budget-usd", type=float, default=8.0)
    p.add_argument(
        "--results-dir",
        default=str(_REPO_ROOT / "tests" / "effectiveness" / "results"),
    )
    return p


def _select_tasks(args) -> list:
    tasks = [t for t in _collect_tasks() if t.tier == args.tier]
    if args.tasks:
        wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
        by_id = {t.task_id: t for t in _collect_tasks()}
        missing = [w for w in wanted if w not in by_id]
        if missing:
            raise SystemExit2(f"unknown task id(s): {missing}")
        tasks = [by_id[w] for w in wanted]
    return tasks


class SystemExit2(Exception):
    """Usage error -> exit code 2."""


def _cells_for(tasks, arms, repeats_override) -> list[tuple]:
    cells = []
    for task in tasks:
        repeats = repeats_override if repeats_override is not None else task.repeats
        for arm in arms:
            for rep in range(1, repeats + 1):
                cells.append((task, arm, rep))
    return cells


def cmd_list(tasks) -> int:
    print(f"{len(tasks)} tasks:", file=sys.stderr)
    for t in tasks:
        print(
            f"  {t.task_id:36s} tier={t.tier} fixture={t.fixture:9s} "
            f"category={t.category:12s} repeats={t.repeats} scorers={','.join(t.scorers)}",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        return cmd_list([t for t in _collect_tasks() if t.tier == args.tier])

    try:
        arms = parse_arms(args.arms, args.toggle)
        tasks = _select_tasks(args)
    except (ArmError, SystemExit2) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    cells = _cells_for(tasks, arms, args.repeats)
    est = len(cells) * EST_CELL_USD
    if args.panel:
        est += len(tasks) * max(0, len(arms) - 1) * 3 * EST_VOTE_USD
    if est > args.max_budget_usd:
        print(
            f"ERROR: estimated cost ${est:.2f} exceeds --max-budget-usd ${args.max_budget_usd:.2f}",
            file=sys.stderr,
        )
        return 1

    print(
        f"plan: tasks={len(tasks)} arms={[a.name for a in arms]} cells: {len(cells)} "
        f"estimated ${est:.2f} (ceiling ${args.max_budget_usd:.2f})",
        file=sys.stderr,
    )
    if args.dry_run:
        for task, arm, rep in cells:
            print(f"  {task.task_id} | {arm.name} | repeat {rep}", file=sys.stderr)
        print("DRY RUN complete (no sessions spawned)", file=sys.stderr)
        return 0

    return _execute(args, tasks, arms, cells)


def _execute(args, tasks, arms, cells) -> int:  # pragma: no cover - wired in Task 20
    raise NotImplementedError("execution loop lands in Task 20")


if __name__ == "__main__":
    sys.exit(main())
