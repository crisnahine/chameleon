"""review_ledger.py's canonical finding-lifecycle ledger: record_findings's
surface bar, the pending/delivered/addressed/resurfaced/shelved transitions,
ws_root-scoped delivery, the resurface-once re-check, and the one-time
legacy ``.judge_pending.`` queue merge (spec sections 3.2, 7.1, 9).

Isolation: CHAMELEON_PLUGIN_DATA and CHAMELEON_HMAC_KEY_PATH both point
under a fresh tmp_path, mirroring test_review_ledger_record.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chameleon_mcp import review_ledger
from chameleon_mcp.core.finding import Finding

REPO = "e" * 64


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
        claim="a finding",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at="2026-07-15T00:00:00Z",
    )
    base.update(over)
    return Finding(**base)


# --- record_findings: the surface bar (spec section 7.1) --------------------


def test_medium_severity_surfaces_pending_even_unverified():
    f = _finding(severity="medium", verified="unverified", claim="medium claim")
    review_ledger.record_findings(REPO, "/repo", [f])
    rows = review_ledger.undelivered_findings(REPO, ws_roots=["/repo"])
    assert len(rows) == 1
    assert rows[0].status == "pending"


def test_high_severity_surfaces_pending_even_unverified():
    f = _finding(severity="high", verified="unverified", claim="high claim")
    review_ledger.record_findings(REPO, "/repo", [f])
    rows = review_ledger.undelivered_findings(REPO, ws_roots=["/repo"])
    assert len(rows) == 1
    assert rows[0].status == "pending"


def test_low_severity_unverified_is_shelved_and_counted():
    f = _finding(severity="low", verified="unverified", claim="low claim")
    review_ledger.record_findings(REPO, "/repo", [f])

    # Shelved findings never surface via undelivered_findings.
    assert review_ledger.undelivered_findings(REPO, ws_roots=["/repo"]) == []

    raw = review_ledger._read_findings_rows(REPO)
    assert len(raw) == 1
    (row,) = raw.values()
    assert row["status"] == "shelved"

    from chameleon_mcp.exec_log import read_check_events

    events = read_check_events(REPO, "", limit=50)["events"]
    assert any(e.get("check") == "findings_ledger" and e.get("status") == "shelved" for e in events)


def test_low_severity_confirmed_surfaces_pending():
    f = _finding(severity="low", verified="confirmed", claim="low confirmed claim")
    review_ledger.record_findings(REPO, "/repo", [f])
    rows = review_ledger.undelivered_findings(REPO, ws_roots=["/repo"])
    assert len(rows) == 1
    assert rows[0].status == "pending"


def test_record_findings_noop_on_empty_repo_id_or_empty_list():
    f = _finding()
    review_ledger.record_findings("", "/repo", [f])
    review_ledger.record_findings(REPO, "/repo", [])
    assert review_ledger.undelivered_findings(REPO, ws_roots=["/repo"]) == []


# --- lifecycle transitions ---------------------------------------------------


def test_mark_delivered_moves_pending_to_delivered_and_drops_from_undelivered():
    f = _finding(claim="deliverable finding")
    review_ledger.record_findings(REPO, "/repo", [f])

    review_ledger.mark_delivered(REPO, [f.match_key])

    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "delivered"
    assert review_ledger.undelivered_findings(REPO, ws_roots=["/repo"]) == []


def test_mark_delivered_advances_repo_keyed_cursor():
    from chameleon_mcp.core.session_state import read_delivery_cursor

    f = _finding(claim="cursor finding")
    review_ledger.record_findings(REPO, "/repo", [f])
    assert read_delivery_cursor(REPO) == ""

    review_ledger.mark_delivered(REPO, [f.match_key])

    assert read_delivery_cursor(REPO) != ""


def test_mark_delivered_unknown_key_is_a_noop():
    from chameleon_mcp.core.session_state import read_delivery_cursor

    review_ledger.mark_delivered(REPO, ["not-a-real-match-key"])
    assert read_delivery_cursor(REPO) == ""


def test_mark_addressed_from_pending_delivered_or_resurfaced():
    f = _finding(claim="addressable finding")
    review_ledger.record_findings(REPO, "/repo", [f])

    review_ledger.mark_addressed(REPO, [f.match_key])

    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "addressed"


def test_mark_resurfaced_only_moves_pending_or_delivered():
    f = _finding(claim="terminal finding", severity="high")
    review_ledger.record_findings(REPO, "/repo", [f])
    review_ledger.mark_addressed(REPO, [f.match_key])  # now a terminal status

    review_ledger.mark_resurfaced(REPO, [f.match_key])  # must be a no-op

    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "addressed"


# --- undelivered_findings: ws_root scoping (monorepo regression pin) --------


def test_undelivered_findings_scoped_by_ws_root():
    fa = _finding(claim="workspace a finding", file="src/a.ts")
    fb = _finding(claim="workspace b finding", file="src/b.ts")
    review_ledger.record_findings(REPO, "/mono/packages/api", [fa])
    review_ledger.record_findings(REPO, "/mono/packages/web", [fb])

    only_a = review_ledger.undelivered_findings(REPO, ws_roots=["/mono/packages/api"])
    assert [f.claim for f in only_a] == ["workspace a finding"]

    only_b = review_ledger.undelivered_findings(REPO, ws_roots=["/mono/packages/web"])
    assert [f.claim for f in only_b] == ["workspace b finding"]

    both = review_ledger.undelivered_findings(
        REPO, ws_roots=["/mono/packages/api", "/mono/packages/web"]
    )
    assert {f.claim for f in both} == {"workspace a finding", "workspace b finding"}


# --- compute_resurface / mark_resurfaced: the two-phase resurface port ------
#
# recheck_and_resurface split into a pure recheck (compute_resurface, never
# writes "resurfaced") and a separate terminal commit (mark_resurfaced,
# already covered above) so a multi-root Stop can defer the commit until it
# knows a root's output actually reached the user (see stop/pipeline.py and
# hook_helper.stop_backstop). These tests pin: compute never writes the
# resurfaced transition, mark_resurfaced is the only thing that does, and
# calling both together reproduces the old combined behavior exactly.


def test_compute_resurface_does_not_write_resurfaced(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const x = 1;\n", encoding="utf-8")

    from chameleon_mcp.judge import _excerpt_digest
    from chameleon_mcp.stop.verify import _excerpt_window

    excerpt = _excerpt_window(repo, "src/a.ts", 1)
    f = _finding(
        claim="unaddressed logic bug",
        severity="high",
        file="src/a.ts",
        span=(1, 1),
        excerpt_sha=_excerpt_digest(excerpt) or "",
    )
    review_ledger.record_findings(REPO, str(repo), [f])

    result = review_ledger.compute_resurface(REPO, str(repo))
    assert result.lines and any("unaddressed high-severity" in ln for ln in result.lines)
    assert any("src/a.ts:1" in ln for ln in result.lines)
    assert result.match_keys == (f.match_key,)

    # compute_resurface is PURE: the row is still pending, not resurfaced.
    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "pending"

    # A second compute (no commit happened) still reports the same candidate
    # -- it has no one-shot state of its own, only mark_resurfaced does.
    result2 = review_ledger.compute_resurface(REPO, str(repo))
    assert result2.match_keys == (f.match_key,)


def test_high_severity_unchanged_resurfaces_once_then_not_again(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const x = 1;\n", encoding="utf-8")

    from chameleon_mcp.judge import _excerpt_digest
    from chameleon_mcp.stop.verify import _excerpt_window

    excerpt = _excerpt_window(repo, "src/a.ts", 1)
    f = _finding(
        claim="unaddressed logic bug",
        severity="high",
        file="src/a.ts",
        span=(1, 1),
        excerpt_sha=_excerpt_digest(excerpt) or "",
    )
    review_ledger.record_findings(REPO, str(repo), [f])

    # compute + immediate mark_resurfaced together reproduce the old combined
    # recheck_and_resurface behavior exactly.
    result = review_ledger.compute_resurface(REPO, str(repo))
    assert result.lines and any("unaddressed high-severity" in ln for ln in result.lines)
    assert any("src/a.ts:1" in ln for ln in result.lines)
    review_ledger.mark_resurfaced(REPO, result.match_keys)

    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "resurfaced"

    # A delivery pass runs between the two rechecks, mirroring the real
    # Stop -> UserPromptSubmit -> Stop ordering. `resurfaced` must be
    # TERMINAL for ordinary delivery: this must not move the row back to
    # "delivered" and re-arm the resurface (the bug a naive wiring produced
    # -- delivery kept flipping resurfaced rows back to delivered, so the
    # next recheck saw "delivered" again and nagged forever).
    review_ledger.mark_delivered(REPO, [f.match_key])
    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "resurfaced"  # unchanged: not a valid transition

    # Already resurfaced, still unchanged, and the interleaved delivery
    # attempt was a no-op -> no second nag.
    assert review_ledger.compute_resurface(REPO, str(repo)) == review_ledger.ResurfaceResult(
        lines=[], match_keys=()
    )


def test_file_changed_since_review_is_addressed_not_resurfaced(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const x = 1;\n", encoding="utf-8")

    from chameleon_mcp.judge import _excerpt_digest
    from chameleon_mcp.stop.verify import _excerpt_window

    excerpt = _excerpt_window(repo, "src/a.ts", 1)
    f = _finding(
        claim="already fixed",
        severity="high",
        file="src/a.ts",
        span=(1, 1),
        excerpt_sha=_excerpt_digest(excerpt) or "",
    )
    review_ledger.record_findings(REPO, str(repo), [f])

    (repo / "src" / "a.ts").write_text("export const x = 2; // fixed\n", encoding="utf-8")

    # The addressed transition is NOT deferred (see compute_resurface's
    # docstring): dropping an addressed row is never a discard risk.
    assert review_ledger.compute_resurface(REPO, str(repo)).match_keys == ()
    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "addressed"


def test_file_deleted_since_review_is_addressed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    f = _finding(claim="deleted file finding", severity="high", file="src/gone.ts", span=(1, 1))
    review_ledger.record_findings(REPO, str(repo), [f])

    assert review_ledger.compute_resurface(REPO, str(repo)).match_keys == ()
    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "addressed"


def test_fileless_high_finding_resurfaces_not_silently_addressed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    f = _finding(claim="whole-diff issue", severity="high", file="", span=(0, 0))
    review_ledger.record_findings(REPO, str(repo), [f])

    result = review_ledger.compute_resurface(REPO, str(repo))
    assert result.lines and any("unaddressed high-severity" in ln for ln in result.lines)
    assert result.match_keys == (f.match_key,)

    # Still pending until committed.
    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "pending"

    review_ledger.mark_resurfaced(REPO, result.match_keys)

    # Already resurfaced -> no second nag.
    assert review_ledger.compute_resurface(REPO, str(repo)).match_keys == ()


def test_fileless_non_high_finding_is_addressed_not_left_open(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    f = _finding(claim="fileless medium", severity="medium", file="", span=(0, 0))
    review_ledger.record_findings(REPO, str(repo), [f])

    assert review_ledger.compute_resurface(REPO, str(repo)).match_keys == ()
    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "addressed"


def test_medium_severity_never_resurfaces_but_stays_open(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const x = 1;\n", encoding="utf-8")
    f = _finding(claim="medium unchanged", severity="medium", file="src/a.ts", span=(1, 1))
    review_ledger.record_findings(REPO, str(repo), [f])

    assert review_ledger.compute_resurface(REPO, str(repo)).match_keys == ()
    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "pending"  # untouched, still open


def test_shared_repo_id_monorepo_does_not_cross_resolve(tmp_path):
    ws_a = tmp_path / "mono" / "packages" / "api"
    ws_b = tmp_path / "mono" / "packages" / "web"
    (ws_a / "src").mkdir(parents=True)
    (ws_b / "src").mkdir(parents=True)
    (ws_a / "src" / "a.ts").write_text("api code\n", encoding="utf-8")

    f = _finding(claim="api bug", severity="high", file="src/a.ts", span=(1, 1))
    review_ledger.record_findings(REPO, str(ws_a), [f])

    # B's recheck must not touch A's finding (its rel_path does not exist
    # under B, and must not be wrongly read as "gone -> addressed").
    assert review_ledger.compute_resurface(REPO, str(ws_b)).match_keys == ()
    rows = review_ledger._read_findings_rows(REPO)
    assert rows[f.match_key]["status"] == "pending"

    # A's own recheck resurfaces it (file unchanged in A).
    result = review_ledger.compute_resurface(REPO, str(ws_a))
    assert result.lines and any("src/a.ts:1" in ln for ln in result.lines)
    assert result.match_keys == (f.match_key,)


# --- migrate_pending_queue: the one-time legacy merge (spec section 9) -----


def _write_legacy_pending(repo_data_dir: Path, session_id: str, findings: list[dict]) -> Path:
    from chameleon_mcp.optouts import _safe_session_marker

    marker = _safe_session_marker(session_id)
    path = repo_data_dir / f".judge_pending.{marker}.json"
    payload = {
        "turn_key": "abc123",
        "completed_ts": 1720000000.0,
        "digests": {},
        "verify": {"ran": True, "refuted": 0, "confirmed": 1, "unverified": 0},
        "findings": findings,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_migrate_pending_queue_converts_findings_to_canonical_pending_rows():
    from chameleon_mcp.profile.trust import repo_data_dir

    pending_path = _write_legacy_pending(
        repo_data_dir(REPO),
        "old-session",
        [
            {
                "file": "src/a.ts",
                "line": 3,
                "message": "legacy bug still open",
                "confidence": 0.85,
                "verify": "confirmed",
                "excerpt_sha": "cafebabe00000000",
                "suggested_fix": None,
                "evidence_cmds": None,
            }
        ],
    )

    result = review_ledger.migrate_pending_queue(REPO, "/repo")

    assert result == {"files": 1, "findings": 1}
    assert not pending_path.exists()

    rows = review_ledger.undelivered_findings(REPO, ws_roots=["/repo"])
    assert len(rows) == 1
    row = rows[0]
    assert row.claim == "legacy bug still open"
    assert row.status == "pending"  # not yet delivered -- the user never saw it
    assert row.severity == "high"
    assert row.verified == "confirmed"
    assert row.kind == "correctness"
    # id follows the pinned convention (id == match_key), not a random uuid.
    assert row.id == row.match_key


def test_migrate_pending_queue_runs_once_per_file():
    from chameleon_mcp.profile.trust import repo_data_dir

    _write_legacy_pending(
        repo_data_dir(REPO),
        "s2",
        [{"file": "src/b.ts", "message": "another legacy finding", "confidence": 0.5}],
    )

    first = review_ledger.migrate_pending_queue(REPO, "/repo")
    assert first == {"files": 1, "findings": 1}

    second = review_ledger.migrate_pending_queue(REPO, "/repo")
    assert second == {"files": 0, "findings": 0}
    # No duplicate row was written on the second (no-op) call.
    assert len(review_ledger.undelivered_findings(REPO, ws_roots=["/repo"])) == 1


def test_migrate_pending_queue_noop_when_no_legacy_files():
    assert review_ledger.migrate_pending_queue(REPO, "/repo") == {"files": 0, "findings": 0}


def test_migrate_pending_queue_deletes_unparseable_file_without_raising():
    from chameleon_mcp.optouts import _safe_session_marker
    from chameleon_mcp.profile.trust import repo_data_dir

    marker = _safe_session_marker("bad-session")
    path = repo_data_dir(REPO) / f".judge_pending.{marker}.json"
    path.write_text("not json{{{", encoding="utf-8")

    result = review_ledger.migrate_pending_queue(REPO, "/repo")

    assert result == {"files": 1, "findings": 0}
    assert not path.exists()


def test_migrate_pending_queue_low_confidence_legacy_finding_still_surfaces():
    from chameleon_mcp.profile.trust import repo_data_dir

    _write_legacy_pending(
        repo_data_dir(REPO),
        "s3",
        [{"file": "src/c.ts", "message": "low-confidence legacy hint", "confidence": 0.2}],
    )

    result = review_ledger.migrate_pending_queue(REPO, "/repo")

    assert result == {"files": 1, "findings": 1}
    # confidence < 0.7 maps to "medium" (not "low"), so the surface bar keeps
    # it pending -- only a legacy "low" severity would ever shelve, and the
    # legacy finding shape never carries one.
    assert len(review_ledger.undelivered_findings(REPO, ws_roots=["/repo"])) == 1
