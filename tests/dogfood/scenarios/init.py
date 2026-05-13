"""Phase 1.x: /chameleon-init scenarios."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario

# ---------------------------------------------------------------------------
# 1.1 /chameleon-init cooperative  (real claude)
# ---------------------------------------------------------------------------

def _run_init_cooperative(ctx) -> Result:
    ts_repo = ctx.repo_paths.get("ts")
    if ts_repo is None or not ts_repo.is_dir():
        return Result(status="SKIP", notes="CHAMELEON_TEST_TS_REPO not set")

    # Tear down any existing .chameleon/ and plugin data for this repo
    chameleon_dir = ts_repo / ".chameleon"
    if chameleon_dir.exists():
        shutil.rmtree(chameleon_dir, ignore_errors=True)

    plugin_root = ctx.plugin_root
    plugin_data_dir = ctx.plugin_data_dir  # ephemeral tmpdir from runner

    env = os.environ.copy()
    env["CHAMELEON_PLUGIN_DATA"] = str(plugin_data_dir)
    env["CHAMELEON_ALLOW_TMP_REPO"] = "1"

    prompt = "use the chameleon mcp to initialize this repo's chameleon profile"

    cmd = [
        "claude", "-p", prompt,
        "--plugin-dir", str(plugin_root),
        "--output-format", "stream-json",
        "--verbose",
        "--include-hook-events",
        "--max-turns", "6",
        "--model", "sonnet",
        "--permission-mode", "acceptEdits",
        "--allowedTools",
        "mcp__plugin_chameleon_chameleon-mcp__bootstrap_repo,"
        "mcp__plugin_chameleon_chameleon-mcp__detect_repo,"
        "mcp__plugin_chameleon_chameleon-mcp__get_archetype,"
        "mcp__plugin_chameleon_chameleon-mcp__get_canonical_excerpt,"
        "mcp__plugin_chameleon_chameleon-mcp__get_rules,"
        "Read,Bash",
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ts_repo),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(status="FAIL", notes="claude -p timed out after 300s")
    except FileNotFoundError:
        return Result(status="SKIP", notes="claude CLI not found in PATH")

    # Extract cost from stream-json events
    cost_usd = 0.0
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            cost_usd = float(ev.get("total_cost_usd", 0.0))

    committed = chameleon_dir / "COMMITTED"
    if committed.is_file():
        return Result(status="PASS", notes="COMMITTED sentinel present", cost_usd=cost_usd)

    stderr_tail = proc.stderr[-400:] if proc.stderr else ""
    return Result(
        status="FAIL",
        notes=f"COMMITTED not found after claude session. stderr tail: {stderr_tail}",
        cost_usd=cost_usd,
    )


# ---------------------------------------------------------------------------
# 1.2 /chameleon-init non-cooperative  (cheap, no claude)
# ---------------------------------------------------------------------------

def _run_init_non_cooperative(ctx) -> Result:
    mcp_dir = str(ctx.plugin_root / "mcp")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)

    from chameleon_mcp.tools import bootstrap_repo  # type: ignore[import]

    fixture_src = ctx.plugin_root / "tests" / "fixtures" / "eval_repos" / "ts_minimal"
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes="ts_minimal fixture missing")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo_copy = Path(tmp) / "ts_minimal"
        shutil.copytree(fixture_src, repo_copy)

        old_plugin_data = os.environ.get("CHAMELEON_PLUGIN_DATA")
        os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
        try:
            response = bootstrap_repo(str(repo_copy), force=False)
        finally:
            if old_plugin_data is None:
                os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
            else:
                os.environ["CHAMELEON_PLUGIN_DATA"] = old_plugin_data

        data = response.get("data", {})
        status = data.get("status")
        if status == "already_bootstrapped":
            return Result(status="PASS", notes="already_bootstrapped returned as expected")
        return Result(
            status="FAIL",
            notes=f"expected already_bootstrapped, got status={status!r}; data={data}",
        )


# ---------------------------------------------------------------------------
# 1.3 Idempotence  (cheap, no claude)
# ---------------------------------------------------------------------------

def _structural_fields(profile: dict) -> dict:
    """Return profile fields that should be stable across two bootstraps."""
    skip = {"created_at", "generation"}
    return {k: v for k, v in profile.items() if k not in skip}


def _run_init_idempotence(ctx) -> Result:
    import time as _time

    fixture_src = ctx.plugin_root / "tests" / "fixtures" / "eval_repos" / "ts_minimal"
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes="ts_minimal fixture missing")

    mcp_dir = str(ctx.plugin_root / "mcp")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)

    from chameleon_mcp.tools import bootstrap_repo  # type: ignore[import]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo_copy = Path(tmp) / "ts_minimal"
        shutil.copytree(fixture_src, repo_copy)

        # Tear down .chameleon/ so we start clean
        chameleon_dir = repo_copy / ".chameleon"
        if chameleon_dir.exists():
            shutil.rmtree(chameleon_dir)

        old_plugin_data = os.environ.get("CHAMELEON_PLUGIN_DATA")
        os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
        try:
            r1 = bootstrap_repo(str(repo_copy), force=True)
        finally:
            if old_plugin_data is None:
                os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
            else:
                os.environ["CHAMELEON_PLUGIN_DATA"] = old_plugin_data

        if r1["data"].get("status") != "success":
            return Result(status="FAIL", notes=f"first bootstrap failed: {r1['data']}")

        profile_file = chameleon_dir / "profile.json"
        if not profile_file.is_file():
            return Result(status="FAIL", notes="profile.json missing after first bootstrap")

        with profile_file.open("r", encoding="utf-8") as fh:
            profile_first = json.load(fh)

        # Sleep 1s so the wall-clock generation counter advances between runs.
        # The generation is int(time.time()) inside the orchestrator and is not
        # controllable via the `now` kwarg (that only affects canonical selection).
        _time.sleep(1)

        old_plugin_data = os.environ.get("CHAMELEON_PLUGIN_DATA")
        os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
        try:
            r2 = bootstrap_repo(str(repo_copy), force=True)
        finally:
            if old_plugin_data is None:
                os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
            else:
                os.environ["CHAMELEON_PLUGIN_DATA"] = old_plugin_data

        if r2["data"].get("status") != "success":
            return Result(status="FAIL", notes=f"second bootstrap failed: {r2['data']}")

        with profile_file.open("r", encoding="utf-8") as fh:
            profile_second = json.load(fh)

        gen1 = profile_first.get("generation", -1)
        gen2 = profile_second.get("generation", -2)
        if gen2 <= gen1:
            return Result(
                status="FAIL",
                notes=f"generation did not increment: {gen1} -> {gen2}",
            )

        struct1 = _structural_fields(profile_first)
        struct2 = _structural_fields(profile_second)
        if struct1 != struct2:
            diffs = [k for k in (set(struct1) | set(struct2)) if struct1.get(k) != struct2.get(k)]
            return Result(
                status="FAIL",
                notes=f"structural fields differ between bootstraps: {diffs}",
            )

        return Result(
            status="PASS",
            notes=f"generations differ ({gen1} -> {gen2}), structural fields match",
        )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="1.1",
        name="/chameleon-init cooperative",
        family="init",
        needs_claude=True,
        cost="moderate",
        requires=["repo:ts"],
        run=_run_init_cooperative,
    ),
    Scenario(
        id="1.2",
        name="/chameleon-init non-cooperative",
        family="init",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_init_non_cooperative,
    ),
    Scenario(
        id="1.3",
        name="idempotence",
        family="init",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_init_idempotence,
    ),
]
