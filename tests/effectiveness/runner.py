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

import time  # noqa: E402

from tests.effectiveness import report  # noqa: E402
from tests.effectiveness.arms import ArmError, parse_arms  # noqa: E402
from tests.effectiveness.bootstrap import (  # noqa: E402
    bootstrap_fixture,
    ensure_chameleon_env,
    env_repo_root,
    grant_worktree_trust,
)
from tests.effectiveness.scorers import PANEL_SCORER  # noqa: E402
from tests.effectiveness.scorers.base import ScoreContext, run_scorer  # noqa: E402
from tests.effectiveness.scorers.judge_panel import (  # noqa: E402
    deterministic_disagreement,
    run_panel,
)
from tests.effectiveness.tasks import collect_tasks, load_packs  # noqa: E402
from tests.effectiveness.worktrees import changed_files, prepare_cell, session_diff  # noqa: E402
from tests.journey.harness import preflight as journey_preflight  # noqa: E402
from tests.journey.harness.claude import spawn_claude  # noqa: E402
from tests.journey.harness.context import build_context  # noqa: E402
from tests.journey.harness.fixtures import setup_fixture  # noqa: E402

# Per-session cost ceiling used for budget projection (tier-ci tasks cap at
# 12 turns on sonnet; observed journey acts of similar size run $0.15-0.30).
EST_CELL_USD = 0.30
EST_VOTE_USD = 0.05

FIXTURE_SEEDS = {"ts": "eff_ts", "rails": "eff_rails"}
SESSION_TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob"]

# Seams (tests monkeypatch these module attributes).
_prepare_cell = prepare_cell
_grant_trust = grant_worktree_trust
_changed_files = changed_files
_session_diff = session_diff
_spawn = spawn_claude


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


def _preflight(args, tasks) -> None:
    """Abort before any spawn if the environment cannot run the selection."""
    journey_preflight.claude_on_path()
    from tests.journey.harness.fixtures import check_git_version

    check_git_version((2, 28))
    journey_preflight.python_venv_present(_REPO_ROOT)
    committed = sorted({FIXTURE_SEEDS[t.fixture] for t in tasks if t.fixture in FIXTURE_SEEDS})
    if committed:
        journey_preflight.fixtures_present(
            _REPO_ROOT,
            fixtures_root=_REPO_ROOT / "tests" / "effectiveness" / "fixtures",
            required=committed,
        )


def _prepare_fixtures(ctx, tasks) -> dict[str, Path]:
    """fixture name -> bootstrapped repo root. Env repos resolve or record a reason."""
    roots: dict[str, Path] = {}
    for fixture in sorted({t.fixture for t in tasks}):
        if fixture in FIXTURE_SEEDS:
            seed = _REPO_ROOT / "tests" / "effectiveness" / "fixtures" / FIXTURE_SEEDS[fixture]
            work_dir, _origin = setup_fixture(FIXTURE_SEEDS[fixture], seed, ctx.run_dir / "working")
            bootstrap_fixture(work_dir)
            roots[fixture] = work_dir
        else:
            root, reason = env_repo_root(fixture)
            if root is None:
                print(f"SKIP fixture {fixture}: {reason}", file=sys.stderr)
            else:
                roots[fixture] = root
    return roots


def _execute(args, tasks, arms, cells) -> int:
    results_root = Path(args.results_dir).resolve()
    results_root.mkdir(parents=True, exist_ok=True)
    ctx = build_context(
        plugin_root=_REPO_ROOT, results_root=results_root, run_prefix="effectiveness"
    )
    print(f"run_dir: {ctx.run_dir}", file=sys.stderr)
    ensure_chameleon_env(ctx.env)

    try:
        _preflight(args, tasks)
    except journey_preflight.PreflightError as exc:
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 1

    pack = load_packs()
    try:
        fixture_roots = _prepare_fixtures(ctx, tasks)
    except Exception as exc:  # noqa: BLE001 - harness-level failure
        print(f"FIXTURE PREP FAILED: {exc}", file=sys.stderr)
        return 1

    (ctx.run_dir / "worktrees").mkdir(exist_ok=True)
    (ctx.run_dir / "diffs").mkdir(exist_ok=True)

    cell_rows: list[dict] = []
    diffs_by_task: dict[str, dict[str, str]] = {}
    cells_by_task: dict[str, list[dict]] = {}
    cost_so_far = 0.0

    for idx, (task, arm, rep) in enumerate(cells):
        remaining = len(cells) - idx
        if cost_so_far + remaining * EST_CELL_USD > args.max_budget_usd:
            for t2, a2, r2 in cells[idx:]:
                cell_rows.append(_cell_row(t2, a2, r2, "skipped", "budget exhausted"))
            print(f"BUDGET ABORT before cell {idx + 1}/{len(cells)}", file=sys.stderr)
            break

        if task.fixture not in fixture_roots:
            cell_rows.append(
                _cell_row(task, arm, rep, "skipped", f"fixture {task.fixture} unavailable")
            )
            continue

        row = _run_one_cell(args, ctx, pack, fixture_roots[task.fixture], task, arm, rep)
        cell_rows.append(row)
        cost_so_far += (row.get("session") or {}).get("cost_usd") or 0.0
        if row["status"] == "ok":
            cells_by_task.setdefault(task.task_id, []).append(row)
            diffs_by_task.setdefault(task.task_id, {})[arm.name] = row.pop("_diff", "")
        print(
            f"[{idx + 1}/{len(cells)}] {task.task_id} | {arm.name} | r{rep} -> "
            f"{row['status']} (cumulative ${cost_so_far:.2f})",
            file=sys.stderr,
        )

    panel_rows = _panel_phase(args, ctx, tasks, arms, cells_by_task, diffs_by_task)
    for p in panel_rows:
        cost_so_far += p.get("panel_cost_usd") or 0.0

    for c in cell_rows:
        c.pop("_diff", None)

    aggregates = report.aggregate(cell_rows)
    baselines = report.load_baselines(_REPO_ROOT / "tests" / "effectiveness" / "baselines.json")
    deltas = report.compare_to_baseline(aggregates, baselines, tier=args.tier)
    errors = sum(1 for c in cell_rows if c["status"] == "error")
    run_doc = {
        "run_id": ctx.run_dir.name,
        "tier": args.tier,
        "arms": [a.name for a in arms],
        "model": args.model,
        "toggle": args.toggle,
        "cells": cell_rows,
        "panel": panel_rows,
        "aggregates": aggregates,
        "baseline_deltas": deltas,
        "errors": errors,
        "total_cost_usd": round(cost_so_far, 4),
    }
    report.write_outputs(ctx.run_dir, run_doc)
    print(f"results: {ctx.run_dir / 'run.json'}", file=sys.stderr)

    ran_ok = any(c["status"] == "ok" for c in cell_rows)
    return 0 if ran_ok else 1


def _cell_row(task, arm, rep, status, reason=None, session=None, scores=None) -> dict:
    return {
        "task_id": task.task_id,
        "category": task.category,
        "fixture": task.fixture,
        "arm": arm.name,
        "repeat": rep,
        "status": status,
        "reason": reason,
        "session": session or {},
        "scores": scores or {},
    }


def _resolve_prompt(task, pack, repo_root) -> tuple[str | None, str | None]:
    """(prompt, skip_reason). Tier-full crossfile tasks resolve targets at run time."""
    resolver = pack.runtime_target_resolvers.get(task.task_id)
    if resolver is None:
        return task.prompt, None
    target = resolver(repo_root)
    if target is None:
        return None, "no crossfile target with 3+ recorded callers in env repo"
    pack.crossfile_targets[task.task_id] = target
    return task.prompt.format(**target), None


def _run_one_cell(args, ctx, pack, fixture_repo, task, arm, rep) -> dict:
    from tests.effectiveness.arms import arm_env

    cell_id = f"{task.task_id}__{arm.name}__r{rep}".replace("/", "_")
    dest = ctx.run_dir / "worktrees" / cell_id
    try:
        prompt, skip_reason = _resolve_prompt(task, pack, fixture_repo)
        if skip_reason:
            return _cell_row(task, arm, rep, "skipped", skip_reason)
        setup_fn = pack.setups.get(task.setup) if task.setup else None
        baseline_sha = _prepare_cell(
            fixture_repo=fixture_repo,
            dest=dest,
            arm=arm,
            setup_fn=setup_fn,
            trust_fn=_grant_trust,
        )
        repo_id = _grant_trust(dest)
        transcript = ctx.run_dir / "transcripts" / f"{cell_id}.txt"
        t0 = time.monotonic()
        session = _spawn(
            prompt=prompt,
            cwd=dest,
            env=arm_env(arm, ctx.env),
            transcript_path=transcript,
            max_turns=task.max_turns,
            allowed_tools=SESSION_TOOLS,
            timeout_s=600,
            model=args.model,
            plugin_root=ctx.plugin_root,
        )
        wall = round(time.monotonic() - t0, 2)
        session_meta = {
            "session_id": session.session_id,
            "cost_usd": session.cost_usd,
            "wall_seconds": wall,
            "returncode": session.returncode,
            "transcript": str(transcript),
            "baseline_sha": baseline_sha,
            "model": args.model,
        }
        if session.returncode != 0:
            return _cell_row(
                task,
                arm,
                rep,
                "error",
                f"session returncode {session.returncode}",
                session=session_meta,
            )
        changed = _changed_files(dest, baseline_sha)
        diff = _session_diff(dest, baseline_sha)
        (ctx.run_dir / "diffs" / f"{cell_id}.patch").write_text(diff, encoding="utf-8")
        score_ctx = ScoreContext(
            task=task,
            arm=arm.name,
            repeat=rep,
            worktree=dest,
            baseline_sha=baseline_sha,
            changed_files=changed,
            repo_id=repo_id,
            session_id=session.session_id,
            transcript_path=transcript,
            hook_events=session.hook_events,
            bash_commands=session.bash_commands,
            cost_usd=session.cost_usd,
            wall_seconds=wall,
            pack=pack,
            run_dir=ctx.run_dir,
        )
        scores = {
            name: run_scorer(name, score_ctx) for name in task.scorers if name != PANEL_SCORER
        }
        row = _cell_row(task, arm, rep, "ok", session=session_meta, scores=scores)
        row["_diff"] = diff  # consumed by the panel phase, stripped before output
        return row
    except Exception as exc:  # noqa: BLE001 - one bad cell never kills the run
        return _cell_row(task, arm, rep, "error", f"{type(exc).__name__}: {exc}")


def _panel_phase(args, ctx, tasks, arms, cells_by_task, diffs_by_task) -> list[dict]:
    """Pairwise panel per task: baseline arm (first listed) vs each other arm.

    Runs when --panel was passed, or unsolicited when the deterministic
    scorers disagree about the pair's winner.
    """
    rows: list[dict] = []
    if len(arms) < 2:
        return rows
    baseline_arm = arms[0].name
    for task in tasks:
        task_cells = cells_by_task.get(task.task_id) or []
        diffs = diffs_by_task.get(task.task_id) or {}
        for other in arms[1:]:
            pair = (baseline_arm, other.name)
            if baseline_arm not in diffs or other.name not in diffs:
                continue
            wanted = args.panel or deterministic_disagreement(task_cells, pair)
            if not wanted:
                continue
            result = run_panel(
                task_id=task.task_id,
                pair=pair,
                diffs=diffs,
                run_dir=ctx.run_dir,
            )
            rows.append({"task_id": task.task_id, "pair": list(pair), **result})
    return rows


if __name__ == "__main__":
    sys.exit(main())
