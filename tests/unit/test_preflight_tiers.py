"""Unit tests for PreToolUse tiered injection (v0.7.0).

Tests that preflight_and_advise() selects the right tier based on
enforcement state:
- Tier 1 (~50 token pointer) for archetypes already seen this session
- Tier 2 (annotated canonical) for first-edit archetypes or archetypes
  with prior violations
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch


def _make_pattern_context_result(
    archetype: str,
    *,
    trust_state: str = "trusted",
    repo_id: str = "test-repo",
    confidence_band: str = "high",
    match_quality: str = "exact",
    sub_buckets_count: int = 1,
    canonical_content: str = "export default function Example() {}",
    witness_path: str = "src/Example.tsx",
    rules_count: int = 2,
    summary: str = "React component. src/components/**/*.tsx",
) -> dict:
    """Build a daemon_client.call return value mimicking get_pattern_context."""
    return {
        "data": {
            "archetype": {
                "archetype": archetype,
                "confidence_band": confidence_band,
                "match_quality": match_quality,
                "sub_buckets_count": sub_buckets_count,
                "summary": summary,
            },
            "canonical_excerpt": {
                "content": canonical_content,
                "witness_path": witness_path,
            },
            "rules": [{"id": f"r{i}"} for i in range(rules_count)],
            "idioms": "",
            "repo": {
                "id": repo_id,
                "trust_state": trust_state,
            },
        },
    }


def _run_preflight(payload: dict, *, env: dict | None = None) -> dict:
    """Call preflight_and_advise() with mocked stdin/stdout/env; return emitted JSON.

    Follows the same capture pattern as _run_verify() in test_posttool_verify.py.
    Mocks: daemon_client.call (returns pattern context), find_repo_root,
    _compute_repo_id, is_chameleon_suppressed, record_edit_observation, metrics.
    """
    captured: list[str] = []

    def _fake_write(s: str) -> None:
        captured.append(s)

    stdin_data = json.dumps(payload)
    merged_env = {}
    if env:
        merged_env.update(env)

    with (
        patch("sys.stdin", io.StringIO(stdin_data)),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, merged_env, clear=False),
    ):
        mock_stdout.write = _fake_write
        from chameleon_mcp.hook_helper import preflight_and_advise

        preflight_and_advise()

    output = "".join(captured).strip()
    return json.loads(output) if output else {}


def _run_preflight_with_context(
    tmp_path: Path,
    archetype: str,
    *,
    daemon_result: dict | None = None,
    env: dict | None = None,
) -> dict:
    """Run preflight_and_advise with full mock stack and controllable env.

    Sets CHAMELEON_PLUGIN_DATA so enforcement state reads/writes go to
    tmp_path, and patches the daemon + repo-resolution chain.
    """
    if daemon_result is None:
        daemon_result = _make_pattern_context_result(archetype)

    repo_id = daemon_result["data"]["repo"]["id"]
    data_dir = tmp_path / "chameleon_data"
    repo_data_dir = data_dir / repo_id
    repo_data_dir.mkdir(parents=True, exist_ok=True)

    file_path = str(tmp_path / "src" / "TestFile.tsx")
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text("const x = 1;", encoding="utf-8")

    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "session_id": "test-session",
    }

    merged_env = {"CHAMELEON_PLUGIN_DATA": str(data_dir)}
    if env:
        merged_env.update(env)

    captured: list[str] = []

    def _fake_write(s: str) -> None:
        captured.append(s)

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, merged_env, clear=False),
        patch("chameleon_mcp.daemon_client.call", return_value=daemon_result),
        patch(
            "chameleon_mcp.profile.loader.find_repo_root",
            return_value=tmp_path,
        ),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.drift.observations.record_edit_observation"),
        patch("chameleon_mcp.metrics.emit_hook_metric"),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=data_dir),
    ):
        mock_stdout.write = _fake_write
        from chameleon_mcp.hook_helper import preflight_and_advise

        preflight_and_advise()

    output = "".join(captured).strip()
    return json.loads(output) if output else {}


def _run_preflight_first_edit(tmp_path: Path, archetype: str) -> dict:
    """Run preflight for a file whose archetype has NOT been seen this session.

    No enforcement state file exists, so the archetype is not in
    archetypes_seen. Should trigger Tier 2 (annotated canonical).
    """
    return _run_preflight_with_context(tmp_path, archetype)


def _run_preflight_second_edit(tmp_path: Path, archetype: str) -> dict:
    """Run preflight for a file whose archetype IS already in archetypes_seen.

    Seeds enforcement state with the archetype in archetypes_seen before
    calling preflight. Should trigger Tier 1 (~50 token pointer).
    """
    from chameleon_mcp.enforcement import EnforcementState, save_state

    daemon_result = _make_pattern_context_result(archetype)
    repo_id = daemon_result["data"]["repo"]["id"]
    data_dir = tmp_path / "chameleon_data"
    repo_data_dir = data_dir / repo_id
    repo_data_dir.mkdir(parents=True, exist_ok=True)

    state = EnforcementState(archetypes_seen={archetype})
    save_state(state, repo_data_dir, "test-session")

    return _run_preflight_with_context(
        tmp_path, archetype, daemon_result=daemon_result
    )


def _run_preflight_with_violations(tmp_path: Path, archetype: str) -> dict:
    """Run preflight for an archetype that has prior violations this session.

    Seeds enforcement state with the archetype in archetypes_with_violations
    (and archetypes_seen). Should trigger Tier 2 even though the archetype
    has been seen before.
    """
    from chameleon_mcp.enforcement import EnforcementState, save_state

    daemon_result = _make_pattern_context_result(archetype)
    repo_id = daemon_result["data"]["repo"]["id"]
    data_dir = tmp_path / "chameleon_data"
    repo_data_dir = data_dir / repo_id
    repo_data_dir.mkdir(parents=True, exist_ok=True)

    state = EnforcementState(
        archetypes_seen={archetype},
        archetypes_with_violations={archetype},
    )
    save_state(state, repo_data_dir, "test-session")

    return _run_preflight_with_context(
        tmp_path, archetype, daemon_result=daemon_result
    )


def test_tier1_pointer_for_seen_archetype(tmp_path):
    """Second edit in same archetype gets Tier 1 (~50 token pointer).

    Tier 1 emits the archetype name and confidence but NOT the full
    canonical witness body.
    """
    result = _run_preflight_second_edit(tmp_path, archetype="component")
    hook_output = result.get("hookSpecificOutput", {})
    ctx = hook_output.get("additionalContext", "")
    assert "[🦎 chameleon:" in ctx
    assert "component" in ctx
    assert "Canonical witness" not in ctx


def test_tier2_canonical_for_new_archetype(tmp_path):
    """First edit in archetype gets Tier 2 (annotated canonical).

    Tier 2 includes the canonical witness or REQUIRED annotations -
    the full context block the model needs on first encounter.
    """
    result = _run_preflight_first_edit(tmp_path, archetype="component")
    hook_output = result.get("hookSpecificOutput", {})
    ctx = hook_output.get("additionalContext", "")
    assert "REQUIRED:" in ctx or "Canonical witness" in ctx


def test_tier2_for_archetype_with_violations(tmp_path):
    """Archetype with violations gets Tier 2 even if seen before.

    When an archetype has accumulated violations in this session,
    the model needs the full canonical context again to correct course.
    """
    result = _run_preflight_with_violations(tmp_path, archetype="component")
    hook_output = result.get("hookSpecificOutput", {})
    ctx = hook_output.get("additionalContext", "")
    assert "REQUIRED:" in ctx or "Canonical witness" in ctx
