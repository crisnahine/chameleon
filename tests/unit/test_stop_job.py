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
import time
from pathlib import Path

import pytest

from chameleon_mcp import refuter, review_ledger
from chameleon_mcp.core.finding import Finding
from chameleon_mcp.core.session_state import read_session_doc
from chameleon_mcp.stop import job, lenses, scheduler
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
