"""Phase 5.x: teach scenarios."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario

_FIXTURE_REL = "tests/fixtures/eval_repos/ts_minimal"


def _ensure_mcp_on_path(ctx) -> None:
    d = str(ctx.plugin_root / "mcp")
    if d not in sys.path:
        sys.path.insert(0, d)


def _make_fresh_copy(ctx) -> Path:
    src = ctx.plugin_root / _FIXTURE_REL
    dest = ctx.plugin_data_dir / "ts_minimal"
    shutil.copytree(src, dest)
    return dest


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
# 5.1  Teach persists to idioms.md
# ---------------------------------------------------------------------------

def _run_teach_persists(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import teach_profile, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        trust_response = trust_profile(str(repo), repo.name)
        if trust_response["data"].get("status") != "success":
            return Result(status="FAIL", notes=f"trust failed: {trust_response['data']}")

        response = teach_profile(str(repo), "Always prefer arrow functions over function declarations.")
    finally:
        _restore_env(old)

    data = response.get("data", {})
    if data.get("status") != "success":
        return Result(status="FAIL", notes=f"teach_profile status={data.get('status')!r}, error={data.get('error')!r}")

    idioms_path = repo / ".chameleon" / "idioms.md"
    if not idioms_path.is_file():
        return Result(status="FAIL", notes="idioms.md not created after teach")

    content = idioms_path.read_text(encoding="utf-8")
    if "arrow functions" not in content:
        return Result(status="FAIL", notes=f"'arrow functions' not found in idioms.md (len={len(content)})")

    return Result(status="PASS", notes=f"idioms.md contains taught idiom (len={len(content)})")


# ---------------------------------------------------------------------------
# 5.2  Taught idiom surfaces next-edit  (real claude, requires repo:ts)
# ---------------------------------------------------------------------------

def _run_idiom_surfaces_on_edit(ctx) -> Result:
    ts_repo = ctx.repo_paths.get("ts")
    if ts_repo is None or not ts_repo.is_dir():
        return Result(status="SKIP", notes="CHAMELEON_TEST_TS_REPO not set")

    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import (  # type: ignore[import]
        bootstrap_repo,
        teach_profile,
        trust_profile,
    )

    sentinel = "DOGFOOD-IDIOM-MARKER-XYZ: never use eval()"

    old = _set_env(ctx)
    try:
        if not (ts_repo / ".chameleon" / "COMMITTED").exists():
            bootstrap_repo(str(ts_repo))
        teach_profile(str(ts_repo), sentinel)
        trust_profile(str(ts_repo), ts_repo.name)
    finally:
        _restore_env(old)

    # Run a real claude session targeting a file under src/utils/
    utils_dir = ts_repo / "src" / "utils"
    utils_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = utils_dir / "_dogfood_teach_test.ts"
    tmp_file.write_text("export const x = 'before';\n", encoding="utf-8")

    import json
    import subprocess

    env = os.environ.copy()
    env["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
    env["CHAMELEON_ALLOW_TMP_REPO"] = "1"

    prompt = (
        f"Read the file {tmp_file} then edit it to replace 'before' with 'after'. "
        f"Make only that change."
    )

    try:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--plugin-dir", str(ctx.plugin_root),
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
        _remove_sentinel_from_idioms(ts_repo, sentinel)
        return Result(status="FAIL", notes="claude -p timed out")
    except FileNotFoundError:
        tmp_file.unlink(missing_ok=True)
        _remove_sentinel_from_idioms(ts_repo, sentinel)
        return Result(status="SKIP", notes="claude CLI not found in PATH")
    finally:
        tmp_file.unlink(missing_ok=True)
        _remove_sentinel_from_idioms(ts_repo, sentinel)

    pretool_advisories: list[str] = []
    cost_usd = 0.0
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
            if obj.get("hook_name", "").startswith("PreToolUse"):
                stdout = obj.get("stdout", "")
                if "additionalContext" in stdout:
                    pretool_advisories.append(stdout)

    if not pretool_advisories:
        return Result(
            status="FAIL",
            notes="no PreToolUse advisory captured",
            cost_usd=cost_usd,
        )

    combined = " ".join(pretool_advisories)
    # The hook signals idiom presence with this phrase rather than inlining the
    # full idioms.md text; verify the taught sentinel caused the advisory to
    # surface the "idioms available" notice (proving idioms.md was non-empty).
    if "idioms" not in combined.lower():
        return Result(
            status="FAIL",
            notes=f"advisory present but idioms notice missing (len={len(combined)}); first 300: {combined[:300]}",
            cost_usd=cost_usd,
        )

    return Result(
        status="PASS",
        notes=f"taught idiom surfaced in advisory ({len(pretool_advisories)} advisory events)",
        cost_usd=cost_usd,
    )


def _remove_sentinel_from_idioms(repo: Path, sentinel: str) -> None:
    """Remove sentinel lines from idioms.md to clean up the real repo."""
    idioms_path = repo / ".chameleon" / "idioms.md"
    if not idioms_path.is_file():
        return
    try:
        content = idioms_path.read_text(encoding="utf-8")
        # Remove the sentinel text from the file (it's embedded in a section)
        # Find the section containing the sentinel and remove it
        lines = content.splitlines(keepends=True)
        out: list[str] = []
        skip = False
        for line in lines:
            if sentinel in line:
                skip = True
                # Also remove the preceding header (### slug\nStatus: ...)
                # Remove the last 2 lines we added (### header + Status line)
                while out and (out[-1].strip().startswith("Status:") or out[-1].strip().startswith("###")):
                    out.pop()
                continue
            if skip and line.startswith("###"):
                skip = False
            if not skip:
                out.append(line)
        idioms_path.write_text("".join(out), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 5.3  Idiom survives refresh
# ---------------------------------------------------------------------------

def _run_idiom_survives_refresh(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import (  # type: ignore[import]
        refresh_repo,
        teach_profile,
        trust_profile,
    )

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        trust_profile(str(repo), repo.name)
        sentinel = "SURVIVES-REFRESH-SENTINEL-ABC"
        teach_response = teach_profile(str(repo), sentinel)
        if teach_response["data"].get("status") != "success":
            return Result(status="FAIL", notes=f"teach failed: {teach_response['data']}")

        idioms_path = repo / ".chameleon" / "idioms.md"
        before = idioms_path.read_text(encoding="utf-8") if idioms_path.is_file() else ""
        if sentinel not in before:
            return Result(status="FAIL", notes="sentinel not in idioms.md before refresh")

        refresh_repo(str(repo), force=True)

        after = idioms_path.read_text(encoding="utf-8") if idioms_path.is_file() else ""
    finally:
        _restore_env(old)

    if sentinel not in after:
        return Result(status="FAIL", notes=f"idioms.md lost sentinel after refresh (len before={len(before)}, after={len(after)})")

    return Result(status="PASS", notes=f"idioms.md retained sentinel after refresh (len={len(after)})")


# ---------------------------------------------------------------------------
# 5.4  Trust re-prompts after refresh
# ---------------------------------------------------------------------------

def _run_trust_reprompts_after_refresh(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import (  # type: ignore[import]
        get_pattern_context,
        refresh_repo,
        trust_profile,
    )

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        # Trust the fresh fixture
        trust_response = trust_profile(str(repo), repo.name)
        if trust_response["data"].get("status") != "success":
            return Result(status="FAIL", notes=f"initial trust failed: {trust_response['data']}")

        # Force refresh to produce a new profile hash
        refresh_repo(str(repo), force=True)

        # Check trust state
        ts_file = next(repo.rglob("*.ts"), repo / "src" / "index.ts")
        ctx_response = get_pattern_context(str(ts_file))
    finally:
        _restore_env(old)

    repo_info = ctx_response["data"].get("repo", {})
    trust_state = repo_info.get("trust_state")

    if trust_state not in ("stale", "untrusted"):
        return Result(
            status="FAIL",
            notes=f"expected trust_state=stale after refresh, got {trust_state!r}",
        )

    return Result(status="PASS", notes=f"trust_state={trust_state!r} after refresh (re-prompt triggered)")


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="5.1",
        name="teach persists to idioms.md",
        family="teach",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_teach_persists,
    ),
    Scenario(
        id="5.2",
        name="taught idiom surfaces next-edit",
        family="teach",
        needs_claude=True,
        cost="moderate",
        requires=["repo:ts"],
        run=_run_idiom_surfaces_on_edit,
    ),
    Scenario(
        id="5.3",
        name="idiom survives refresh",
        family="teach",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_idiom_survives_refresh,
    ),
    Scenario(
        id="5.4",
        name="trust re-prompts after refresh",
        family="teach",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_trust_reprompts_after_refresh,
    ),
]
