"""stop/judge_wait.py: the CHAMELEON_JUDGE_WAIT poll-and-render path
(spec section 3.1).

NOT wired into the live Stop pipeline (Task 7 does that wiring) -- every
test here drives a seeded session doc / heartbeat file / ledger row
directly, never a real launched job (the CONFTEST GUARD: this module never
spawns, so nothing here needs @pytest.mark.real_judge_spawn).

Isolation mirrors test_stop_job.py: CHAMELEON_PLUGIN_DATA and
CHAMELEON_HMAC_KEY_PATH both point under a fresh tmp_path.
"""

from __future__ import annotations

import time

import pytest

from chameleon_mcp import review_ledger
from chameleon_mcp.core.budget import TurnBudget
from chameleon_mcp.core.finding import Finding
from chameleon_mcp.stop import assemble, job, judge_wait, scheduler

REPO_ID = "jw-repo"
SID = "jw-sess"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_JUDGE_WAIT", raising=False)
    yield


def _finding(**over) -> Finding:
    base = dict(
        id="f1",
        kind="correctness",
        severity="high",
        confidence=0.9,
        file="src/a.ts",
        span=(3, 3),
        claim="judge-wait finding",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at="2026-07-15T00:00:00Z",
        verified="confirmed",
    )
    base.update(over)
    return Finding(**base)


def _budget(seconds: float) -> TurnBudget:
    return TurnBudget.for_hook(total_seconds=seconds, token_ceiling=20_000)


# --- judge_wait_enabled ------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CHAMELEON_JUDGE_WAIT", raising=False)
    assert judge_wait.judge_wait_enabled() is False


def test_enabled_only_on_exact_value_1(monkeypatch):
    monkeypatch.setenv("CHAMELEON_JUDGE_WAIT", "true")
    assert judge_wait.judge_wait_enabled() is False
    monkeypatch.setenv("CHAMELEON_JUDGE_WAIT", "1")
    assert judge_wait.judge_wait_enabled() is True


# --- wait_and_render: the no-op-unless-enabled contract ----------------------


def test_wait_and_render_is_a_noop_without_the_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAMELEON_JUDGE_WAIT", raising=False)
    heartbeat = tmp_path / "hb"
    heartbeat.write_text("x", encoding="utf-8")
    # A budget that would time out instantly if polling ever started -- proves
    # the no-op path never even calls wait_for_job.
    result = judge_wait.wait_and_render(
        repo_id=REPO_ID,
        repo_data=tmp_path / "data",
        ws_root=tmp_path / "repo",
        session_id=SID,
        heartbeat_path=heartbeat,
        budget=_budget(-1.0),
    )
    assert result == (None, ())


# --- _job_done ---------------------------------------------------------------


def test_job_done_true_when_session_doc_slot_cleared(tmp_path):
    heartbeat = tmp_path / "hb"
    heartbeat.write_text("x", encoding="utf-8")
    # No claim was ever made for this heartbeat -- job_inflight defaults to "",
    # which already differs from str(heartbeat).
    assert judge_wait._job_done(REPO_ID, SID, heartbeat) is True


def test_job_done_false_while_slot_is_live_and_heartbeat_fresh(tmp_path):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    assert judge_wait._job_done(REPO_ID, SID, heartbeat) is False


def test_job_done_true_once_slot_cleared(tmp_path):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    scheduler.clear_job_slot(REPO_ID, SID)
    assert judge_wait._job_done(REPO_ID, SID, heartbeat) is True


def test_job_done_true_when_heartbeat_gone_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_JOB_HEARTBEAT_STALE_SECONDS", "1")
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    old = time.time() - 1000
    import os

    os.utime(heartbeat, (old, old))
    assert judge_wait._job_done(REPO_ID, SID, heartbeat) is True


def test_job_done_true_when_heartbeat_file_missing(tmp_path):
    heartbeat = tmp_path / "never-existed"
    assert judge_wait._job_done(REPO_ID, SID, heartbeat) is True


# --- wait_for_job: budget boundary, no real sleeping -------------------------


def test_wait_for_job_returns_true_immediately_when_already_done(tmp_path):
    heartbeat = tmp_path / "hb"
    heartbeat.write_text("x", encoding="utf-8")
    budget = _budget(30.0)
    start = time.monotonic()
    assert judge_wait.wait_for_job(REPO_ID, SID, heartbeat, budget) is True
    assert time.monotonic() - start < 0.2  # no sleep needed


def test_wait_for_job_returns_false_when_budget_already_expired(tmp_path):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None  # job stays "live" the whole test
    budget = _budget(-1.0)  # already expired
    start = time.monotonic()
    assert judge_wait.wait_for_job(REPO_ID, SID, heartbeat, budget) is False
    assert time.monotonic() - start < 0.2  # never slept past a dead budget


# --- wait_and_render: end-to-end over a seeded ledger/session-doc -----------


def test_wait_and_render_emits_seeded_finding_once_job_done(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_JUDGE_WAIT", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    f = _finding()
    review_ledger.record_findings(REPO_ID, str(repo), [f])
    heartbeat = tmp_path / "hb"
    heartbeat.write_text("x", encoding="utf-8")  # job_inflight never claimed -> already "done"

    text, keys = judge_wait.wait_and_render(
        repo_id=REPO_ID,
        repo_data=tmp_path / REPO_ID,
        ws_root=repo,
        session_id=SID,
        heartbeat_path=heartbeat,
        budget=_budget(5.0),
    )

    assert text is not None
    assert "judge-wait finding" in text
    # Delivery is now DEFERRED (the Stop caller commits it only if the block
    # actually packs into the ranked emission): wait_and_render reports the
    # keys but leaves the finding pending, and a caller-side mark_delivered on
    # those keys drains it -- mirroring compute_resurface / mark_resurfaced.
    assert keys == (f.match_key,)
    assert len(review_ledger.undelivered_findings(REPO_ID, ws_roots=[str(repo)])) == 1
    review_ledger.mark_delivered(REPO_ID, keys)
    assert review_ledger.undelivered_findings(REPO_ID, ws_roots=[str(repo)]) == []


def test_wait_and_render_times_out_cleanly_leaves_finding_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_JUDGE_WAIT", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    review_ledger.record_findings(REPO_ID, str(repo), [_finding()])
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None  # job stays live -> never "done"

    result = judge_wait.wait_and_render(
        repo_id=REPO_ID,
        repo_data=tmp_path / REPO_ID,
        ws_root=repo,
        session_id=SID,
        heartbeat_path=heartbeat,
        budget=_budget(-1.0),  # already expired: no real sleeping in this test
    )

    assert result == (None, ())
    assert len(review_ledger.undelivered_findings(REPO_ID, ws_roots=[str(repo)])) == 1


def test_wait_and_render_prefers_the_jobs_own_cached_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_JUDGE_WAIT", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    f = _finding(claim="cached wait finding")
    review_ledger.record_findings(REPO_ID, str(repo), [f])
    repo_data = tmp_path / REPO_ID
    assemble.write_delivery_payload(repo_data, SID, "JOB-RENDERED PAYLOAD", (f.match_key,))
    heartbeat = tmp_path / "hb"
    heartbeat.write_text("x", encoding="utf-8")

    text, keys = judge_wait.wait_and_render(
        repo_id=REPO_ID,
        repo_data=repo_data,
        ws_root=repo,
        session_id=SID,
        heartbeat_path=heartbeat,
        budget=_budget(5.0),
    )

    assert text is not None
    assert "JOB-RENDERED PAYLOAD" in text
    assert assemble.read_delivery_payload(repo_data, SID) is None  # consumed
    # Delivery deferred: the payload's finding is reported but stays pending
    # until the Stop caller commits it (the block may still be ceiling-dropped).
    assert keys == (f.match_key,)
    assert len(review_ledger.undelivered_findings(REPO_ID, ws_roots=[str(repo)])) == 1


# --- end-to-end through the real job runner (no waiting needed) -------------


def test_real_job_then_judge_wait_finds_its_payload(tmp_path, monkeypatch):
    """Drives an actual `job.main(...)` run (lenses stubbed, no real spawn --
    the same harness test_stop_job.py uses), then confirms judge_wait picks up
    exactly what that job persisted and rendered. Proves the two modules'
    contracts (job.py's payload write, judge_wait's payload-preferring read)
    actually interoperate, not just each one's own isolated behavior."""
    monkeypatch.setenv("CHAMELEON_JUDGE_WAIT", "1")
    from chameleon_mcp.stop import lenses
    from chameleon_mcp.stop.lenses import LensResult
    from chameleon_mcp.stop.scheduler import JobRequest

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    src = repo / "src" / "widget.ts"
    src.write_text("export const x = 1;\n", encoding="utf-8")

    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    request = JobRequest(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        files=(str(src),),
        intent_tokens=(),
        lens_names=("correctness",),
        model="sonnet",
        heartbeat_path=heartbeat,
    )
    request_path = tmp_path / "request.json"
    import json as _json

    request_path.write_text(_json.dumps(request.to_dict()), encoding="utf-8")

    finding = _finding(claim="real job finding")
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[finding])
    )
    from chameleon_mcp import refuter

    monkeypatch.setattr(refuter, "run_batch", lambda *a, **k: [{"id": "0", "verdict": "confirmed"}])

    rc = job.main([str(request_path)])
    assert rc == 0

    text, keys = judge_wait.wait_and_render(
        repo_id=REPO_ID,
        repo_data=tmp_path / REPO_ID,
        ws_root=repo,
        session_id=SID,
        heartbeat_path=heartbeat,
        budget=_budget(5.0),
    )
    assert text is not None
    assert "real job finding" in text
    assert keys  # the finding it rendered is reported for the caller to commit
