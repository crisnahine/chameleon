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

    return _run_preflight_with_context(tmp_path, archetype, daemon_result=daemon_result)


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

    return _run_preflight_with_context(tmp_path, archetype, daemon_result=daemon_result)


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


def test_tier1_pointer_surfaces_stale_trust_banner(tmp_path):
    """sc01 regression: a repeat edit to an already-seen archetype takes the
    Tier-1 short-pointer path, which must still surface the stale-trust
    banner when trust_state resolves stale -- the CHAMELEON_TRUST_REVALIDATE=1
    re-check detects staleness for this call exactly as reliably as it does on
    the Tier-2 (first-in-archetype) path, so the banner must not be dropped
    just because this edit took the short-pointer branch.
    """
    daemon_result = _make_pattern_context_result("component", trust_state="stale")
    # Mirrors _run_preflight_second_edit, but with a stale (not trusted)
    # daemon_result so this test controls trust_state directly.
    from chameleon_mcp.enforcement import EnforcementState, save_state

    repo_id = daemon_result["data"]["repo"]["id"]
    data_dir = tmp_path / "chameleon_data"
    repo_data_dir = data_dir / repo_id
    repo_data_dir.mkdir(parents=True, exist_ok=True)
    state = EnforcementState(archetypes_seen={"component"})
    save_state(state, repo_data_dir, "test-session")

    result = _run_preflight_with_context(tmp_path, "component", daemon_result=daemon_result)
    hook_output = result.get("hookSpecificOutput", {})
    ctx = hook_output.get("additionalContext", "")
    # Confirms this is genuinely the Tier-1 path (no witness body).
    assert "Canonical witness" not in ctx
    assert "Trust is stale" in ctx


# --- Tier-2 idiom-slug dual-write into SessionDoc.idioms_shown_slugs ------


def _seed_idiom(profile_dir: Path, *, slug: str, title: str) -> None:
    from chameleon_mcp.core.idiom_store import IdiomRecord, upsert_idiom

    upsert_idiom(
        profile_dir,
        IdiomRecord(
            slug=slug,
            title=title,
            rationale=f"{title} rationale.",
            languages=["typescript"],
            archetypes=[],
            paths=[],
            status="active",
            added_date="2026-07-15",
            rank=1,
        ),
    )


def _idiom_block(title: str) -> str:
    return f"### {title}\nLanguage: typescript\nStatus: active\nAlways do the {title} thing.\n"


def test_tier2_records_rendered_idiom_slug_into_session_doc(tmp_path, monkeypatch):
    """The Tier-2 recording site resolves each idiom title its block actually
    rendered to the store's slug and records it on
    ``SessionDoc.idioms_shown_slugs``, the structured per-session "already
    shown" signal the idiom lens dedups against."""
    data_dir = tmp_path / "chameleon_data"
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    profile_dir = tmp_path / ".chameleon"
    profile_dir.mkdir(parents=True, exist_ok=True)
    _seed_idiom(profile_dir, slug="wrap-fetches", title="Wrap Fetches")

    daemon_result = _make_pattern_context_result("component")
    daemon_result["data"]["idioms"] = _idiom_block("Wrap Fetches")
    repo_id = daemon_result["data"]["repo"]["id"]

    result = _run_preflight_with_context(tmp_path, "component", daemon_result=daemon_result)
    hook_output = result.get("hookSpecificOutput", {})
    ctx = hook_output.get("additionalContext", "")
    assert "Wrap Fetches" in ctx  # sanity: this really was the Tier-2 render

    from chameleon_mcp.core.session_state import read_session_doc

    doc = read_session_doc(repo_id, "test-session")
    assert doc.idioms_shown_slugs == {"wrap-fetches"}


def test_tier2_multiple_renders_accumulate_idiom_slugs(tmp_path, monkeypatch):
    """A later Tier-2 render in the same session ADDS to the recorded slug
    set (union), never replaces it -- matching ``update_session_doc``'s own
    load-mutate-save merge discipline."""
    data_dir = tmp_path / "chameleon_data"
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    profile_dir = tmp_path / ".chameleon"
    profile_dir.mkdir(parents=True, exist_ok=True)
    _seed_idiom(profile_dir, slug="wrap-fetches", title="Wrap Fetches")
    _seed_idiom(profile_dir, slug="log-via-logger", title="Log Via Logger")

    daemon_result_1 = _make_pattern_context_result("component")
    daemon_result_1["data"]["idioms"] = _idiom_block("Wrap Fetches")
    repo_id = daemon_result_1["data"]["repo"]["id"]
    _run_preflight_with_context(tmp_path, "component", daemon_result=daemon_result_1)

    daemon_result_2 = _make_pattern_context_result("hook")
    daemon_result_2["data"]["idioms"] = _idiom_block("Log Via Logger")
    _run_preflight_with_context(tmp_path, "hook", daemon_result=daemon_result_2)

    from chameleon_mcp.core.session_state import read_session_doc

    doc = read_session_doc(repo_id, "test-session")
    assert doc.idioms_shown_slugs == {"wrap-fetches", "log-via-logger"}


def test_tier2_unresolvable_idiom_title_skips_slug_without_crash(tmp_path, monkeypatch):
    """An idiom title rendered into the Tier-2 block that matches no store
    record (renamed, deleted, or simply a bug elsewhere) must be skipped --
    never recorded as a fabricated slug, and never a crash."""
    data_dir = tmp_path / "chameleon_data"
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    profile_dir = tmp_path / ".chameleon"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # No idiom seeded in the store at all: "Ghost Idiom" resolves to nothing.

    daemon_result = _make_pattern_context_result("component")
    daemon_result["data"]["idioms"] = _idiom_block("Ghost Idiom")
    repo_id = daemon_result["data"]["repo"]["id"]

    result = _run_preflight_with_context(tmp_path, "component", daemon_result=daemon_result)
    hook_output = result.get("hookSpecificOutput", {})
    assert "hookSpecificOutput" in result  # the hook completed normally
    ctx = hook_output.get("additionalContext", "")
    assert "Ghost Idiom" in ctx  # sanity: it really rendered into Tier-2

    from chameleon_mcp.core.session_state import read_session_doc

    doc = read_session_doc(repo_id, "test-session")
    assert doc.idioms_shown_slugs == set()
