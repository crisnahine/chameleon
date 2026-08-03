"""Journey harness runner.

Usage (from the repo root, with PYTHONPATH=.):
  plugin/mcp/.venv/bin/python -m tests.journey.runner               # full run
  plugin/mcp/.venv/bin/python -m tests.journey.runner --list        # list acts
  plugin/mcp/.venv/bin/python -m tests.journey.runner --dry-run     # preflight only
  plugin/mcp/.venv/bin/python -m tests.journey.runner --max-budget-usd 80
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLUGIN_DIR = _REPO_ROOT / "plugin"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.journey.harness import preflight  # noqa: E402
from tests.journey.harness.claude import abnormal_termination  # noqa: E402
from tests.journey.harness.context import JourneyContext, build_context  # noqa: E402
from tests.journey.harness.fixtures import setup_fixture  # noqa: E402

# Per-act cost ceilings, in USD. These are UPPER BOUNDS, not estimates: the
# budget guard sums the ceilings of the acts still to run and aborts when
# spend-so-far plus that sum passes --max-budget-usd. A ceiling set at an act's
# typical cost therefore aborts a healthy run roughly half the time, after the
# money is already spent and with the gate left incomplete.
#
# Each is derived from the highest cost that act has actually recorded across
# the runs under tests/journey/results/, plus ~15%, rounded up to the nearest
# 0.50. Recompute from the per-act `total_cost_usd` in each run's
# transcripts/act_*.txt after a batch of acts changes shape; the numbers drifted
# 4x on 08 before anyone re-derived them, which is what made the guard fire on
# the wrong act. Ceilings only ever go up here: lowering one on a handful of
# samples buys a tighter guard at the price of spurious aborts.
_ACTS = [
    ("00_preflight", "Pre-flight wipe + isolation setup", 0.30, [0]),
    (
        "01_install_mcp_doctor",
        "Install + MCP boot + Doctor + using-chameleon verify",
        2.00,
        [1, 2, 3, 4],
    ),
    ("02_init_flow", "Init flow (TS, both auto_rename modes + force=True)", 3.00, [5, 6, 7, 15]),
    ("03_hot_path_drift", "Hot path advisory (Edit + Write)", 2.00, [8, 9]),
    ("03b_drift_refresh", "Drift injection + refresh recovery", 6.50, [10, 11]),
    ("04_v060_ux_bundle", "auto_refresh subprocess discipline", 3.00, [12]),
    ("04b_canonical_trust", "canonical_ref lifecycle + trust.auto_preserve_when", 5.00, [13, 14]),
    ("05_teach_status_doctor", "Teach idiom (structured + slug boundary)", 2.00, [16]),
    ("05b_status", "Status v0.6.0 config surface", 1.50, [17]),
    ("05c_doctor", "Doctor stale errors filter", 1.50, [18]),
    ("06_suppression_callout", "Pause + disable + 4-level precedence", 3.00, [19]),
    ("06b_callout_hmac", "Callout-detector + HMAC tampering", 2.50, [20, 23]),
    ("07_rails_parity", "Rails parity", 4.00, [21]),
    ("08_hooks_security_sanitization", "Hooks + security + sanitization", 10.00, [22, 24, 25, 26]),
    (
        "09_schema_atomicity_concurrency",
        "Schema + atomicity + concurrency + monorepo",
        5.00,
        [27, 28, 29, 30, 31, 32],
    ),
    (
        "10_daemon_observability_resilience",
        "Daemon + observability + resilience",
        2.50,
        [33, 34, 35],
    ),
    ("10b_log_rotation", "Log rotation + auto_refresh.log truncate", 3.50, [36]),
    (
        "12_pr_review",
        "PR review (convention + logic findings, anti-hallucination)",
        10.00,
        [38, 39],
    ),
    (
        "12b_pr_review_deep",
        "PR review deep paths (secret / migration / dependency-ACK / eval sink)",
        9.50,
        [40, 41],
    ),
    (
        "12c_receiving_review",
        "Receiving code review (ground-before-draft, never-ledger, pre-existing gate)",
        2.50,
        [42, 43],
    ),
    ("11_uninstall_cleanup", "Uninstall + cleanup", 1.00, [37]),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tests.journey.runner")
    p.add_argument("--list", action="store_true", help="List acts + phases and exit")
    p.add_argument("--dry-run", action="store_true", help="Run preflight only, no Claude spawn")
    p.add_argument(
        "--max-budget-usd",
        type=float,
        default=100.0,
        help="Abort if projected cost exceeds (default 100)",
    )
    p.add_argument(
        "--model",
        default="",
        help=(
            "Model every act's worker spawns with (default: the 'sonnet' alias). "
            "Pass an exact id (e.g. claude-sonnet-5) to pin a release gate, since "
            "the alias floats to whatever the latest Sonnet is and results are "
            "compared across runs. Cost estimates assume Sonnet pricing."
        ),
    )
    p.add_argument(
        "--results-dir",
        default=str(_REPO_ROOT / "tests" / "journey" / "results"),
        help="Where to write per-run output",
    )
    p.add_argument(
        "--acts",
        default="",
        help=(
            "Comma-separated act ids to run (default: all). Dependencies are NOT "
            "auto-added: acts that reuse a bootstrapped+trusted fixture need their "
            "provider in the list too (ts_basic: 02_init_flow; rails_basic: "
            "07_rails_parity). See each act's docstring."
        ),
    )
    return p


def cmd_list() -> int:
    print(f"{len(_ACTS)} acts:", file=sys.stderr)
    for act_id, name, ceiling, phases in _ACTS:
        print(f"  {act_id:40s}  ${ceiling:>5.2f}  phases={phases}  {name}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        return cmd_list()

    if args.acts:
        wanted = [a.strip() for a in args.acts.split(",") if a.strip()]
        known = {a[0] for a in _ACTS}
        unknown = [w for w in wanted if w not in known]
        if unknown:
            print(f"ERROR: unknown act id(s) {unknown}; see --list", file=sys.stderr)
            return 1
        # Keep registry order regardless of the order given on the CLI.
        selected = [a for a in _ACTS if a[0] in set(wanted)]
        print(f"act filter: running {[a[0] for a in selected]}", file=sys.stderr)
    else:
        selected = _ACTS

    total_estimated = sum(a[2] for a in selected)
    if total_estimated > args.max_budget_usd:
        print(
            f"ERROR: estimated total cost ${total_estimated:.2f} > --max-budget-usd ${args.max_budget_usd:.2f}",
            file=sys.stderr,
        )
        return 1

    # Set before any act imports: acts run in-process via importlib and each
    # calls spawn_claude without a model, so this env var is what reaches them.
    if args.model:
        os.environ["CHAMELEON_JOURNEY_MODEL"] = args.model
        print(f"worker model pinned: {args.model}", file=sys.stderr)

    results_root = Path(args.results_dir).resolve()
    results_root.mkdir(parents=True, exist_ok=True)

    # Acquire the concurrent-runner lock BEFORE creating this invocation's own
    # uniquely-timestamped run_dir: build_context() creates run_dir exclusively
    # (mkdir exist_ok=False), so a lock taken only afterward could never
    # actually detect a second, overlapping invocation -- each one would just
    # create its own distinct run_dir and never contend. Preflight's other
    # checks are cheap and order-independent, so folding them into this same
    # try/except (rather than running them only after the lock) costs nothing.
    try:
        pf = preflight.run_all(
            repo_root=_REPO_ROOT, results_root=results_root, plugin_dir=_PLUGIN_DIR
        )
    except preflight.PreflightError as e:
        print(f"PREFLIGHT FAILED: {e}", file=sys.stderr)
        return 2

    print(f"preflight ok: claude={pf['claude']}, git={pf['git_version']}", file=sys.stderr)

    ctx = build_context(plugin_root=_PLUGIN_DIR, results_root=results_root, repo_root=_REPO_ROOT)
    print(f"run_dir: {ctx.run_dir}", file=sys.stderr)

    for name, seed_path in pf["fixtures"].items():
        try:
            work_dir, origin_dir = setup_fixture(name, seed_path, ctx.run_dir / "working")
        except Exception as e:
            print(f"fixture {name} setup failed: {e}", file=sys.stderr)
            return 3
        ctx.fixtures[name] = work_dir
        ctx.origins[name] = origin_dir
    print(f"fixtures ready: {list(ctx.fixtures)}", file=sys.stderr)

    if args.dry_run:
        print("DRY RUN complete (no acts executed)", file=sys.stderr)
        return 0

    return _run_acts(ctx, args, selected)


def _run_acts(ctx: JourneyContext, args: argparse.Namespace, acts: list | None = None) -> int:
    """Sequentially run each act, applying mid-run abort budget check."""
    all_results: list[dict] = []
    any_failed = False
    acts = _ACTS if acts is None else acts

    for idx, (act_id, name, ceiling, phases) in enumerate(acts):
        remaining_ceilings = [a[2] for a in acts[idx:]]
        projected = ctx.cost_so_far_usd + sum(remaining_ceilings)
        if projected > args.max_budget_usd:
            print(
                f"BUDGET ABORT before {act_id}: projected ${projected:.2f} > ${args.max_budget_usd:.2f}",
                file=sys.stderr,
            )
            for skipped_idx in range(idx, len(acts)):
                skip_act_id = acts[skipped_idx][0]
                skip_phases = acts[skipped_idx][3]
                for ph in skip_phases:
                    all_results.append(
                        {
                            "act": skip_act_id,
                            "phase": ph,
                            "status": "SKIP",
                            "notes": "budget exhausted",
                        }
                    )
            break

        ctx.current_checkpoint_file = ctx.run_dir / "checkpoints" / f"{act_id}.jsonl"
        ctx.current_checkpoint_file.touch()
        ctx.env["CHAMELEON_JOURNEY_CHECKPOINT"] = str(ctx.current_checkpoint_file)

        print(f"[ACT {act_id}] {name} - starting (estimate ${ceiling:.2f})", file=sys.stderr)

        mod = importlib.import_module(f"tests.journey.acts.act_{act_id}")
        t0 = time.monotonic()
        try:
            act_result = mod.run(ctx)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            print(f"[ACT {act_id}] ERROR ({elapsed:.1f}s): {exc}", file=sys.stderr)
            for ph in phases:
                all_results.append(
                    {"act": act_id, "phase": ph, "status": "ERROR", "notes": str(exc)}
                )
            any_failed = True
            continue

        elapsed = time.monotonic() - t0
        ctx.cost_so_far_usd += act_result.cost_usd

        print(
            f"[ACT {act_id}] done in {elapsed:.1f}s, cost ${act_result.cost_usd:.2f} (cumulative ${ctx.cost_so_far_usd:.2f})",
            file=sys.stderr,
        )

        # A worker that stopped mid-task leaves checkpoints for the phases it
        # reached and nothing for the rest, and an act reading those cannot
        # tell "the assertion held" from "the session died before the step
        # ran" -- so a phase can be reported PASS, or promoted to PASS by a
        # transcript cross-check, off a session that never did the work. A
        # phase with no events lands as SKIP, which alone never fails a run,
        # so without this the gate goes green on a dead worker. Every phase of
        # such an act is recorded ERROR with its original outcome kept in the
        # notes: partial detail survives, but the run cannot go green on a
        # worker that died.
        #
        # An act whose every declared phase PASSED is the exception: it holds
        # the whole result it came for, so exhausting the turn cap on the way
        # out is a budget fact rather than a lost one, and voiding those
        # outcomes would discard work the worker demonstrably did. A deferred
        # tool stays fatal regardless -- see abnormal_termination.
        phases_complete = bool(act_result.phase_outcomes) and all(
            outcome.status == "PASS" for outcome in act_result.phase_outcomes
        )
        abnormal = abnormal_termination(act_result, work_complete=phases_complete)
        if abnormal:
            print(f"[ACT {act_id}] SESSION TERMINATED ABNORMALLY: {abnormal}", file=sys.stderr)
            for phase_outcome in act_result.phase_outcomes:
                all_results.append(
                    {
                        "act": act_id,
                        "phase": phase_outcome.phase,
                        "status": "ERROR",
                        "notes": (
                            f"session terminated abnormally: {abnormal}; "
                            f"act reported {phase_outcome.status}: {phase_outcome.notes}"
                        ),
                    }
                )
            any_failed = True
            continue

        for phase_outcome in act_result.phase_outcomes:
            all_results.append(
                {
                    "act": act_id,
                    "phase": phase_outcome.phase,
                    "status": phase_outcome.status,
                    "notes": phase_outcome.notes,
                }
            )
            if phase_outcome.status in ("FAIL", "ERROR"):
                any_failed = True

    _write_outputs(ctx, all_results)
    return 1 if any_failed else 0


def _write_outputs(ctx: JourneyContext, results: list[dict]) -> None:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = ctx.run_dir / "run.json"
    json_path.write_text(
        json.dumps(
            {
                "timestamp": ts,
                "cost_so_far_usd": ctx.cost_so_far_usd,
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    lines = [
        "# Journey run",
        "",
        f"Run at {ts}",
        "",
        f"Total cost: ${ctx.cost_so_far_usd:.2f}",
        "",
        "| act | phase | status | notes |",
        "|-----|-------|--------|-------|",
    ]
    for r in results:
        # Wide enough that an abnormal-termination note, whose reason prefix
        # alone runs past 60 characters, still leaves the act's own verdict
        # visible after it -- that pairing is the whole point of keeping the
        # original outcome in the note.
        lines.append(f"| {r['act']} | {r['phase']} | {r['status']} | {r['notes'][:200]} |")

    (ctx.run_dir / "run.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"results: {json_path}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
