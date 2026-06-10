"""Unit tests for the session-attestation ledger in chameleon_mcp.review_ledger.

record_session_attestation / read_session_attestations persist the Stop-path
session attestation into a SEPARATE per-repo NDJSON (session_attestations.ndjson)
using the same signing and trim machinery as the PR-review ledger, so existing
review-history consumers never see attestation rows.

Isolation copied from test_review_ledger_record.py: CHAMELEON_PLUGIN_DATA and
CHAMELEON_HMAC_KEY_PATH both point under a fresh tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.review_ledger import (
    _attestation_path,
    _ledger_path,
    build_review_ledger_panel,
    read_review_history,
    read_session_attestations,
    record_review,
    record_session_attestation,
)

REPO = "f" * 64


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    yield


def _payload(session_id: str = "s1", **overrides) -> dict:
    base = {
        "session_id": session_id,
        "engine_version": "2.10.0",
        "profile_sha256": "deadbeef",
        "generation": 3,
        "schema_version": 2,
        "trust_state": "trusted",
        "enforcement_mode": "shadow",
        "env": {"verify_off": False, "enforce_off": False},
        "suppression": {"reason": None, "session_disabled_at": None, "pause_until": None},
        "checks": [{"check": "stop_relint", "status": "ran", "reason": None, "count": 1}],
        "check_events_unverified": 0,
        "governed_files": [
            {
                "file": "src/a.ts",
                "content_digest": "abcd1234abcd1234",
                "decision_log_id": 7,
                "archetype": "component",
                "match_quality": "ast",
                "outcome": "clean",
                "observed_at": 1000,
            }
        ],
        "governed_truncated": 0,
        "ungoverned_files": [{"file": "notes.py", "content_digest": "eeee1234eeee1234"}],
        "ungoverned_truncated": 0,
        "overrides": [{"rule": "eval-call", "file": "src/a.ts", "blanket": False, "count": 2}],
        "overrides_truncated": 0,
        "stop_hook_blocks": 1,
        "duplication_spawns": 0,
    }
    base.update(overrides)
    return base


def test_record_stamps_and_signs_read_verifies_and_filters():
    out = record_session_attestation(REPO, _payload("s1"))
    assert out["appended"] is True
    rec = out["record"]
    assert rec["record_type"] == "session_attestation"
    assert rec["attestation_schema"] == 1
    assert rec["ts"]
    assert rec["hmac"]

    record_session_attestation(REPO, _payload("s2", stop_hook_blocks=0))

    history = read_session_attestations(REPO)
    assert history["total"] == 2
    assert history["unverified"] == 0
    # Newest first.
    assert [r["session_id"] for r in history["records"]] == ["s2", "s1"]
    assert all(r["verified"] is True for r in history["records"])

    only_s1 = read_session_attestations(REPO, session_id="s1")
    assert [r["session_id"] for r in only_s1["records"]] == ["s1"]
    assert only_s1["total"] == 1
    s1 = only_s1["records"][0]
    assert s1["governed_files"][0]["decision_log_id"] == 7
    assert s1["overrides"][0]["count"] == 2


def test_identical_payload_deduped_changed_payload_appends():
    first = record_session_attestation(REPO, _payload("s1"))
    assert first["appended"] is True
    second = record_session_attestation(REPO, _payload("s1"))
    assert second["appended"] is False
    assert second["digest"] == first["digest"]
    assert _attestation_path(REPO).read_text(encoding="utf-8").count("\n") == 1

    changed = record_session_attestation(REPO, _payload("s1", stop_hook_blocks=2))
    assert changed["appended"] is True
    history = read_session_attestations(REPO, session_id="s1")
    # The NEWEST row per session is authoritative.
    assert history["records"][0]["stop_hook_blocks"] == 2
    assert history["total"] == 2


def test_trim_keeps_most_recent_with_env_override(monkeypatch):
    monkeypatch.setenv("CHAMELEON_ATTESTATION_LEDGER_MAX_RECORDS", "3")
    for i in range(6):
        record_session_attestation(REPO, _payload("s1", stop_hook_blocks=i))
    history = read_session_attestations(REPO, limit=100)
    assert history["total"] == 3
    assert [r["stop_hook_blocks"] for r in history["records"]] == [5, 4, 3]


def test_review_ledger_consumers_unaffected_by_attestations():
    record_session_attestation(REPO, _payload("s1"))
    record_review(REPO, commit_sha="abc123", verdict="APPROVE")

    history = read_review_history(REPO, limit=100)
    assert history["total"] == 1
    assert history["records"][0]["verdict"] == "APPROVE"

    panel = build_review_ledger_panel(REPO)
    assert panel is not None and panel["total"] == 1

    # Separate files: attestations never mix into review_ledger.ndjson.
    assert _attestation_path(REPO) != _ledger_path(REPO)
    attest = read_session_attestations(REPO)
    assert attest["total"] == 1
    assert attest["records"][0]["record_type"] == "session_attestation"


def test_malformed_payload_values_coerced_without_breaking_signed_shape():
    garbage = {
        "session_id": 42,  # non-str -> None
        "generation": "seven",  # non-int -> None
        "env": "not-a-dict",
        "suppression": ["nope"],
        "checks": [
            {"check": "stop_relint", "status": "ran", "count": "many"},  # count coerced
            {"status": "ran"},  # no check name -> dropped
            "garbage-entry",
        ],
        "governed_files": [{"file": "a.ts", "decision_log_id": "x", "observed_at": None}],
        "ungoverned_files": [{"content_digest": "zz"}],  # no file -> dropped
        "overrides": [{"rule": "eval-call", "blanket": 1, "count": 2.9}],
        "stop_hook_blocks": -5,  # negative -> 0
        "check_events_unverified": "lots",  # non-int -> 0
    }
    out = record_session_attestation(REPO, garbage)
    rec = out["record"]
    assert rec["session_id"] is None
    assert rec["generation"] is None
    assert rec["env"] == {"verify_off": False, "enforce_off": False}
    assert rec["suppression"] == {
        "reason": None,
        "session_disabled_at": None,
        "pause_until": None,
    }
    assert rec["checks"] == [{"check": "stop_relint", "status": "ran", "reason": None, "count": 0}]
    assert rec["governed_files"] == [
        {
            "file": "a.ts",
            "content_digest": None,
            "decision_log_id": None,
            "archetype": None,
            "match_quality": None,
            "outcome": None,
            "observed_at": None,
        }
    ]
    assert rec["ungoverned_files"] == []
    assert rec["overrides"] == [{"rule": "eval-call", "file": None, "blanket": True, "count": 2}]
    assert rec["stop_hook_blocks"] == 0
    assert rec["check_events_unverified"] == 0
    # The coerced record still signs and verifies.
    history = read_session_attestations(REPO)
    assert history["records"][0]["verified"] is True


def test_doctrine_docstring_ships():
    assert "raise-only" in (record_session_attestation.__doc__ or "")


def test_check_count_growth_dedupes_new_check_status_appends():
    # The Stop relint gate records one "ran" event per Stop, so counts grow on
    # idle sessions; count growth alone must dedupe or every idle Stop appends.
    first = record_session_attestation(REPO, _payload("s1"))
    assert first["appended"] is True

    bumped = record_session_attestation(
        REPO,
        _payload(
            "s1",
            checks=[{"check": "stop_relint", "status": "ran", "reason": None, "count": 7}],
        ),
    )
    assert bumped["appended"] is False
    assert bumped["digest"] == first["digest"]

    # A NEW (check, status) combination is substance: it must append.
    degraded = record_session_attestation(
        REPO,
        _payload(
            "s1",
            checks=[
                {"check": "stop_relint", "status": "ran", "reason": None, "count": 7},
                {
                    "check": "correctness_judge",
                    "status": "degraded_spawn",
                    "reason": "spawn_timeout",
                    "count": 1,
                },
            ],
        ),
    )
    assert degraded["appended"] is True
