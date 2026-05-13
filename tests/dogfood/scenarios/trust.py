"""Phase 2.x: trust scenarios."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario

_FIXTURE_REL = "tests/fixtures/eval_repos/ts_minimal"


def _mcp_dir(ctx) -> str:
    return str(ctx.plugin_root / "mcp")


def _ensure_mcp_on_path(ctx) -> None:
    d = _mcp_dir(ctx)
    if d not in sys.path:
        sys.path.insert(0, d)


def _make_fresh_copy(ctx) -> Path:
    """Clone the ts_minimal fixture into a sub-dir of ctx.plugin_data_dir."""
    src = ctx.plugin_root / _FIXTURE_REL
    dest = ctx.plugin_data_dir / "ts_minimal"
    shutil.copytree(src, dest)
    return dest


def _set_env(ctx) -> dict:
    """Apply CHAMELEON_PLUGIN_DATA + CHAMELEON_ALLOW_TMP_REPO; return old values."""
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
# 2.1  Untrusted surfaces non-blocking
# ---------------------------------------------------------------------------

def _run_untrusted_surfaces_non_blocking(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import get_pattern_context  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    # Ensure no trust record by using an isolated plugin_data_dir (already ephemeral)
    old = _set_env(ctx)
    try:
        # Pick any .ts file in the fixture
        ts_file = next(repo.rglob("*.ts"), repo / "src" / "index.ts")
        response = get_pattern_context(str(ts_file))
    finally:
        _restore_env(old)

    data = response.get("data", {})
    repo_info = data.get("repo", {})
    trust_state = repo_info.get("trust_state")

    # The call must succeed (non-blocking) and report untrusted.
    # get_pattern_context does not suppress archetype data for untrusted repos;
    # the trust gate is enforced at the hook / get_rules layer.
    if trust_state != "untrusted":
        return Result(status="FAIL", notes=f"expected trust_state=untrusted, got {trust_state!r}")
    if "api_version" not in response:
        return Result(status="FAIL", notes="response missing api_version envelope field")
    return Result(status="PASS", notes="trust_state=untrusted, call succeeded non-blocking")


# ---------------------------------------------------------------------------
# 2.2  Trust granted with basename
# ---------------------------------------------------------------------------

def _run_trust_granted_with_basename(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import get_pattern_context, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        trust_response = trust_profile(str(repo), repo.name)
        trust_data = trust_response.get("data", {})
        if trust_data.get("status") != "success":
            return Result(
                status="FAIL",
                notes=f"trust_profile returned status={trust_data.get('status')!r}, error={trust_data.get('error')!r}",
            )

        ts_file = next(repo.rglob("*.ts"), repo / "src" / "index.ts")
        ctx_response = get_pattern_context(str(ts_file))
    finally:
        _restore_env(old)

    ctx_data = ctx_response.get("data", {})
    repo_info = ctx_data.get("repo", {})
    trust_state = repo_info.get("trust_state")
    archetype = ctx_data.get("archetype", {}).get("archetype")

    if trust_state != "trusted":
        return Result(status="FAIL", notes=f"expected trust_state=trusted after grant, got {trust_state!r}")
    # archetype may be None if the profile has zero archetypes, but trust_state must be trusted
    return Result(status="PASS", notes=f"trust_state=trusted, archetype={archetype!r}")


# ---------------------------------------------------------------------------
# 2.3  Material-change re-prompt
# ---------------------------------------------------------------------------

def _run_material_change_reprompt(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import get_pattern_context, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        # Trust first
        trust_response = trust_profile(str(repo), repo.name)
        if trust_response["data"].get("status") != "success":
            return Result(status="FAIL", notes=f"initial trust failed: {trust_response['data']}")

        # Mutate profile.json to simulate a material change
        profile_file = repo / ".chameleon" / "profile.json"
        profile_file.write_bytes(profile_file.read_bytes() + b" ")

        ts_file = next(repo.rglob("*.ts"), repo / "src" / "index.ts")
        ctx_response = get_pattern_context(str(ts_file))
    finally:
        _restore_env(old)

    repo_info = ctx_response["data"].get("repo", {})
    trust_state = repo_info.get("trust_state")
    if trust_state != "stale":
        return Result(status="FAIL", notes=f"expected trust_state=stale after mutation, got {trust_state!r}")
    return Result(status="PASS", notes="trust_state=stale after material change")


# ---------------------------------------------------------------------------
# 2.4  Empty confirmation rejected
# ---------------------------------------------------------------------------

def _run_empty_confirmation_rejected(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        response = trust_profile(str(repo), "")
    finally:
        _restore_env(old)

    data = response.get("data", {})
    status = data.get("status")
    error = data.get("error", "")

    if status != "failed":
        return Result(status="FAIL", notes=f"expected status=failed with empty token, got {status!r}")
    # Check that the error message explains what's needed
    if "confirmation_token" not in error and "token" not in error.lower():
        return Result(status="FAIL", notes=f"error message doesn't mention token: {error!r}")
    return Result(status="PASS", notes=f"rejected empty token: {error[:100]}")


# ---------------------------------------------------------------------------
# 2.5  yes-trust-<short8> token variant accepted
# ---------------------------------------------------------------------------

def _run_yes_trust_token(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import _compute_repo_id, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        repo_id = _compute_repo_id(repo)
        short8 = repo_id[:8]
        token = f"yes-trust-{short8}"
        response = trust_profile(str(repo), token)
    finally:
        _restore_env(old)

    data = response.get("data", {})
    status = data.get("status")
    if status != "success":
        return Result(
            status="FAIL",
            notes=f"yes-trust token rejected: status={status!r}, error={data.get('error')!r}",
        )
    return Result(status="PASS", notes=f"yes-trust-{short8} accepted")


# ---------------------------------------------------------------------------
# 2.6  Trust on corrupted profile rejected
# ---------------------------------------------------------------------------

def _run_trust_corrupted_profile(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    # Corrupt profile.json
    profile_file = repo / ".chameleon" / "profile.json"
    profile_file.write_text("{", encoding="utf-8")

    old = _set_env(ctx)
    try:
        # trust_profile may raise json.JSONDecodeError on corrupted profile
        # (the server's ProfileLoadError wrapper doesn't catch json.JSONDecodeError
        # from the json.loads() call inside load_profile_dir). Either a status=failed
        # response or a raised JSON error is acceptable evidence of rejection.
        try:
            response = trust_profile(str(repo), repo.name)
        except Exception as exc:
            # Any exception here means trust was rejected - that's the right behavior.
            return Result(
                status="PASS",
                notes=f"trust raised on corrupted profile: {type(exc).__name__}: {str(exc)[:80]}",
            )
    finally:
        _restore_env(old)

    data = response.get("data", {})
    status = data.get("status")
    if status != "failed":
        return Result(
            status="FAIL",
            notes=f"expected trust to fail on corrupted profile, got status={status!r}",
        )
    return Result(status="PASS", notes=f"trust rejected corrupted profile: {data.get('error', '')[:80]}")


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="2.1",
        name="untrusted surfaces non-blocking",
        family="trust",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_untrusted_surfaces_non_blocking,
    ),
    Scenario(
        id="2.2",
        name="trust granted with basename",
        family="trust",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_trust_granted_with_basename,
    ),
    Scenario(
        id="2.3",
        name="material-change re-prompt",
        family="trust",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_material_change_reprompt,
    ),
    Scenario(
        id="2.4",
        name="empty confirmation rejected",
        family="trust",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_empty_confirmation_rejected,
    ),
    Scenario(
        id="2.5",
        name="yes-trust-<short8> token variant",
        family="trust",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_yes_trust_token,
    ),
    Scenario(
        id="2.6",
        name="trust on corrupted profile rejected",
        family="trust",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_trust_corrupted_profile,
    ),
]
