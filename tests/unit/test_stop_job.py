"""stop/job.py: the detached job runner, driven in-process with a written
request file -- never a real subprocess (the brief's own testing ladder:
``job.main`` is called directly, not launched). Lenses are always
monkeypatched stubs (``chameleon_mcp.stop.lenses.resolve_runner``); the real
lens/refuter internals are covered by test_stop_lens_*.py and
test_stop_verify_stage.py. Isolation mirrors test_stop_scheduler.py: an
isolated ``CHAMELEON_PLUGIN_DATA`` dir and HMAC key file.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from chameleon_mcp import refuter, review_ledger
from chameleon_mcp.core.finding import Finding
from chameleon_mcp.core.session_state import read_session_doc
from chameleon_mcp.stop import assemble, job, lenses, scheduler
from chameleon_mcp.stop.lenses import LensResult

REPO_ID = "job-test-repo"
SID = "job-sess-1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    yield


def _write_source(
    repo: Path, rel: str = "src/widget.ts", body: str = "export const x = 1;\n"
) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _stub_finding(**over) -> Finding:
    base = dict(
        id="f1",
        kind="correctness",
        severity="high",
        confidence=0.9,
        file="src/widget.ts",
        span=(1, 1),
        claim="stub finding",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at="2026-07-15T00:00:00Z",
    )
    base.update(over)
    return Finding(**base)


def _write_request(
    tmp_path: Path, heartbeat: Path, *, lens_names=("correctness",)
) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    src = _write_source(repo)
    request = scheduler.JobRequest(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        files=(str(src),),
        intent_tokens=(),
        lens_names=lens_names,
        model="sonnet",
        heartbeat_path=heartbeat,
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request.to_dict()), encoding="utf-8")
    return request_path, repo


def _events() -> list[dict]:
    from chameleon_mcp.exec_log import read_check_events

    out = read_check_events(REPO_ID, SID, limit=200)
    return [e for e in out["events"] if e.get("check") == "review_job"]


def _persisted_findings(repo: Path) -> list[Finding]:
    return review_ledger.undelivered_findings(REPO_ID, ws_roots=[str(repo)])


def _shadow_log_rows() -> list[dict]:
    from chameleon_mcp.metrics import _metrics_path

    path = _metrics_path()
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r.get("hook") == "stop-correctness-judge"]


# --- argv / request-file edge cases -----------------------------------------


def test_main_returns_zero_when_argv_empty():
    assert job.main([]) == 0


def test_main_returns_zero_when_request_file_missing(tmp_path):
    assert job.main([str(tmp_path / "nope.json")]) == 0


def test_main_returns_zero_on_corrupt_request_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json{{{", encoding="utf-8")
    assert job.main([str(bad)]) == 0


def test_request_file_is_unlinked_after_load(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, _repo = _write_request(tmp_path, heartbeat)
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[])
    )

    job.main([str(request_path)])

    assert not request_path.exists()


# --- the full happy path: lenses -> verify -> persist -> clear slot --------


def test_main_runs_lenses_verify_persists_and_clears_slot(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    request_path, repo = _write_request(tmp_path, heartbeat)

    finding = _stub_finding()
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[finding])
    )
    monkeypatch.setattr(refuter, "run_batch", lambda *a, **k: [{"id": "0", "verdict": "confirmed"}])

    rc = job.main([str(request_path)])

    assert rc == 0

    doc = read_session_doc(REPO_ID, SID)
    assert doc.job_inflight == ""
    assert doc.job_started_at == 0.0
    # The spend from try_acquire_job_slot is NOT refunded on a completed run
    # (see scheduler.clear_job_slot's docstring) -- only a failed LAUNCH
    # rolls back via _release_job_slot.
    assert doc.review_spawns == 1

    persisted = _persisted_findings(repo)
    assert len(persisted) == 1
    assert persisted[0].claim == "stub finding"
    assert persisted[0].verified == "confirmed"


def test_main_persists_empty_findings_when_lenses_find_nothing(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, repo = _write_request(tmp_path, heartbeat)
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[])
    )

    rc = job.main([str(request_path)])

    assert rc == 0
    assert _persisted_findings(repo) == []


# --- fail-open: a lens exception never crashes the job ----------------------


def test_lens_exception_fails_open_job_still_returns_zero_and_emits_event(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, repo = _write_request(tmp_path, heartbeat)

    def _raising_runner(name):
        def _raise(*a, **k):
            raise RuntimeError("lens exploded")

        return _raise

    monkeypatch.setattr(lenses, "resolve_runner", _raising_runner)

    rc = job.main([str(request_path)])

    assert rc == 0
    events = _events()
    assert any(e.get("status") == "lens_error" for e in events)
    doc = read_session_doc(REPO_ID, SID)
    assert doc.job_inflight == ""  # the slot is still cleared despite the failure

    assert _persisted_findings(repo) == []


def test_unresolvable_lens_name_fails_open(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, _repo = _write_request(tmp_path, heartbeat, lens_names=("not-a-real-lens",))

    rc = job.main([str(request_path)])

    assert rc == 0
    events = _events()
    assert any(e.get("status") == "lens_error" for e in events)


def test_lenses_run_concurrently_not_sequentially(tmp_path, monkeypatch):
    # Two lenses that each block on a shared Barrier(2) BOTH complete only if
    # the job runs them concurrently: the barrier releases only once both
    # threads reach it. Sequential execution would leave the first lens
    # waiting on a barrier the second never reaches, the wait would time out
    # into a BrokenBarrierError, _run_lens_one would swallow it, and neither
    # finding would surface. Pins the ThreadPoolExecutor concurrency in
    # stop/job.py::_run_lenses that the deleted lens_runner.run_lenses used to
    # provide (was test_qa30_remediation::test_run_lenses_executes_lenses_concurrently).
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, repo = _write_request(
        tmp_path, heartbeat, lens_names=("correctness", "duplication")
    )

    barrier = threading.Barrier(2, timeout=5)

    def _barrier_runner(name):
        span = (1, 1) if name == "correctness" else (2, 2)

        def _run(*a, **k):
            barrier.wait()  # returns only once the other lens is also in-flight
            return LensResult(findings=[_stub_finding(id=name, claim=f"{name} finding", span=span)])

        return _run

    monkeypatch.setattr(lenses, "resolve_runner", _barrier_runner)
    # Keep VERIFY off the real refuter -- the concurrency proof is the lens
    # stage, not verification (both findings pass through unverified, kept).
    monkeypatch.setattr(refuter, "run_batch", lambda *a, **k: [])

    rc = job.main([str(request_path)])

    assert rc == 0
    # Both lenses cleared the barrier -> both findings persisted; sequential
    # execution would have deadlocked the barrier and surfaced zero.
    persisted = _persisted_findings(repo)
    assert {f.claim for f in persisted} == {"correctness finding", "duplication finding"}


# --- heartbeat -----------------------------------------------------------------


def test_heartbeat_touches_immediately_and_advances(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    old = time.time() - 1000
    os.utime(heartbeat, (old, old))

    request_path, _repo = _write_request(tmp_path, heartbeat)
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[])
    )

    rc = job.main([str(request_path)])

    assert rc == 0
    # The heartbeat thread touches immediately on start (before any wait),
    # long before the backdated stamp -- proves it advanced, not just that
    # some unrelated write happened to the file.
    assert heartbeat.stat().st_mtime > old + 500


# --- must never consult the optout hierarchy --------------------------------


def test_job_does_not_self_disable_under_inherited_chameleon_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_DISABLE", "1")
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, repo = _write_request(tmp_path, heartbeat)

    finding = _stub_finding()
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[finding])
    )
    monkeypatch.setattr(refuter, "run_batch", lambda *a, **k: [{"id": "0", "verdict": "confirmed"}])

    rc = job.main([str(request_path)])

    assert rc == 0
    # Findings were processed and persisted despite the inherited
    # CHAMELEON_DISABLE=1 -- job.py never reads it as a run/skip gate.
    assert len(_persisted_findings(repo)) == 1


# --- pre-VERIFY shadow log: precision sampling sees refuted rows too --------


def test_shadow_logs_raw_finding_before_verify_drops_it(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, repo = _write_request(tmp_path, heartbeat)

    finding = _stub_finding(
        claim="raw finding that gets refuted", file="src/widget.ts", span=(7, 7)
    )
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[finding])
    )
    # VERIFY refutes (drops) the only finding.
    monkeypatch.setattr(refuter, "run_batch", lambda *a, **k: [{"id": "0", "verdict": "refuted"}])

    rc = job.main([str(request_path)])

    assert rc == 0
    # Dropped by VERIFY -- nothing persisted to the ledger.
    assert _persisted_findings(repo) == []
    # But the RAW finding was shadow-logged before VERIFY ran, matching the
    # pre-cutover ``_correctness_judge_gate``'s emit shape exactly.
    rows = _shadow_log_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["repo_id"] == REPO_ID
    assert row["rule"] == "correctness-judge-finding"
    assert row["advisory_emitted"] is True
    assert row["would_block"] is False
    assert row["file_rel"] == "src/widget.ts"
    assert row["line"] == 7


def test_shadow_log_fires_for_every_raw_finding_regardless_of_verify_outcome(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, repo = _write_request(tmp_path, heartbeat)

    kept = _stub_finding(id="kept", claim="kept finding")
    dropped = _stub_finding(id="dropped", claim="dropped finding")
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[kept, dropped])
    )

    def _batch(_root, findings, *_a, **_k):
        verdicts = []
        for f in findings:
            verdicts.append(
                {
                    "id": f["id"],
                    "verdict": "confirmed" if f["claim"] == "kept finding" else "refuted",
                }
            )
        return verdicts

    monkeypatch.setattr(refuter, "run_batch", _batch)

    rc = job.main([str(request_path)])

    assert rc == 0
    # Only the confirmed finding survives to the ledger...
    persisted = _persisted_findings(repo)
    assert len(persisted) == 1
    assert persisted[0].claim == "kept finding"
    # ...but BOTH raw findings were shadow-logged pre-VERIFY.
    rows = _shadow_log_rows()
    assert len(rows) == 2


def test_no_shadow_log_rows_when_lenses_find_nothing(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, _repo = _write_request(tmp_path, heartbeat)
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[])
    )

    rc = job.main([str(request_path)])

    assert rc == 0
    assert _shadow_log_rows() == []


# --- delivery payload: written at job end (spec section 3.5) ----------------


def test_run_writes_delivery_payload_from_persisted_findings(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, repo = _write_request(tmp_path, heartbeat)

    finding = _stub_finding(claim="payload-worthy finding")
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[finding])
    )
    monkeypatch.setattr(refuter, "run_batch", lambda *a, **k: [{"id": "0", "verdict": "confirmed"}])

    rc = job.main([str(request_path)])

    assert rc == 0
    from chameleon_mcp.profile.trust import repo_data_dir

    payload = assemble.read_delivery_payload(repo_data_dir(REPO_ID), SID)
    assert payload is not None
    assert "payload-worthy finding" in payload.text
    # The job persisted the match_keys its render represents alongside the text,
    # so a cache-hit consumer marks delivered only what was shown.
    assert payload.match_keys == (finding.match_key,)


def test_run_clears_stale_payload_when_nothing_is_undelivered(tmp_path, monkeypatch):
    from chameleon_mcp.profile.trust import repo_data_dir

    assemble.write_delivery_payload(repo_data_dir(REPO_ID), SID, "stale leftover text", ("mk1",))
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request_path, _repo = _write_request(tmp_path, heartbeat)
    monkeypatch.setattr(
        lenses, "resolve_runner", lambda name: lambda *a, **k: LensResult(findings=[])
    )

    rc = job.main([str(request_path)])

    assert rc == 0
    assert assemble.read_delivery_payload(repo_data_dir(REPO_ID), SID) is None


# --- item B: the job must always clear its slot, even on setup failure ------
#
# Everything between request-load and the try/finally (resolving the
# heartbeat interval, constructing and starting the heartbeat thread) used to
# live OUTSIDE the try/finally that clears the job slot. A failure there
# (a bad threshold read, a thread the OS refused to start) skipped
# clear_job_slot entirely and left the session's single-inflight slot wedged
# until the heartbeat staleness window expired on its own.


def test_thread_construction_failure_still_clears_the_job_slot(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    request_path, _repo = _write_request(tmp_path, heartbeat)

    def _boom(*a, **k):
        raise RuntimeError("thread construction exploded")

    monkeypatch.setattr(job.threading, "Thread", _boom)

    rc = job.main([str(request_path)])

    assert rc == 0
    doc = read_session_doc(REPO_ID, SID)
    assert doc.job_inflight == ""  # slot cleared despite never reaching _run at all


def test_threshold_lookup_failure_before_run_still_clears_the_job_slot(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    request_path, _repo = _write_request(tmp_path, heartbeat)

    from chameleon_mcp import _thresholds

    def _boom(name):
        raise RuntimeError("threshold lookup exploded")

    monkeypatch.setattr(_thresholds, "threshold_int", _boom)

    rc = job.main([str(request_path)])

    assert rc == 0
    doc = read_session_doc(REPO_ID, SID)
    assert doc.job_inflight == ""
    events = _events()
    assert any(e.get("status") == "run_error" for e in events)


# --- scheduler.clear_job_slot: the deliberate divergence from _release_job_slot --


def test_clear_job_slot_clears_inflight_without_refunding_spend():
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    assert read_session_doc(REPO_ID, SID).review_spawns == 1

    scheduler.clear_job_slot(REPO_ID, SID)

    doc = read_session_doc(REPO_ID, SID)
    assert doc.job_inflight == ""
    assert doc.job_started_at == 0.0
    assert doc.review_spawns == 1


# --- conftest-guard confirmation: even the REAL (unstubbed) resolve_runner ---
# never reaches a real subprocess. Every other test above stubs
# ``resolve_runner``, which never even imports judge.py; this one goes
# through the real correctness/duplication/idiom lenses to prove the
# autouse guard's patches (``judge._spawn_reviewer_status``,
# ``refuter._spawn_status``) hold at the depth job.py actually calls them
# from, with ``subprocess.Popen`` itself hard-blocked as a second line of
# defense in case a future lens adds a spawn path the guard does not yet
# cover.


def test_main_through_real_lenses_never_spawns_claude(tmp_path, monkeypatch):
    import subprocess

    # The real (unstubbed) lens pipeline legitimately shells out to `git`
    # (diff reconstruction) and `node ts_dump.mjs` (TS AST extraction) as
    # part of ordinary, local, non-LLM evidence gathering -- those are not
    # what this guard is about, and both already fail open on OSError, so
    # every call is intercepted and denied hermetically (no real subprocess
    # of ANY kind touches the host). What matters is tracked separately: a
    # raising guard alone would be swallowed by the lens's own broad
    # `except Exception` (AssertionError IS an Exception subclass) and read
    # as a false pass, so `claude` invocations are recorded, not just denied.
    claude_calls: list[tuple] = []

    def _deny_popen(args, *a, **k):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if argv and "claude" in str(argv[0]):
            claude_calls.append((args, a, k))
        raise FileNotFoundError(f"blocked by test: no real subprocess allowed ({argv[:1]!r})")

    monkeypatch.setattr(subprocess, "Popen", _deny_popen)

    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    request_path, repo = _write_request(
        tmp_path, heartbeat, lens_names=("correctness", "duplication", "idiom")
    )

    rc = job.main([str(request_path)])

    assert rc == 0
    assert claude_calls == [], f"a real `claude` spawn was attempted: {claude_calls!r}"
    assert _persisted_findings(repo) == []
