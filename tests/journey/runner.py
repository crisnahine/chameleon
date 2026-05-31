"""Journey harness runner.

Usage:
  mcp/.venv/bin/python -m tests.journey.runner               # full run
  mcp/.venv/bin/python -m tests.journey.runner --list        # list acts
  mcp/.venv/bin/python -m tests.journey.runner --dry-run     # preflight only
  mcp/.venv/bin/python -m tests.journey.runner --max-budget-usd 30
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.journey.harness import preflight  # noqa: E402
from tests.journey.harness.context import JourneyContext, build_context  # noqa: E402
from tests.journey.harness.fixtures import setup_fixture  # noqa: E402

_ACTS = [
    ("00_preflight", "Pre-flight wipe + isolation setup", 0.30, [0]),
    (
        "01_install_mcp_doctor",
        "Install + MCP boot + Doctor + using-chameleon verify",
        1.20,
        [1, 2, 3, 4],
    ),
    ("02_init_flow", "Init flow (TS, both auto_rename modes + force=True)", 3.00, [5, 6, 7, 15]),
    ("03_hot_path_drift", "Hot path advisory (Edit + Write)", 2.00, [8, 9]),
    ("03b_drift_refresh", "Drift injection + refresh recovery", 2.00, [10, 11]),
    ("04_v060_ux_bundle", "auto_refresh subprocess discipline", 2.00, [12]),
    ("04b_canonical_trust", "canonical_ref lifecycle + trust.auto_preserve_when", 2.50, [13, 14]),
    ("05_teach_status_doctor", "Teach idiom (structured + cap tests)", 2.00, [16]),
    ("05b_status", "Status v0.6.0 config surface", 1.50, [17]),
    ("05c_doctor", "Doctor stale errors filter", 1.50, [18]),
    ("06_suppression_callout", "Pause + disable + 4-level precedence", 1.50, [19]),
    ("06b_callout_hmac", "Callout-detector + HMAC tampering", 2.00, [20, 23]),
    ("07_rails_parity", "Rails parity", 3.00, [21]),
    ("08_hooks_security_sanitization", "Hooks + security + sanitization", 2.00, [22, 24, 25, 26]),
    (
        "09_schema_atomicity_concurrency",
        "Schema + atomicity + concurrency + monorepo",
        2.50,
        [27, 28, 29, 30, 31, 32],
    ),
    (
        "10_daemon_observability_resilience",
        "Daemon + observability + resilience",
        2.00,
        [33, 34, 35],
    ),
    ("10b_log_rotation", "Log rotation + auto_refresh.log truncate", 1.50, [36]),
    ("12_pr_review", "PR review (convention + logic findings, anti-hallucination)", 1.50, [38, 39]),
    ("11_uninstall_cleanup", "Uninstall + cleanup", 0.50, [37]),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tests.journey.runner")
    p.add_argument("--list", action="store_true", help="List acts + phases and exit")
    p.add_argument("--dry-run", action="store_true", help="Run preflight only, no Claude spawn")
    p.add_argument(
        "--max-budget-usd",
        type=float,
        default=35.0,
        help="Abort if projected cost exceeds (default 35)",
    )
    p.add_argument(
        "--results-dir",
        default=str(_REPO_ROOT / "tests" / "journey" / "results"),
        help="Where to write per-run output",
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

    total_estimated = sum(a[2] for a in _ACTS)
    if total_estimated > args.max_budget_usd:
        print(
            f"ERROR: estimated total cost ${total_estimated:.2f} > --max-budget-usd ${args.max_budget_usd:.2f}",
            file=sys.stderr,
        )
        return 1

    results_root = Path(args.results_dir).resolve()
    results_root.mkdir(parents=True, exist_ok=True)
    ctx = build_context(plugin_root=_REPO_ROOT, results_root=results_root)
    print(f"run_dir: {ctx.run_dir}", file=sys.stderr)

    try:
        pf = preflight.run_all(plugin_root=_REPO_ROOT, run_dir=ctx.run_dir)
    except preflight.PreflightError as e:
        print(f"PREFLIGHT FAILED: {e}", file=sys.stderr)
        return 2

    print(f"preflight ok: claude={pf['claude']}, git={pf['git_version']}", file=sys.stderr)

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

    return _run_acts(ctx, args)


def _run_acts(ctx: JourneyContext, args: argparse.Namespace) -> int:
    """Sequentially run each act, applying mid-run abort budget check."""
    all_results: list[dict] = []
    any_failed = False

    for idx, (act_id, name, ceiling, phases) in enumerate(_ACTS):
        remaining_ceilings = [a[2] for a in _ACTS[idx:]]
        projected = ctx.cost_so_far_usd + sum(remaining_ceilings)
        if projected > args.max_budget_usd:
            print(
                f"BUDGET ABORT before {act_id}: projected ${projected:.2f} > ${args.max_budget_usd:.2f}",
                file=sys.stderr,
            )
            for skipped_idx in range(idx, len(_ACTS)):
                skip_act_id = _ACTS[skipped_idx][0]
                skip_phases = _ACTS[skipped_idx][3]
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
        lines.append(f"| {r['act']} | {r['phase']} | {r['status']} | {r['notes'][:80]} |")

    (ctx.run_dir / "run.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"results: {json_path}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
