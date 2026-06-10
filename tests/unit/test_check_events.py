"""Unit tests for the per-session check-event sidecar in chameleon_mcp.exec_log.

append_check_event / read_check_events record which turn-end checks ran, were
skipped, or degraded; the Stop-path session attestation aggregates them. The
record field names are a cross-module contract (the judge paths write the
degraded and in-flight events into the same file), so the round-trip pins them
exactly.

Isolation mirrors test_exec_log.py: CHAMELEON_HMAC_KEY_PATH and TMPDIR both
point under a fresh tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chameleon_mcp.exec_log import (
    HMACKeyError,
    append_check_event,
    read_check_events,
)
from chameleon_mcp.optouts import _safe_session_marker

REPO = "repo-checks"
SID = "s-checks"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    yield


def _checks_path(tmp_path: Path) -> Path:
    return tmp_path / ".chameleon_exec_log" / REPO / f"{_safe_session_marker(SID)}.checks.jsonl"


def test_append_read_round_trip_exact_field_names(tmp_path: Path):
    append_check_event(
        REPO,
        session_id=SID,
        check="correctness_judge",
        status="degraded",
        reason="spawn_timeout",
        file_rel="src/a.ts",
        detail={"returncode": 1},
    )
    out = read_check_events(REPO, SID, limit=10)
    assert out["unverified"] == 0
    assert len(out["events"]) == 1
    record = out["events"][0]
    assert set(record) == {
        "ts",
        "session_id",
        "check",
        "status",
        "reason",
        "file_rel",
        "detail",
        "hmac",
    }
    assert record["session_id"] == SID
    assert record["check"] == "correctness_judge"
    assert record["status"] == "degraded"
    assert record["reason"] == "spawn_timeout"
    assert record["file_rel"] == "src/a.ts"
    assert record["detail"] == {"returncode": 1}
    assert isinstance(record["ts"], float)


def test_events_file_is_checks_sidecar_separate_from_exec_log(tmp_path: Path):
    from chameleon_mcp.exec_log import append_exec_log

    append_exec_log(REPO, session_id=SID, command="echo hi", exit_code=0)
    append_check_event(REPO, session_id=SID, check="stop_relint", status="ran")

    sidecar = _checks_path(tmp_path)
    assert sidecar.is_file()
    exec_log = sidecar.parent / f"{_safe_session_marker(SID)}.jsonl"
    assert exec_log.is_file()
    # The Bash exec log carries no check events and vice versa.
    assert "stop_relint" not in exec_log.read_text(encoding="utf-8")
    assert "command_sha256" not in sidecar.read_text(encoding="utf-8")


def test_tampered_line_excluded_and_counted_unverified(tmp_path: Path):
    append_check_event(REPO, session_id=SID, check="stop_relint", status="ran")
    append_check_event(REPO, session_id=SID, check="idiom_review", status="skipped")
    path = _checks_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["status"] = "skipped"  # flip a field, keep the stale signature
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out = read_check_events(REPO, SID, limit=10)
    assert out["unverified"] == 1
    assert [e["check"] for e in out["events"]] == ["idiom_review"]


def test_no_hmac_key_writes_null_hmac_and_reads_unverified(tmp_path: Path):
    from unittest.mock import patch

    import chameleon_mcp.exec_log as el

    def _no_key():
        raise HMACKeyError("no key")

    with patch.object(el, "_ensure_hmac_key", _no_key):
        # Never raises even with the key unavailable.
        append_check_event(REPO, session_id=SID, check="stop_relint", status="ran")

    raw = json.loads(_checks_path(tmp_path).read_text(encoding="utf-8").strip())
    assert raw["hmac"] is None

    out = read_check_events(REPO, SID, limit=10)
    assert out["events"] == []
    assert out["unverified"] == 1


def test_read_limit_keeps_newest_and_skips_corrupt_lines(tmp_path: Path):
    for i in range(5):
        append_check_event(
            REPO, session_id=SID, check="posttool_verify", status="skipped", reason=f"r{i}"
        )
    path = _checks_path(tmp_path)
    with open(path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")

    out = read_check_events(REPO, SID, limit=2)
    # Newest lines kept (the corrupt trailing line is skipped non-fatally).
    assert [e["reason"] for e in out["events"]] == ["r4"]
    assert out["unverified"] == 0

    full = read_check_events(REPO, SID, limit=100)
    assert [e["reason"] for e in full["events"]] == ["r0", "r1", "r2", "r3", "r4"]


def test_unknown_reason_stored_verbatim():
    append_check_event(
        REPO,
        session_id=SID,
        check="correctness_judge",
        status="routed_skip_low_risk",
        reason="some-future-reason",
    )
    out = read_check_events(REPO, SID, limit=10)
    assert out["events"][0]["reason"] == "some-future-reason"
    assert out["events"][0]["status"] == "routed_skip_low_risk"


def test_read_failopen_missing_sidecar():
    assert read_check_events("never-written", "no-session", limit=10) == {
        "events": [],
        "unverified": 0,
    }
