"""SessionStart surfaces a degraded correctness-judge spawn from last session.

A failed reviewer spawn previously lived only in the attestation ledger:
the turn-end layer could be silently dead (auth-broken --bare, missing
binary) for weeks with no user-visible signal. When the newest session
attestation records a correctness_judge degraded_spawn check, the next
SessionStart context gains one cooldown-gated line pointing at
/chameleon-doctor.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ID = "jh_repo"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _plant_attestation(checks, session_id="s-prev"):
    from chameleon_mcp.review_ledger import record_session_attestation

    record_session_attestation(REPO_ID, {"session_id": session_id, "checks": checks})


def _degraded_checks(reason="spawn_nonzero_exit"):
    return [
        {"check": "correctness_judge", "status": "degraded_spawn", "reason": reason, "count": 3},
        {"check": "stop_relint", "status": "ran", "reason": None, "count": 4},
    ]


def _run_session_start(repo, tmp_path, session_id="s-new") -> str:
    captured: list[str] = []
    env = {
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT),
        "CHAMELEON_PLUGIN_DATA": str(tmp_path),
    }
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"session_id": session_id}))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None),
        patch("chameleon_mcp.hook_helper._wire_statusline_settings", lambda *a, **k: None),
    ):
        out.write = captured.append
        from chameleon_mcp.hook_helper import session_start

        rc = session_start()
    assert rc == 0
    return "".join(captured)


def test_degraded_spawn_last_session_surfaces_banner(repo, tmp_path):
    _plant_attestation(_degraded_checks())
    out = _run_session_start(repo, tmp_path)
    assert "turn-end reviewer failed to spawn last session" in out
    assert "spawn_nonzero_exit" in out
    assert "/chameleon-doctor" in out


def test_healthy_last_session_no_banner(repo, tmp_path):
    _plant_attestation(
        [{"check": "correctness_judge", "status": "spawned", "reason": "completed", "count": 2}]
    )
    out = _run_session_start(repo, tmp_path)
    assert "turn-end reviewer failed to spawn" not in out


def test_no_attestation_no_banner(repo, tmp_path):
    out = _run_session_start(repo, tmp_path)
    assert "turn-end reviewer failed to spawn" not in out


def test_banner_is_cooldown_gated(repo, tmp_path):
    _plant_attestation(_degraded_checks())
    first = _run_session_start(repo, tmp_path)
    assert "turn-end reviewer failed to spawn" in first
    second = _run_session_start(repo, tmp_path)
    assert "turn-end reviewer failed to spawn" not in second


def test_own_session_attestation_does_not_banner(repo, tmp_path):
    # A resumed session must not warn about its own in-progress state.
    _plant_attestation(_degraded_checks(), session_id="s-new")
    out = _run_session_start(repo, tmp_path, session_id="s-new")
    assert "turn-end reviewer failed to spawn" not in out


def test_unknown_reason_is_not_echoed(repo, tmp_path):
    # The reason rides into injected context, so anything outside the known
    # degradation kinds is replaced, never echoed (ledger text is local but
    # the injection surface stays allowlisted).
    _plant_attestation(_degraded_checks(reason="<script>alert(1)</script>"))
    out = _run_session_start(repo, tmp_path)
    assert "turn-end reviewer failed to spawn last session" in out
    assert "<script>" not in out
    assert "(unknown)" in out
