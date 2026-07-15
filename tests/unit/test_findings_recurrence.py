"""review_ledger.py's shelved-finding recurrence auto-promotion (spec §7.1):
a below-surface-bar finding carries a `recurrence` count and `session_ids`
list forward across record_findings calls sharing the same match_key, and is
promoted from `shelved` to `pending` once it has recurred often enough. This
is a churn-class pin for the counter shape a later shelved-findings miner
also reads.

Isolation mirrors test_ledger_lifecycle.py: CHAMELEON_PLUGIN_DATA and
CHAMELEON_HMAC_KEY_PATH both point under a fresh tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp import review_ledger
from chameleon_mcp.core.finding import Finding

REPO = "f" * 64


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    yield


def _finding(**over) -> Finding:
    base = dict(
        id="f1",
        kind="correctness",
        severity="medium",
        confidence=0.5,
        file="src/a.ts",
        span=(1, 1),
        claim="a recurring finding",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        verified="unverified",
        created_at="2026-07-16T00:00:00Z",
    )
    base.update(over)
    return Finding(**base)


def _events() -> list[dict]:
    from chameleon_mcp.exec_log import read_check_events

    return read_check_events(REPO, "", limit=50)["events"]


# --- (a) first sighting: shelved, recurrence 0 -------------------------------


def test_first_sighting_below_bar_is_shelved_with_zero_recurrence():
    f = _finding()
    review_ledger.record_findings(REPO, "/repo", [f], surface_bar="high")

    assert review_ledger.undelivered_findings(REPO, ws_roots=["/repo"]) == []
    raw = review_ledger._read_findings_rows(REPO)
    (row,) = raw.values()
    assert row["status"] == "shelved"
    assert row["recurrence"] == 0
    assert row["session_ids"] == []


# --- (b) second sighting (different session): promoted to pending -----------


def test_second_sighting_different_session_promotes_to_pending():
    f = _finding()
    review_ledger.record_findings(REPO, "/repo", [f], surface_bar="high", session_id="sess-1")

    f2 = _finding()  # same claim/file/kind -> same match_key
    review_ledger.record_findings(REPO, "/repo", [f2], surface_bar="high", session_id="sess-2")

    raw = review_ledger._read_findings_rows(REPO)
    (row,) = raw.values()
    assert row["status"] == "pending"
    assert row["recurrence"] == 1
    assert set(row["session_ids"]) == {"sess-1", "sess-2"}

    rows = review_ledger.undelivered_findings(REPO, ws_roots=["/repo"])
    assert len(rows) == 1
    assert rows[0].status == "pending"

    events = _events()
    assert any(
        e.get("check") == "findings_ledger" and e.get("status") == "promoted" for e in events
    )


# --- (c) CHAMELEON_SHELVED_PROMOTION=0 disables promotion --------------------


def test_promotion_disabled_keeps_recurring_finding_shelved(monkeypatch):
    monkeypatch.setenv("CHAMELEON_SHELVED_PROMOTION", "0")

    f = _finding()
    review_ledger.record_findings(REPO, "/repo", [f], surface_bar="high", session_id="sess-1")
    f2 = _finding()
    review_ledger.record_findings(REPO, "/repo", [f2], surface_bar="high", session_id="sess-2")

    raw = review_ledger._read_findings_rows(REPO)
    (row,) = raw.values()
    assert row["status"] == "shelved"
    assert row["recurrence"] == 1  # the counter still advances; only promotion is disabled

    assert review_ledger.undelivered_findings(REPO, ws_roots=["/repo"]) == []

    events = _events()
    assert not any(
        e.get("check") == "findings_ledger" and e.get("status") == "promoted" for e in events
    )


# --- (d) shelved_findings: lock-free snapshot of shelved rows ----------------


def test_shelved_findings_returns_only_shelved_rows_with_recurrence():
    below = _finding(claim="below bar finding")
    above = _finding(claim="above bar finding", severity="high")
    review_ledger.record_findings(REPO, "/repo", [below, above], surface_bar="high")

    shelved = review_ledger.shelved_findings(REPO)
    assert len(shelved) == 1
    assert shelved[0]["claim"] == "below bar finding"
    assert shelved[0]["status"] == "shelved"
    assert shelved[0]["recurrence"] == 0


def test_shelved_findings_empty_repo_id_fails_open():
    assert review_ledger.shelved_findings("") == []


def test_shelved_findings_no_data_fails_open():
    assert review_ledger.shelved_findings(REPO) == []


# --- round-trip: extra recurrence/session_ids keys don't break Finding.from_dict


def test_recurring_row_still_round_trips_through_undelivered_findings():
    f = _finding(severity="high", claim="high severity recurring finding")
    review_ledger.record_findings(REPO, "/repo", [f], session_id="sess-1")
    f2 = _finding(severity="high", claim="high severity recurring finding")
    review_ledger.record_findings(REPO, "/repo", [f2], session_id="sess-2")

    rows = review_ledger.undelivered_findings(REPO, ws_roots=["/repo"])
    assert len(rows) == 1
    assert rows[0].claim == "high severity recurring finding"
    assert rows[0].status == "pending"
