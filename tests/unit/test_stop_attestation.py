"""End-to-end tests for the Stop-path session attestation write.

Drives hook_helper.stop_backstop with the make_trusted_repo + _run_stop harness
pattern from test_stop_backstop.py, against real EnforcementState, drift.db,
check-event sidecar, and attestation ledger files under tmp_path. Asserts the
single-site write contract: one attestation per top-level Stop after the gates
ran and saved state, nothing on the unobservable paths, hook output unchanged
in every case.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState, save_state
from chameleon_mcp.review_ledger import read_session_attestations

REPO_ID = "stop_attest_repo"
SID = "s-attest"


def _digest16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Sandbox plugin data, the exec-log TMPDIR, and the HMAC key per test.

    CHAMELEON_PLUGIN_DATA must equal the patched _plugin_data_dir below so the
    attestation ledger (resolved via repo_data_dir) and the enforcement state
    (resolved via the patched helper) land in the same per-repo dir.
    """
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


@pytest.fixture
def make_trusted_repo(tmp_path):
    """Factory: a trusted repo + enforcement config + isolated data dir.

    The judge and duplication spawns are disabled by default so a Stop run
    stays deterministic; tests opt back in via config_extra.
    """
    stack = ExitStack()

    def _factory(
        *,
        mode: str = "enforce",
        stop_block_cap: int = 3,
        config_extra: dict | None = None,
        extra_profile_files: dict[str, str] | None = None,
    ):
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        enforcement = {
            "mode": mode,
            "stop_block_cap": stop_block_cap,
            "correctness_judge": False,
            "duplication_review": False,
        }
        enforcement.update(config_extra or {})
        profile_dir.joinpath("config.json").write_text(
            json.dumps({"enforcement": enforcement}), encoding="utf-8"
        )
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
        for name, body in (extra_profile_files or {}).items():
            profile_dir.joinpath(name).write_text(body, encoding="utf-8")

        data_dir = tmp_path / REPO_ID
        data_dir.mkdir(parents=True, exist_ok=True)
        (repo / "src").mkdir(parents=True, exist_ok=True)

        from chameleon_mcp.profile.trust import hash_profile

        trust_rec = MagicMock()
        trust_rec.grants_root.return_value = True
        trust_rec.hash_for_root.return_value = hash_profile(profile_dir)

        stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
        stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID))
        stack.enter_context(
            patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec)
        )
        stack.enter_context(
            patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None)
        )
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path)
        )

        return repo, data_dir, SID, profile_dir

    try:
        yield _factory
    finally:
        stack.close()


def _run_stop(payload, env, *, still_blockable: bool = True):
    """Drive stop_backstop; return (emitted JSON, exit code)."""
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
        patch(
            "chameleon_mcp.hook_helper._stop_file_still_blockable",
            return_value=still_blockable,
        ),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        rc = stop_backstop()
    s = "".join(cap).strip()
    return (json.loads(s) if s else {}, rc)


def _stop_payload(repo, *, subagent: bool = False, stop_hook_active: bool = False) -> dict:
    payload = {"session_id": SID, "cwd": str(repo), "stop_hook_active": stop_hook_active}
    if subagent:
        payload["hook_event_name"] = "SubagentStop"
    return payload


def _records():
    return read_session_attestations(REPO_ID, limit=100)["records"]


def test_block_path_writes_one_attestation_with_incremented_blocks(make_trusted_repo):
    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    file_path = str(repo / "src" / "Widget.ts")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=2, blockable_unresolved=True, last_verified_at=10.0)
    save_state(st, data_dir, sid)

    out, rc = _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "1"})
    assert rc == 0
    assert out.get("decision") == "block"

    records = _records()
    assert len(records) == 1
    rec = records[0]
    assert rec["session_id"] == sid
    # State was saved by the gate BEFORE the attestation read it.
    assert rec["stop_hook_blocks"] == 1
    assert rec["enforcement_mode"] == "enforce"
    assert rec["verified"] is True


def test_subagent_stop_writes_nothing(make_trusted_repo):
    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    out, _exit = _run_stop(_stop_payload(repo, subagent=True), env={"CHAMELEON_ENFORCE": "1"})
    assert _records() == []


def test_attestation_env_kill_switch_writes_nothing(make_trusted_repo):
    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    out, _exit = _run_stop(
        _stop_payload(repo), env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_ATTESTATION": "0"}
    )
    assert out == {}
    assert _records() == []


def test_unobservable_paths_write_nothing(make_trusted_repo):
    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    env = {"CHAMELEON_ENFORCE": "1"}

    # stop_hook_active: never re-blocks and never attests.
    out, _exit = _run_stop(_stop_payload(repo, stop_hook_active=True), env=env)
    assert out == {}
    assert _records() == []

    # No repo.
    with patch("chameleon_mcp.profile.loader.find_repo_root", return_value=None):
        out, _exit = _run_stop(_stop_payload(repo), env=env)
    assert out == {}
    assert _records() == []

    # Untrusted.
    untrusted = MagicMock()
    untrusted.grants_root.return_value = False
    with patch("chameleon_mcp.profile.trust.trust_state_for", return_value=untrusted):
        out, _exit = _run_stop(_stop_payload(repo), env=env)
    assert out == {}
    assert _records() == []

    # Stale hash.
    with patch("chameleon_mcp.profile.trust.hash_profile", return_value="DRIFTED"):
        out, _exit = _run_stop(_stop_payload(repo), env=env)
    assert out == {}
    assert _records() == []


def test_enforce_off_writes_minimal_attestation_output_unchanged(make_trusted_repo):
    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    file_path = str(repo / "src" / "Widget.ts")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=2, blockable_unresolved=True, last_verified_at=10.0)
    save_state(st, data_dir, sid)

    out, rc = _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "0"})
    assert rc == 0
    assert out == {}  # byte-identical to the pre-attestation behavior

    records = _records()
    assert len(records) == 1
    rec = records[0]
    assert rec["env"]["enforce_off"] is True
    assert {
        "check": "stop_relint",
        "status": "skipped",
        "reason": "enforce_env_off",
        "count": 1,
    } in rec["checks"]


def test_suppressed_session_writes_minimal_attestation(make_trusted_repo):
    from chameleon_mcp.optouts import _safe_session_marker

    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    marker = data_dir / f".session_disabled.{_safe_session_marker(sid)}"
    marker.write_text(f"disabled-at=1717999999.5\nsession_id={sid}\n", encoding="utf-8")
    (data_dir / ".pause_until").write_text("2999-01-01T00:00:00Z", encoding="utf-8")

    with patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value="pause"):
        out, rc = _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "1"})
    assert rc == 0
    assert out == {}

    records = _records()
    assert len(records) == 1
    sup = records[0]["suppression"]
    assert sup["reason"] == "pause"
    assert sup["session_disabled_at"] == "1717999999.5"
    assert sup["pause_until"] == "2999-01-01T00:00:00Z"
    assert {
        "check": "stop_relint",
        "status": "skipped",
        "reason": "suppressed",
        "count": 1,
    } in records[0]["checks"]


def test_ungoverned_triple_and_classification(make_trusted_repo):
    exports_index = json.dumps(
        {"schema_version": 1, "files": {"src/Widget.ts": {"names": ["Widget"], "open": False}}}
    )
    repo, data_dir, sid, profile_dir = make_trusted_repo(
        mode="enforce", extra_profile_files={"exports_index.json": exports_index}
    )
    py_file = str(repo / "src" / "notes.py")
    rb_file = str(repo / "src" / "thing.rb")
    ts_file = str(repo / "src" / "Widget.ts")
    Path(py_file).write_text("print('hi')\n", encoding="utf-8")
    Path(rb_file).write_text("class Thing; end\n", encoding="utf-8")
    Path(ts_file).write_text("export const Widget = 1\n", encoding="utf-8")

    st = EnforcementState()
    st.files[py_file] = FileState(last_verified_at=10.0)
    st.files[rb_file] = FileState(last_verified_at=11.0)
    st.files[ts_file] = FileState(last_verified_at=12.0)
    save_state(st, data_dir, sid)

    out, _exit = _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "1"})

    rec = _records()[0]
    ungoverned = {e["file"] for e in rec["ungoverned_files"]}
    governed = {e["file"] for e in rec["governed_files"]}
    # .py: no archetype AND no lint dimension AND no exports entry -> ungoverned.
    assert ungoverned == {"src/notes.py"}
    # .rb has a lint dimension; .ts is in the exports index -> governed.
    assert governed == {"src/thing.rb", "src/Widget.ts"}
    assert rec["ungoverned_files"][0]["content_digest"] == _digest16("print('hi')\n")


def test_governed_snapshot_pins_current_content_digest(make_trusted_repo):
    from chameleon_mcp.drift.observations import record_decision

    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    kept = str(repo / "src" / "kept.rb")
    edited = str(repo / "src" / "edited.rb")
    Path(kept).write_text("class Kept; end\n", encoding="utf-8")
    Path(edited).write_text("class Edited; end\n", encoding="utf-8")

    record_decision(
        REPO_ID,
        "src/kept.rb",
        archetype="model",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
        session_id=sid,
        content_digest=_digest16("class Kept; end\n"),
    )
    record_decision(
        REPO_ID,
        "src/edited.rb",
        archetype="model",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
        session_id=sid,
        content_digest=_digest16("class Edited; end\n"),
    )
    # The file changes AFTER its decision row was written.
    Path(edited).write_text("class Edited; def x; end; end\n", encoding="utf-8")

    st = EnforcementState()
    st.files[kept] = FileState(last_verified_at=10.0)
    st.files[edited] = FileState(last_verified_at=11.0)
    save_state(st, data_dir, sid)

    _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "1"})

    rec = _records()[0]
    by_file = {e["file"]: e for e in rec["governed_files"]}
    assert by_file["src/kept.rb"]["decision_log_id"] is not None
    assert by_file["src/kept.rb"]["outcome"] == "clean"
    # Mismatched digest must resolve to null, never to a later/stale row.
    assert by_file["src/edited.rb"]["decision_log_id"] is None
    assert by_file["src/edited.rb"]["outcome"] is None


def test_session_overrides_embedded_and_truncation_counted(make_trusted_repo, monkeypatch):
    from chameleon_mcp.drift.observations import record_override

    monkeypatch.setenv("CHAMELEON_ATTESTATION_MAX_OVERRIDES", "2")
    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")

    record_override(REPO_ID, "eval-call", rel_path="a.rb", session_id=sid, observed_at=100)
    record_override(REPO_ID, "eval-call", rel_path="b.rb", session_id=sid, observed_at=200)
    record_override(
        REPO_ID, "secret-detected-in-content", rel_path="c.rb", session_id=sid, observed_at=300
    )
    record_override(REPO_ID, "eval-call", rel_path="z.rb", session_id="other", observed_at=400)

    _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "1"})

    rec = _records()[0]
    assert len(rec["overrides"]) == 2
    assert rec["overrides_truncated"] == 1
    assert all(o["file"] != "z.rb" for o in rec["overrides"])
    entry = rec["overrides"][0]
    assert set(entry) == {"rule", "file", "blanket", "count"}


def test_check_events_aggregated_with_counts_and_unverified(make_trusted_repo):
    from chameleon_mcp.exec_log import append_check_event
    from chameleon_mcp.optouts import _safe_session_marker

    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    # Synthetic degraded judge events, standing in for the async judge path.
    for _ in range(2):
        append_check_event(
            REPO_ID,
            session_id=sid,
            check="correctness_judge",
            status="degraded",
            reason="spawn_timeout",
        )
    append_check_event(REPO_ID, session_id=sid, check="idiom_review", status="skipped")

    # Tamper the idiom_review line: it must be excluded and counted.
    sidecar = (
        Path(os.environ["TMPDIR"])
        / ".chameleon_exec_log"
        / REPO_ID
        / f"{_safe_session_marker(sid)}.checks.jsonl"
    )
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[-1])
    tampered["status"] = "ran"
    lines[-1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    sidecar.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "1"})

    rec = _records()[0]
    assert {
        "check": "correctness_judge",
        "status": "degraded",
        "reason": "spawn_timeout",
        "count": 2,
    } in rec["checks"]
    assert rec["check_events_unverified"] == 1
    assert not any(c["check"] == "idiom_review" for c in rec["checks"])


def test_max_files_truncation_keeps_newest_verified(make_trusted_repo, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ATTESTATION_MAX_FILES", "2")
    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    st = EnforcementState()
    for i in range(4):
        p = str(repo / "src" / f"f{i}.py")
        Path(p).write_text(f"print({i})\n", encoding="utf-8")
        st.files[p] = FileState(last_verified_at=float(i))
    save_state(st, data_dir, sid)

    _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "1"})

    rec = _records()[0]
    listed = {e["file"] for e in rec["ungoverned_files"]} | {
        e["file"] for e in rec["governed_files"]
    }
    # Newest-verified kept; the overflow is counted, not silently dropped.
    assert listed == {"src/f3.py", "src/f2.py"}
    assert rec["governed_truncated"] + rec["ungoverned_truncated"] == 2


def test_attestation_failure_never_changes_hook_output(make_trusted_repo):
    repo, data_dir, sid, profile_dir = make_trusted_repo(mode="enforce")
    file_path = str(repo / "src" / "Widget.ts")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=2, blockable_unresolved=True, last_verified_at=10.0)
    save_state(st, data_dir, sid)

    with patch(
        "chameleon_mcp.review_ledger.record_session_attestation",
        side_effect=RuntimeError("ledger exploded"),
    ):
        out, rc = _run_stop(_stop_payload(repo), env={"CHAMELEON_ENFORCE": "1"})
    assert rc == 0
    assert out.get("decision") == "block"  # outcome unchanged, valid JSON emitted
    assert _records() == []
