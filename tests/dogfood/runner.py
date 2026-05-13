"""Dogfood test harness runner.

Usage:
    python -m tests.dogfood.runner [options]

Options:
    --phase 1.1,2.x     Filter by phase id; "1.x" = all 1.x scenarios
    --family init,trust  Filter by family name
    --cost free,cheap    Filter by cost band (default: free,cheap)
    --include-real-claude  Allow needs_claude=true scenarios
    --include-expensive  Allow expensive cost band
    --list               List matching scenarios and exit 0
    --results-dir DIR    Where to write JSON/MD output (default: tests/dogfood/results/)
    --max-budget-usd N   Abort if estimated total cost exceeds N (default: 5.0)
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        f"dogfood runner requires Python >= 3.11 (got {sys.version_info.major}."
        f"{sys.version_info.minor}). Use mcp/.venv/bin/python instead, e.g.:\n"
        f"  mcp/.venv/bin/python -m tests.dogfood.runner\n"
    )
    sys.exit(2)

import argparse
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root is on sys.path so "tests.dogfood.*" imports work
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.dogfood.scenario import CostBand, Context, Result, Scenario, StatusName
from tests.dogfood.scenarios import all_scenarios

_COST_ORDER: list[CostBand] = ["free", "cheap", "moderate", "expensive"]

_COST_ESTIMATE: dict[CostBand, float] = {
    "free": 0.0,
    "cheap": 0.02,
    "moderate": 0.20,
    "expensive": 1.00,
}


def _load_dotenv(repo_root: Path) -> None:
    """Best-effort .env loader — no pip dependency."""
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _resolve_repo(env_var: str) -> Path | None:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


# ---------------------------------------------------------------------------
# Phase filter helpers
# ---------------------------------------------------------------------------

def _phase_matches(scenario_id: str, phase_filters: list[str]) -> bool:
    """Return True if scenario_id matches any of the phase filters.

    Filters:
      "1.1"  -> exact match on id "1.1"
      "1.x"  -> match any id whose major component is "1"
    """
    if not phase_filters:
        return True
    for f in phase_filters:
        if f.endswith(".x"):
            major = f[:-2]
            if scenario_id == major or scenario_id.startswith(major + "."):
                return True
        else:
            if scenario_id == f:
                return True
    return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tests.dogfood.runner",
        description="Run chameleon dogfood scenarios.",
    )
    p.add_argument("--phase", default="", help="Comma-separated phase filter (e.g. 1.1,2.x)")
    p.add_argument("--family", default="", help="Comma-separated family filter (e.g. init,trust)")
    p.add_argument(
        "--cost",
        default="free,cheap",
        help="Comma-separated cost bands to allow (default: free,cheap)",
    )
    p.add_argument("--include-real-claude", action="store_true", help="Allow needs_claude=true scenarios")
    p.add_argument("--include-expensive", action="store_true", help="Allow expensive cost band")
    p.add_argument("--list", dest="list_only", action="store_true", help="List matching scenarios and exit")
    p.add_argument(
        "--results-dir",
        default=str(_REPO_ROOT / "tests" / "dogfood" / "results"),
        help="Directory for JSON/MD result files",
    )
    p.add_argument("--max-budget-usd", type=float, default=5.0, help="Abort if estimated cost exceeds this (default: 5.0)")
    return p


def _filter_scenarios(
    scenarios: list[Scenario],
    phase_filters: list[str],
    family_filters: list[str],
    allowed_costs: set[str],
    include_real_claude: bool,
) -> list[Scenario]:
    out = []
    for s in scenarios:
        if phase_filters and not _phase_matches(s.id, phase_filters):
            continue
        if family_filters and s.family not in family_filters:
            continue
        if s.cost not in allowed_costs:
            continue
        if s.needs_claude and not include_real_claude:
            continue
        out.append(s)
    return out


def _run_scenario(s: Scenario, ctx: Context) -> Result:
    """Execute one scenario inside a per-scenario tmpdir."""
    if s.run is None:
        return Result(status="SKIP", notes="no run() defined")

    runnable, reason = s.is_runnable(ctx)
    if not runnable:
        return Result(status="SKIP", notes=reason)

    # Per-scenario isolated plugin_data tmpdir
    with tempfile.TemporaryDirectory(prefix=f"chameleon_dogfood_{s.id}_") as tmp:
        ctx.plugin_data_dir = Path(tmp)

        if s.setup is not None:
            try:
                s.setup(ctx)
            except Exception as exc:
                return Result(status="ERROR", notes=f"setup failed: {exc}")

        t0 = time.monotonic()
        try:
            result = s.run(ctx)
        except Exception as exc:
            result = Result(status="ERROR", notes=str(exc))
        finally:
            elapsed = time.monotonic() - t0
            if result.duration_s == 0.0:
                result.duration_s = elapsed

        if s.teardown is not None:
            try:
                s.teardown(ctx)
            except Exception as exc:
                # Teardown failures demote to ERROR only if the scenario passed
                if result.status == "PASS":
                    result.status = "ERROR"
                    result.notes = f"teardown failed: {exc}"

    return result


def _status_icon(status: StatusName) -> str:
    return {"PASS": "Y", "FAIL": "N", "SKIP": "SKIP", "ERROR": "ERR"}.get(status, status)


def _write_results(
    results: list[dict],
    results_dir: Path,
    timestamp: str,
) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)

    json_path = results_dir / f"dogfood_{timestamp}.json"
    md_path = results_dir / f"dogfood_{timestamp}.md"

    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    # Markdown summary
    lines = [
        "# Dogfood run results",
        f"",
        f"Run at: {timestamp}",
        f"",
        f"| phase | name | pass | notes |",
        f"|-------|------|------|-------|",
    ]
    for r in results:
        icon = _status_icon(r["status"])
        lines.append(f"| {r['id']} | {r['name']} | {icon} | {r['notes']} |")

    totals = {k: sum(1 for r in results if r["status"] == k) for k in ("PASS", "FAIL", "SKIP", "ERROR")}
    lines += [
        "",
        f"**Total:** {len(results)} scenarios — "
        f"{totals['PASS']} PASS, {totals['FAIL']} FAIL, "
        f"{totals['SKIP']} SKIP, {totals['ERROR']} ERROR",
    ]

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    _load_dotenv(_REPO_ROOT)

    # Resolve cost bands
    allowed_costs: set[str] = set(c.strip() for c in args.cost.split(",") if c.strip())
    if args.include_expensive:
        allowed_costs.add("expensive")

    phase_filters = [p.strip() for p in args.phase.split(",") if p.strip()]
    family_filters = [f.strip() for f in args.family.split(",") if f.strip()]

    all_sc = all_scenarios()
    selected = _filter_scenarios(
        all_sc,
        phase_filters,
        family_filters,
        allowed_costs,
        args.include_real_claude,
    )

    # Sort by id for deterministic ordering
    selected.sort(key=lambda s: [int(x) if x.isdigit() else x for x in s.id.replace(".", " ").split()])

    if args.list_only:
        if not selected:
            print(f"0 scenarios would run", file=sys.stderr)
        else:
            print(f"{len(selected)} scenario(s) would run:", file=sys.stderr)
            for s in selected:
                claude_tag = " [needs-claude]" if s.needs_claude else ""
                print(f"  {s.id:6s}  [{s.cost:8s}]  {s.family:16s}  {s.name}{claude_tag}", file=sys.stderr)
        return 0

    # Budget check
    estimated_cost = sum(_COST_ESTIMATE.get(s.cost, 0.0) for s in selected)
    if estimated_cost > args.max_budget_usd:
        print(
            f"ERROR: estimated cost ${estimated_cost:.2f} exceeds --max-budget-usd ${args.max_budget_usd:.2f}",
            file=sys.stderr,
        )
        print("Use --max-budget-usd to raise the limit or narrow the filter.", file=sys.stderr)
        return 1

    if not selected:
        print(f"0 scenarios run", file=sys.stderr)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        results_dir = Path(args.results_dir)
        json_path, md_path = _write_results([], results_dir, timestamp)
        print(f"Results: {json_path}", file=sys.stderr)
        return 0

    # Build base context (plugin_data_dir is ephemeral per-scenario, set in _run_scenario)
    repo_paths: dict[str, Path] = {}
    ts_repo = _resolve_repo("CHAMELEON_TEST_TS_REPO")
    ruby_repo = _resolve_repo("CHAMELEON_TEST_RUBY_REPO")
    if ts_repo is not None:
        repo_paths["ts"] = ts_repo
    if ruby_repo is not None:
        repo_paths["ruby"] = ruby_repo

    base_ctx = Context(
        plugin_root=_REPO_ROOT,
        plugin_data_dir=Path(tempfile.gettempdir()),  # placeholder; overridden per scenario
        repo_paths=repo_paths,
        real_claude_allowed=args.include_real_claude,
        cost_so_far_usd=0.0,
    )

    print(f"Running {len(selected)} scenario(s)...", file=sys.stderr)

    all_results: list[dict] = []
    any_failed = False
    cost_so_far = 0.0

    for s in selected:
        print(f"[RUN]  {s.id:6s}  {s.name}", file=sys.stderr)
        base_ctx.cost_so_far_usd = cost_so_far

        result = _run_scenario(s, base_ctx)
        cost_so_far += result.cost_usd

        tag = result.status
        notes_str = f"  {result.notes}" if result.notes else ""
        print(
            f"[{tag}] {s.id:6s}  {s.name}  ({result.duration_s:.2f}s){notes_str}",
            file=sys.stderr,
        )

        if result.status in ("FAIL", "ERROR"):
            any_failed = True

        all_results.append({
            "id": s.id,
            "name": s.name,
            "family": s.family,
            "status": result.status,
            "notes": result.notes,
            "duration_s": round(result.duration_s, 3),
            "cost_usd": result.cost_usd,
        })

    # Summary table
    totals = {k: sum(1 for r in all_results if r["status"] == k) for k in ("PASS", "FAIL", "SKIP", "ERROR")}
    print("", file=sys.stderr)
    print("| phase  | name                          | pass | notes |", file=sys.stderr)
    print("|--------|-------------------------------|------|-------|", file=sys.stderr)
    for r in all_results:
        icon = _status_icon(r["status"])
        notes_col = r["notes"][:60] if r["notes"] else ""
        print(f"| {r['id']:6s} | {r['name'][:29]:29s} | {icon:4s} | {notes_col} |", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        f"Summary: {len(all_results)} scenarios — "
        f"{totals['PASS']} PASS, {totals['FAIL']} FAIL, "
        f"{totals['SKIP']} SKIP, {totals['ERROR']} ERROR  "
        f"(estimated cost: ${cost_so_far:.4f})",
        file=sys.stderr,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results_dir = Path(args.results_dir)
    json_path, md_path = _write_results(all_results, results_dir, timestamp)
    print(f"Results: {json_path}", file=sys.stderr)
    print(f"         {md_path}", file=sys.stderr)

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
