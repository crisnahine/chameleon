"""One Edit must produce exactly ONE edit_observations row.

Both the PreToolUse advisor (preflight_and_advise) and the PostToolUse
verifier (posttool_verify) recorded the same edit, doubling every drift
statistic (banner thresholds, refresh routing, /chameleon-status counts).
The verify-side record is canonical: it sees the file as actually written
and also covers the no-archetype branch, so the preflight-side record is
gone. These tests pin the single-writer contract through the real hooks
against a real drift.db.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

REPO_ID = "single_row_repo"
SID = "s-single"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    from chameleon_mcp.drift import observations as obs

    for conn in list(obs._DRIFT_CONN.values()):
        try:
            conn.close()
        except Exception:
            pass
    obs._DRIFT_CONN.clear()
    yield
    for conn in list(obs._DRIFT_CONN.values()):
        try:
            conn.close()
        except Exception:
            pass
    obs._DRIFT_CONN.clear()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    profile_dir = repo / ".chameleon"
    profile_dir.mkdir()
    profile_dir.joinpath("profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    return repo


def _daemon_call(method, params=None, **kwargs):
    """One daemon double serving both hooks' calls for the same edit."""
    if method == "get_pattern_context":
        return {
            "data": {
                "repo": {"id": REPO_ID, "trust_state": "trusted", "profile_status": "ok"},
                "archetype": {
                    "archetype": "component",
                    "confidence_band": "high",
                    "match_quality": "ast",
                },
                "canonical_excerpt": {
                    "witness_path": "src/Other.ts",
                    "content": "export const o = 1\n",
                },
                "rules": [],
                "idioms": "",
            }
        }
    if method == "get_archetype":
        return {
            "data": {"archetype": "component", "confidence_band": "high", "match_quality": "ast"}
        }
    if method == "lint_file":
        return {"data": {"violations": []}}
    return None


def _run_hook(hook_name: str, payload: dict, repo, tmp_path) -> None:
    from chameleon_mcp.profile.trust import hash_profile

    trust_rec = MagicMock()
    trust_rec.grants_root.return_value = True
    trust_rec.hash_for_root.side_effect = lambda root: hash_profile(repo / ".chameleon")

    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, {}, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=_daemon_call),
        patch("chameleon_mcp.daemon.ensure_daemon_async", lambda *a, **k: None),
    ):
        out.write = captured.append
        from chameleon_mcp import hook_helper

        rc = getattr(hook_helper, hook_name)()
    assert rc == 0


def _observation_rows(tmp_path) -> list[tuple]:
    db = tmp_path / REPO_ID / "drift.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT rel_path, archetype, matched_canonical FROM edit_observations ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_one_edit_records_exactly_one_drift_row(repo, tmp_path):
    """The full per-edit hook pair (preflight then verify) writes ONE row."""
    file_path = str(repo / "src" / "Widget.ts")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("export const W = 1\n")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "session_id": SID,
    }

    _run_hook("preflight_and_advise", payload, repo, tmp_path)
    assert _observation_rows(tmp_path) == [], (
        "the PreToolUse advisor must not record drift observations"
    )

    _run_hook("posttool_verify", payload, repo, tmp_path)
    rows = _observation_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0][0] == file_path
    assert rows[0][1] == "component"


def test_two_edits_record_two_rows(repo, tmp_path):
    """The verify-side writer still records every distinct edit."""
    file_path = str(repo / "src" / "Widget.ts")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "session_id": SID,
    }
    for content in ("export const W = 1\n", "export const W = 2\n"):
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        _run_hook("preflight_and_advise", payload, repo, tmp_path)
        _run_hook("posttool_verify", payload, repo, tmp_path)

    assert len(_observation_rows(tmp_path)) == 2
