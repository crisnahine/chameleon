"""Phase 3.x: edit injection scenarios."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from tests.dogfood.scenario import Result, Scenario

_FIXTURE_REL = "tests/fixtures/eval_repos/ts_minimal"


def _ensure_mcp_on_path(ctx) -> None:
    d = str(ctx.plugin_root / "mcp")
    if d not in sys.path:
        sys.path.insert(0, d)


def _set_env(ctx) -> dict:
    old = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
    }
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    return old


def _restore_env(old: dict) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# 3.1  Cooperative edit injection  (real claude, requires repo:ts)
# ---------------------------------------------------------------------------

def _run_cooperative_edit_injection(ctx) -> Result:
    ts_repo = ctx.repo_paths.get("ts")
    if ts_repo is None or not ts_repo.is_dir():
        return Result(status="SKIP", notes="CHAMELEON_TEST_TS_REPO not set")

    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import bootstrap_repo, trust_profile  # type: ignore[import]

    # Ensure the repo is bootstrapped + trusted
    old = _set_env(ctx)
    try:
        if not (ts_repo / ".chameleon" / "COMMITTED").exists():
            bootstrap_repo(str(ts_repo))
        trust_profile(str(ts_repo), ts_repo.name)
    finally:
        _restore_env(old)

    # Create a temp file under src/utils/
    utils_dir = ts_repo / "src" / "utils"
    utils_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = utils_dir / "_dogfood_injection_test.ts"
    tmp_file.write_text("export const x = 'before';\n", encoding="utf-8")

    plugin_root = ctx.plugin_root
    env = os.environ.copy()
    env["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
    env["CHAMELEON_ALLOW_TMP_REPO"] = "1"

    prompt = (
        f"Read the file {tmp_file} then edit it to replace the string 'before' "
        f"with the string 'after'. Make only that change."
    )

    cost_usd = 0.0
    try:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--plugin-dir", str(plugin_root),
                "--output-format", "stream-json",
                "--verbose",
                "--include-hook-events",
                "--max-turns", "6",
                "--model", "sonnet",
                "--permission-mode", "acceptEdits",
                "--allowedTools", "Read,Edit",
            ],
            cwd=str(ts_repo),
            capture_output=True, text=True, timeout=300,
            env=env, check=False,
        )
    except subprocess.TimeoutExpired:
        tmp_file.unlink(missing_ok=True)
        return Result(status="FAIL", notes="claude -p timed out after 300s")
    except FileNotFoundError:
        tmp_file.unlink(missing_ok=True)
        return Result(status="SKIP", notes="claude CLI not found in PATH")
    finally:
        tmp_file.unlink(missing_ok=True)

    pretool_advisories: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            cost_usd = float(obj.get("total_cost_usd", 0.0))
        if obj.get("type") == "system" and obj.get("subtype") == "hook_response":
            hook = obj.get("hook_name", "")
            if hook.startswith("PreToolUse"):
                stdout = obj.get("stdout", "")
                if "additionalContext" in stdout:
                    pretool_advisories.append(stdout)

    if not pretool_advisories:
        return Result(
            status="FAIL",
            notes="PreToolUse:Edit fired but no additionalContext advisory captured",
            cost_usd=cost_usd,
        )

    advisory_blob = pretool_advisories[0]
    if "<chameleon-context>" not in advisory_blob:
        return Result(
            status="FAIL",
            notes=f"advisory present but missing <chameleon-context>: {advisory_blob[:200]}",
            cost_usd=cost_usd,
        )
    if "archetype=" not in advisory_blob:
        return Result(
            status="FAIL",
            notes=f"advisory missing archetype= field: {advisory_blob[:200]}",
            cost_usd=cost_usd,
        )
    # Verify a canonical witness path appears somewhere in the advisory
    has_witness = (
        "Canonical witness:" in advisory_blob
        or "witness_path" in advisory_blob
        or "src/" in advisory_blob
    )
    if not has_witness:
        return Result(
            status="FAIL",
            notes=f"advisory has no canonical witness reference: {advisory_blob[:300]}",
            cost_usd=cost_usd,
        )

    return Result(
        status="PASS",
        notes=f"PreToolUse advisory injected with archetype context ({len(pretool_advisories)} advisory events)",
        cost_usd=cost_usd,
    )


# ---------------------------------------------------------------------------
# 3.2  Injection contains canonical+rules+idioms  (cheap, no claude)
# ---------------------------------------------------------------------------

def _run_injection_shape(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import get_pattern_context, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    # Fresh copy of fixture into ctx.plugin_data_dir
    repo = ctx.plugin_data_dir / "ts_minimal"
    shutil.copytree(fixture_src, repo)

    old = _set_env(ctx)
    try:
        # Trust the fixture repo so full context is returned
        trust_profile(str(repo), repo.name)

        # Call get_pattern_context on a known .ts file
        ts_file = repo / "src" / "utils" / "format_date.ts"
        if not ts_file.is_file():
            ts_file = next(repo.rglob("*.ts"), None)
        if ts_file is None:
            return Result(status="SKIP", notes="no .ts files in ts_minimal fixture")

        response = get_pattern_context(str(ts_file))
    finally:
        _restore_env(old)

    data = response.get("data", {})

    # archetype.archetype must be non-null
    archetype_obj = data.get("archetype", {}) or {}
    archetype_name = archetype_obj.get("archetype")
    if not archetype_name:
        return Result(status="FAIL", notes=f"data.archetype.archetype is null/missing; data={data}")

    # canonical_excerpt.content must be a non-empty string
    canonical = data.get("canonical_excerpt", {}) or {}
    content = canonical.get("content")
    if not isinstance(content, str) or not content.strip():
        return Result(
            status="FAIL",
            notes=f"data.canonical_excerpt.content empty or missing; canonical={canonical}",
        )

    # rules must be a list (may be empty for minimal fixture)
    rules = data.get("rules")
    if not isinstance(rules, list):
        return Result(status="FAIL", notes=f"data.rules is not a list: {type(rules)}")

    # idioms field must exist (may be empty string for minimal fixture)
    if "idioms" not in data:
        return Result(status="FAIL", notes="data.idioms key missing from response")

    return Result(
        status="PASS",
        notes=(
            f"archetype={archetype_name!r}, "
            f"canonical_content_len={len(content)}, "
            f"rules_count={len(rules)}, "
            f"idioms_present={'idioms' in data}"
        ),
    )


# ---------------------------------------------------------------------------
# 3.3  Hook dedup within one turn  (cheap, no claude)
# ---------------------------------------------------------------------------

def _run_session_start_dedup(ctx) -> Result:
    """Verify SessionStart hook produces consistent output across two invocations.

    chameleon does not dedup PreToolUse (each Edit fires independently).
    The 'dedup' tested here is that SessionStart content is stable: two
    invocations with the same session_id return identical (or both non-empty)
    wrapped content, not a growing/duplicate blob.
    """
    plugin_root = ctx.plugin_root
    mcp_dir = plugin_root / "mcp"
    venv_python = mcp_dir / ".venv" / "bin" / "python"

    if venv_python.is_file():
        python = str(venv_python)
    elif shutil.which("python3"):
        python = shutil.which("python3")
    else:
        return Result(status="SKIP", notes="no suitable python interpreter found")

    # Use an isolated plugin_data_dir so no cross-test session markers bleed in
    env = os.environ.copy()
    env["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env["PYTHONPATH"] = str(mcp_dir)

    session_payload = json.dumps({
        "session_id": "dogfood-dedup-test-abc123",
        "hook_event_name": "SessionStart",
    })

    def _invoke_session_start() -> str:
        proc = subprocess.run(
            [python, "-m", "chameleon_mcp.hook_helper", "session-start"],
            input=session_payload,
            capture_output=True, text=True, timeout=30,
            env=env,
        )
        return proc.stdout.strip()

    try:
        out1 = _invoke_session_start()
        out2 = _invoke_session_start()
    except subprocess.TimeoutExpired:
        return Result(status="FAIL", notes="session-start hook timed out")
    except FileNotFoundError as exc:
        return Result(status="SKIP", notes=f"python not found: {exc}")

    # Both outputs should be non-empty valid JSON
    if not out1 or not out2:
        return Result(
            status="FAIL",
            notes=f"session-start emitted empty output (out1={out1!r}, out2={out2!r})",
        )

    try:
        obj1 = json.loads(out1)
        json.loads(out2)
    except json.JSONDecodeError as exc:
        return Result(status="FAIL", notes=f"session-start output not valid JSON: {exc}")

    # Both outputs must be identical (no growing duplicate injection)
    if out1 != out2:
        return Result(
            status="FAIL",
            notes=(
                "SessionStart produced different output on two invocations -- "
                f"out1_len={len(out1)}, out2_len={len(out2)}"
            ),
        )

    # Confirm the output contains chameleon-context content
    combined = json.dumps(obj1)
    if "chameleon" not in combined.lower() and "additionalContext" not in combined:
        return Result(
            status="FAIL",
            notes=f"SessionStart output missing chameleon/additionalContext: {out1[:200]}",
        )

    return Result(
        status="PASS",
        notes=f"SessionStart output stable across 2 invocations (len={len(out1)})",
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="3.1",
        name="cooperative edit injection",
        family="injection",
        needs_claude=True,
        cost="moderate",
        requires=["repo:ts"],
        run=_run_cooperative_edit_injection,
    ),
    Scenario(
        id="3.2",
        name="injection contains canonical+rules+idioms",
        family="injection",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_injection_shape,
    ),
    Scenario(
        id="3.3",
        name="hook dedup within one turn",
        family="injection",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_session_start_dedup,
    ),
]
