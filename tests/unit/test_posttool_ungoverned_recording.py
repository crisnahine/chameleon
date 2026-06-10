"""posttool_verify recording for archetype-less edits + the cooldown check event.

The Stop attestation classifies every hook-observed touched file, so an edit
that resolves to no archetype must still leave a trace: an edit observation
(archetype None), a decision row (match_quality "none") keyed by content
digest, and a FileState entry so the Stop universe includes the file. The
cooldown dedup path additionally records a posttool_verify/skipped/cooldown
check event. All of it is fail-open.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

REPO_ID = "pt_ungov_repo"
SID = "s-ungov"


def _digest16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir(exist_ok=True)
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


def _run_verify(payload: dict, *, env: dict | None = None) -> dict:
    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, env or {}, clear=False),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import posttool_verify

        rc = posttool_verify()
    assert rc == 0
    output = "".join(captured).strip()
    return json.loads(output) if output else {}


@pytest.fixture
def repo(tmp_path):
    """A trusted repo whose archetype resolution returns None."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    profile_dir = repo / ".chameleon"
    profile_dir.mkdir()
    profile_dir.joinpath("profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")

    from chameleon_mcp.profile.trust import hash_profile

    trust_rec = MagicMock()
    trust_rec.grants_root.return_value = True
    trust_rec.hash_for_root.return_value = hash_profile(profile_dir)

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", return_value=None),
        patch(
            "chameleon_mcp.tools.get_archetype",
            return_value={"data": {"archetype": None}},
        ),
    ):
        yield repo


def _decision_rows(tmp_path) -> list[tuple]:
    db = tmp_path / REPO_ID / "drift.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT rel_path, archetype, match_quality, violations_raised, outcome,"
            " session_id, content_digest FROM decision_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _edit_payload(file_path: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": file_path}, "session_id": SID}


def test_no_archetype_clean_edit_records_observation_decision_and_state(repo, tmp_path):
    content = "print('hi')\n"
    f = repo / "src" / "notes.py"
    f.write_text(content, encoding="utf-8")

    out = _run_verify(_edit_payload(str(f)))
    assert out == {}

    rows = _decision_rows(tmp_path)
    assert rows == [("src/notes.py", None, "none", 0, "clean", SID, _digest16(content))]

    db = tmp_path / REPO_ID / "drift.db"
    conn = sqlite3.connect(str(db))
    try:
        obs_rows = conn.execute(
            "SELECT rel_path, archetype, matched_canonical FROM edit_observations"
        ).fetchall()
    finally:
        conn.close()
    assert obs_rows == [(str(f), None, 0)]

    from chameleon_mcp.enforcement import load_state

    fs = load_state(tmp_path / REPO_ID, SID).files.get(str(f))
    assert fs is not None
    assert fs.last_verified_at is not None


def test_no_archetype_advised_edit_and_existing_state_not_clobbered(repo, tmp_path):
    from chameleon_mcp.enforcement import EnforcementState, FileState, load_state, save_state

    content = 'key = "AKIAIOSFODNN7EXAMPLE"\n'
    f = repo / "src" / "creds.py"
    f.write_text(content, encoding="utf-8")

    seeded = EnforcementState()
    seeded.files[str(f)] = FileState(level=2, blockable_unresolved=True, last_verified_at=1.0)
    save_state(seeded, tmp_path / REPO_ID, SID)

    out = _run_verify(_edit_payload(str(f)))
    # The advisory still surfaces.
    assert "violation" in (out.get("hookSpecificOutput") or {}).get("additionalContext", "")

    rows = _decision_rows(tmp_path)
    assert len(rows) == 1
    rel, archetype, mq, raised, outcome, sid, digest = rows[0]
    assert (rel, archetype, mq, outcome, sid) == ("src/creds.py", None, "none", "advised", SID)
    assert raised >= 1
    assert digest == _digest16(content)

    # The pre-existing FileState entry survives (not replaced by a fresh one).
    fs = load_state(tmp_path / REPO_ID, SID).files[str(f)]
    assert fs.level == 2
    assert fs.blockable_unresolved is True


def test_cooldown_dedup_appends_check_event_and_still_emits_context(repo, tmp_path):
    from chameleon_mcp.exec_log import read_check_events
    from chameleon_mcp.optouts import _safe_session_marker

    content = "export const x = 1\n"
    f = repo / "src" / "thing.ts"
    f.write_text(content, encoding="utf-8")

    marker_dir = tmp_path / REPO_ID
    marker_dir.mkdir(exist_ok=True)
    file_hash = hashlib.sha256(str(f).encode("utf-8")).hexdigest()[:16]
    marker = marker_dir / f".verify_seen.{_safe_session_marker(SID)}.{file_hash}"
    marker.write_text(_digest16(content), encoding="utf-8")

    with patch(
        "chameleon_mcp.daemon_client.call",
        side_effect=[{"data": {"archetype": "component"}}],
    ):
        out = _run_verify(_edit_payload(str(f)))

    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "already verified" in ctx

    events = read_check_events(REPO_ID, SID, limit=10)["events"]
    assert {(e["check"], e["status"], e["reason"], e["file_rel"]) for e in events} == {
        ("posttool_verify", "skipped", "cooldown", "src/thing.ts")
    }


def test_verify_env_off_records_nothing(repo, tmp_path):
    f = repo / "src" / "notes.py"
    f.write_text("print('hi')\n", encoding="utf-8")

    out = _run_verify(_edit_payload(str(f)), env={"CHAMELEON_VERIFY": "0"})
    assert out == {}
    assert not (tmp_path / REPO_ID / "drift.db").exists()
    assert _decision_rows(tmp_path) == []
    from chameleon_mcp.enforcement import load_state

    assert load_state(tmp_path / REPO_ID, SID).files == {}
    assert not list((tmp_path / "tmp").glob(".chameleon_exec_log/**/*.checks.jsonl"))


def test_all_three_archetyped_call_sites_persist_marker_digest(repo, tmp_path):
    from chameleon_mcp.optouts import _safe_session_marker

    profile_dir = repo / ".chameleon"
    profile_dir.joinpath("config.json").write_text(
        json.dumps({"enforcement": {"mode": "enforce"}}), encoding="utf-8"
    )
    # config.json changes the profile hash: re-pin the trust record's hash so
    # the enforce gate reads "trusted, not stale".
    from chameleon_mcp.profile.trust import hash_profile, trust_state_for

    trust_state_for(REPO_ID).hash_for_root.return_value = hash_profile(profile_dir)

    arch = {"data": {"archetype": "component", "match_quality": "ast", "confidence_band": "high"}}

    def marker_digest(path: str) -> str | None:
        file_hash = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        marker = tmp_path / REPO_ID / f".verify_seen.{_safe_session_marker(SID)}.{file_hash}"
        return marker.read_text(encoding="utf-8").strip() if marker.is_file() else None

    # Clean outcome.
    clean = repo / "src" / "clean.ts"
    clean.write_text("export const a = 1\n", encoding="utf-8")
    with patch(
        "chameleon_mcp.daemon_client.call",
        side_effect=[arch, {"data": {"violations": []}}],
    ):
        _run_verify(_edit_payload(str(clean)))

    # Advisory outcome (a violation that is not block-eligible).
    advised = repo / "src" / "advised.ts"
    advised.write_text("export const b = 2\n", encoding="utf-8")
    with patch(
        "chameleon_mcp.daemon_client.call",
        side_effect=[
            arch,
            {"data": {"violations": [{"rule": "naming", "severity": "warning", "message": "m"}]}},
        ],
    ):
        _run_verify(_edit_payload(str(advised)))

    # Blocked outcome: enforce mode, L2 file, an active hard-class rule.
    from chameleon_mcp.enforcement import EnforcementState, FileState, save_state

    blocked = repo / "src" / "blocked.ts"
    blocked.write_text("eval('x')\n", encoding="utf-8")
    st = EnforcementState()
    st.files[str(blocked)] = FileState(level=2, last_verified_at=1.0)
    save_state(st, tmp_path / REPO_ID, SID)
    with (
        patch(
            "chameleon_mcp.daemon_client.call",
            side_effect=[
                arch,
                {
                    "data": {
                        "violations": [
                            {"rule": "eval-call", "severity": "error", "message": "eval", "line": 1}
                        ]
                    }
                },
            ],
        ),
        patch(
            "chameleon_mcp.enforcement_calibration.active_block_rules",
            return_value={"eval-call"},
        ),
    ):
        out = _run_verify(_edit_payload(str(blocked)))
    assert out.get("decision") == "block"

    by_rel = {row[0]: row for row in _decision_rows(tmp_path)}
    assert by_rel["src/clean.ts"][4] == "clean"
    assert by_rel["src/clean.ts"][6] == _digest16("export const a = 1\n")
    assert by_rel["src/clean.ts"][6] == marker_digest(str(clean))
    assert by_rel["src/advised.ts"][4] == "advised"
    assert by_rel["src/advised.ts"][6] == _digest16("export const b = 2\n")
    assert by_rel["src/advised.ts"][6] == marker_digest(str(advised))
    assert by_rel["src/blocked.ts"][4] == "blocked"
    assert by_rel["src/blocked.ts"][6] == _digest16("eval('x')\n")


def test_recording_failures_fail_open(repo, tmp_path):
    import sqlite3 as _sql

    content = "print('hi')\n"
    f = repo / "src" / "notes.py"
    f.write_text(content, encoding="utf-8")

    with (
        patch(
            "chameleon_mcp.drift.observations.record_edit_observation",
            side_effect=_sql.OperationalError("locked"),
        ),
        patch(
            "chameleon_mcp.drift.observations.record_decision",
            side_effect=_sql.OperationalError("locked"),
        ),
        patch(
            "chameleon_mcp.exec_log.append_check_event",
            side_effect=RuntimeError("unwritable exec-log dir"),
        ),
        patch(
            "chameleon_mcp.enforcement.save_state",
            side_effect=OSError("disk full"),
        ),
    ):
        out = _run_verify(_edit_payload(str(f)))
    assert out == {}  # valid JSON, exit 0 (asserted inside _run_verify)
