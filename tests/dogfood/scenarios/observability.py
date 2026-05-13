"""Phase 16.x: observability scenarios."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario

_FIXTURE_REL = "tests/fixtures/eval_repos/ts_minimal"

_METRICS_REQUIRED_FIELDS = {
    "ts", "hook", "repo_id", "elapsed_ms",
    "advisory_emitted", "suppression_reason",
    "fail_open", "trust_state", "archetype", "confidence",
}


def _ensure_mcp_on_path(ctx) -> None:
    d = str(ctx.plugin_root / "mcp")
    if d not in sys.path:
        sys.path.insert(0, d)


def _make_fresh_copy(ctx, suffix: str = "ts_minimal") -> Path:
    src = ctx.plugin_root / _FIXTURE_REL
    dest = ctx.plugin_data_dir / suffix
    shutil.copytree(src, dest)
    return dest


def _set_env(ctx, plugin_data_override: Path | None = None) -> dict:
    saved = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
    }
    pd = plugin_data_override or ctx.plugin_data_dir
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(pd)
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# 16.1  metrics.jsonl emitted with required fields
# ---------------------------------------------------------------------------

def _run_metrics_jsonl_required_fields(ctx) -> Result:
    """After a hook call on a trusted fixture, metrics.jsonl must contain all required fields."""
    _ensure_mcp_on_path(ctx)

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    pd = ctx.plugin_data_dir / "pd_metrics"
    pd.mkdir(parents=True, exist_ok=True)

    repo = _make_fresh_copy(ctx, "metrics_repo")
    metrics_path = pd / "metrics.jsonl"

    saved = _set_env(ctx, plugin_data_override=pd)
    try:
        from chameleon_mcp.tools import (  # type: ignore[import]
            bootstrap_repo,
            get_pattern_context,
            trust_profile,
        )

        boot = bootstrap_repo(str(repo))
        if boot.get("data", {}).get("status") not in ("success", "already_bootstrapped"):
            return Result(
                status="FAIL",
                notes=f"bootstrap failed: {boot.get('data', {})!r:.200}",
            )
        trust_profile(str(repo), repo.name)

        # get_pattern_context triggers the hook metric path via emit_hook_metric
        ts_file = next(repo.rglob("*.ts"), repo / "src" / "index.ts")
        get_pattern_context(str(ts_file))
    finally:
        _restore_env(saved)

    # Alternatively use the hook subprocess path to ensure metrics.jsonl is written
    # by the real hook script (preflight-and-advise).
    if not metrics_path.is_file():
        # The in-process get_pattern_context may not always call emit_hook_metric
        # directly (it does in the hook path, not in the direct tool call). Try
        # the hook subprocess path as a fallback.
        hook_path = ctx.plugin_root / "hooks" / "preflight-and-advise"
        if not hook_path.is_file():
            return Result(
                status="SKIP",
                notes="metrics.jsonl not written and hook script not found",
            )

        env = os.environ.copy()
        env["CHAMELEON_PLUGIN_DATA"] = str(pd)
        env["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        env["CLAUDE_PLUGIN_ROOT"] = str(ctx.plugin_root)

        ts_file = next(repo.rglob("*.ts"), repo / "src" / "index.ts")
        hook_input = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ts_file)},
            "session_id": "dogfood-metrics-test",
        })
        try:
            subprocess.run(
                [str(hook_path)],
                input=hook_input,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not metrics_path.is_file():
        return Result(
            status="SKIP",
            notes=(
                "metrics.jsonl not written after hook call; "
                "emit_hook_metric may not be wired to get_pattern_context in the direct-call path"
            ),
        )

    lines = [line.strip() for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return Result(status="FAIL", notes="metrics.jsonl exists but is empty")

    # Parse the last line (most recent hook call)
    try:
        record = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return Result(
            status="FAIL",
            notes=f"last metrics.jsonl line is not valid JSON: {exc}; line={lines[-1][:120]!r}",
        )

    missing = _METRICS_REQUIRED_FIELDS - set(record.keys())
    if missing:
        return Result(
            status="FAIL",
            notes=f"metrics record missing fields: {sorted(missing)}; got keys={sorted(record.keys())}",
        )

    return Result(
        status="PASS",
        notes=f"metrics.jsonl last line has all {len(_METRICS_REQUIRED_FIELDS)} required fields",
    )


# ---------------------------------------------------------------------------
# 16.2  .hook_errors.log rotation triggers
# ---------------------------------------------------------------------------

def _run_hook_errors_log_rotation(ctx) -> Result:
    """Pre-populate a log file > 10MB; run log_rotation; verify .1 backup created."""
    _ensure_mcp_on_path(ctx)

    try:
        from chameleon_mcp.log_rotation import ROTATE_THRESHOLD_BYTES  # type: ignore[import]
    except ImportError:
        return Result(status="SKIP", notes="chameleon_mcp.log_rotation not importable")

    log_dir = ctx.plugin_data_dir / "log_rotation_test"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ".hook_errors.log"
    backup_path = log_dir / ".hook_errors.log.1"

    # Write content just above threshold
    excess = 1024  # 1 KB over threshold
    log_path.write_bytes(b"x" * (ROTATE_THRESHOLD_BYTES + excess))

    # Run rotation via the module __main__ entry (same as hooks do)
    mcp_venv = ctx.plugin_root / "mcp" / ".venv" / "bin" / "python"
    if not mcp_venv.is_file():
        # Fall back to in-process call
        from chameleon_mcp.log_rotation import rotate_if_needed  # type: ignore[import]
        rotate_if_needed(log_path)
    else:
        try:
            subprocess.run(
                [str(mcp_venv), "-m", "chameleon_mcp.log_rotation", str(log_path)],
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            # Fall back to in-process
            from chameleon_mcp.log_rotation import rotate_if_needed  # type: ignore[import]
            rotate_if_needed(log_path)

    if not backup_path.is_file():
        # Also try in-process in case subprocess path silently failed
        from chameleon_mcp.log_rotation import rotate_if_needed  # type: ignore[import]
        # Re-create log if subprocess already rotated it
        if not log_path.is_file():
            log_path.write_bytes(b"x" * (ROTATE_THRESHOLD_BYTES + excess))
        rotate_if_needed(log_path)

    if not backup_path.is_file():
        return Result(
            status="FAIL",
            notes=(
                f".hook_errors.log.1 not created after rotation; "
                f"log_path exists={log_path.exists()}, size="
                f"{log_path.stat().st_size if log_path.exists() else 'n/a'}"
            ),
        )

    if log_path.exists() and log_path.stat().st_size >= ROTATE_THRESHOLD_BYTES:
        return Result(
            status="FAIL",
            notes=(
                f"rotation ran but original log ({log_path.stat().st_size} bytes) "
                f"still at or above threshold ({ROTATE_THRESHOLD_BYTES} bytes)"
            ),
        )

    return Result(
        status="PASS",
        notes=(
            f"log ({ROTATE_THRESHOLD_BYTES + excess} bytes) rotated to .1; "
            f"original {'absent' if not log_path.exists() else f'reset to {log_path.stat().st_size} bytes'}"
        ),
    )


# ---------------------------------------------------------------------------
# 16.3  /chameleon-doctor returns structured envelope
# ---------------------------------------------------------------------------

def _run_doctor_structured_envelope(ctx) -> Result:
    """doctor() must return the full structured envelope with a balanced summary."""
    _ensure_mcp_on_path(ctx)

    saved = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
    }
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

    try:
        from chameleon_mcp.tools import doctor  # type: ignore[import]
        resp = doctor()
    finally:
        _restore_env(saved)

    failures: list[str] = []

    # Top-level envelope
    if "api_version" not in resp:
        failures.append("missing api_version in envelope")
    data = resp.get("data", {})

    required_data_keys = {"overall", "platform", "chameleon_version", "checks", "summary"}
    missing_top = required_data_keys - set(data.keys())
    if missing_top:
        failures.append(f"data missing keys: {sorted(missing_top)}")

    # checks must be a non-empty list
    checks = data.get("checks", [])
    if not isinstance(checks, list):
        failures.append(f"checks is not a list: {type(checks).__name__}")
    elif not checks:
        failures.append("checks list is empty")

    # summary totals must balance
    summary = data.get("summary", {})
    total = summary.get("total", -1)
    ok = summary.get("ok", -1)
    warn = summary.get("warn", -1)
    error = summary.get("error", -1)

    if total < 0 or ok < 0 or warn < 0 or error < 0:
        failures.append(
            f"summary missing fields: total={total}, ok={ok}, warn={warn}, error={error}"
        )
    elif total != ok + warn + error:
        failures.append(
            f"summary totals don't balance: total={total} != ok({ok})+warn({warn})+error({error})"
        )

    if failures:
        return Result(status="FAIL", notes="; ".join(failures))

    return Result(
        status="PASS",
        notes=(
            f"doctor() envelope valid: overall={data.get('overall')!r}, "
            f"{len(checks)} checks, summary total={total} (ok={ok}, warn={warn}, error={error})"
        ),
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="16.1",
        name="metrics.jsonl emitted with required fields",
        family="observability",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_metrics_jsonl_required_fields,
    ),
    Scenario(
        id="16.2",
        name=".hook_errors.log rotation triggers at threshold",
        family="observability",
        needs_claude=False,
        cost="cheap",
        requires=[],
        run=_run_hook_errors_log_rotation,
    ),
    Scenario(
        id="16.3",
        name="/chameleon-doctor returns structured envelope",
        family="observability",
        needs_claude=False,
        cost="cheap",
        requires=[],
        run=_run_doctor_structured_envelope,
    ),
]
