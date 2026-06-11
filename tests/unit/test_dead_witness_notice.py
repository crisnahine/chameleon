"""A deleted canonical witness must be flagged, not served as a silent empty excerpt.

When the witness file that was recorded in canonicals.json no longer exists on
disk, get_canonical_excerpt must return content="" with missing=True so the hook
can render a refresh hint instead of silently degrading tier-2 injection.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.profile.trust import grant_trust
from chameleon_mcp.tools import _compute_repo_id, get_canonical_excerpt

ARCH = "service"
WITNESS = "service.ts"
SAFE_LINE = "export const a = 1;\n"


def _repo_with_witness(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
    (cham / "archetypes.json").write_text(
        json.dumps({"generation": 1, "archetypes": {ARCH: {"summary": "svc"}}})
    )
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "canonicals.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "canonicals": {ARCH: [{"witness": {"path": WITNESS, "sha_hint": "x"}}]},
            }
        )
    )
    (cham / "COMMITTED").touch()
    (repo / WITNESS).write_text(SAFE_LINE * 3)
    grant_trust(_compute_repo_id(repo), cham)
    return repo


def test_deleted_witness_yields_missing_flag(tmp_path, monkeypatch):
    """Deleting the witness file after derivation must set missing=True, not serve empty silently."""
    repo = _repo_with_witness(tmp_path, monkeypatch)

    # Sanity: witness present -> content flows.
    res_before = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res_before.get("content"), "expected content before deletion"
    assert not res_before.get("missing"), "missing flag must be absent when witness exists"

    # Remove the witness file to simulate post-derivation deletion.
    (repo / WITNESS).unlink()

    res = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res.get("content") == "", f"content must be empty, got {res.get('content')!r}"
    assert res.get("missing") is True, f"missing flag must be True, got {res.get('missing')!r}"
    # witness_path is preserved so callers know which file to recover.
    assert res.get("witness_path") == WITNESS


def test_get_pattern_context_missing_witness_flag(tmp_path, monkeypatch):
    """get_pattern_context must propagate missing=True from the canonical layer.

    When the witness recorded in canonicals.json is deleted after derivation,
    get_pattern_context must return canonical_excerpt.missing=True and content=""
    when the archetype resolves to the one whose witness was deleted.

    The archetype resolver is stubbed to return a pre-resolved result (it requires
    derived AST shapes not present in a minimal fixture) so this test focuses on
    the canonical-lookup and file-read path within get_pattern_context itself.
    """
    from unittest.mock import patch as _patch

    from chameleon_mcp.tools import get_pattern_context

    repo = _repo_with_witness(tmp_path, monkeypatch)

    # Stub the archetype resolver to return ARCH as resolved, bypassing AST matching.
    _arch_result = {
        "data": {
            "archetype": ARCH,
            "confidence_band": "high",
            "match_quality": "exact",
            "match_basis": "path_only",
            "file_exists": True,
            "alternatives": [],
            "content_signal_match": "none",
            "sub_buckets_count": 0,
        }
    }

    # Sanity: witness present -> content flows through the collapsed call too.
    file_path = str(repo / WITNESS)
    with _patch("chameleon_mcp.tools._get_archetype_with_loaded", return_value=_arch_result):
        res_before = get_pattern_context(file_path)["data"]["canonical_excerpt"]
    assert res_before.get("content"), "expected content before deletion"
    assert not res_before.get("missing"), "missing flag must be absent before deletion"

    # Remove the witness to simulate post-derivation deletion.
    (repo / WITNESS).unlink()

    with _patch("chameleon_mcp.tools._get_archetype_with_loaded", return_value=_arch_result):
        res = get_pattern_context(file_path)["data"]["canonical_excerpt"]
    assert res.get("content") == "", f"content must be empty, got {res.get('content')!r}"
    assert res.get("missing") is True, f"missing flag must be True, got {res.get('missing')!r}"
    assert res.get("witness_path") == WITNESS


def _run_preflight_with_missing_witness(tmp_path: Path, repo: Path) -> dict:
    """Drive preflight_and_advise with a get_pattern_context result that reports missing=True.

    The mock injects the missing-witness canonical_excerpt data so the render
    path at hook_helper.py:1882-1886 is exercised directly, independent of
    real file-system setup inside the hook.
    """
    repo_id = "dead_witness_test_repo"
    result = {
        "data": {
            "repo": {"id": repo_id, "trust_state": "trusted"},
            "archetype": {
                "archetype": ARCH,
                "confidence_band": "high",
                "match_quality": "ast",
                "summary": "",
                "sub_buckets_count": 0,
            },
            "canonical_excerpt": {
                "content": "",
                "witness_path": WITNESS,
                "truncated": False,
                "missing": True,
            },
            "rules": [],
            "idioms": "",
        }
    }
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / WITNESS)},
        "session_id": "s-dead-witness",
    }
    run_env = {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}
    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, run_env, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", return_value=None),
        patch("chameleon_mcp.tools.get_pattern_context", return_value=result),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import preflight_and_advise

        preflight_and_advise()
    output = "".join(captured).strip()
    return json.loads(output) if output else {}


def test_hook_render_missing_witness_notice(tmp_path, monkeypatch):
    """preflight_and_advise must render the missing-witness refresh hint in additionalContext.

    When get_pattern_context reports canonical_excerpt.missing=True, the advisory
    block must contain the phrase "missing on disk; run /chameleon-refresh".
    """
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)

    out = _run_preflight_with_missing_witness(tmp_path, repo)
    hso = out.get("hookSpecificOutput", {})
    context = hso.get("additionalContext", "")
    assert "missing on disk; run /chameleon-refresh" in context, (
        f"expected missing-witness notice in additionalContext, got: {context!r}"
    )
