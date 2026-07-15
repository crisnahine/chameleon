"""stop/scheduler.py: route decision, single-inflight job slot, detached launch.

Isolation mirrors test_judge_async_auto_route.py: an isolated
CHAMELEON_PLUGIN_DATA dir (so ``repo_data_dir`` never touches the real
plugin data dir) and an isolated HMAC key file (so the check-event writes
route() makes never touch -- or depend on -- the developer's real key at
``~/.claude/hooks/.exec_hmac.key``).

``scheduler.launch_job`` is neutralized by the autouse ``_no_real_judge_spawn``
fixture in conftest.py (the same fail-closed guard that protects the
judge/refuter spawn seams). Every test that exercises the real detach
mechanics opts out with ``@pytest.mark.real_judge_spawn`` and mocks
``subprocess.Popen`` itself, the same convention those tests already use.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from chameleon_mcp import autopass, intent_capture, tools
from chameleon_mcp import duplication_review as dr
from chameleon_mcp import hook_helper as hh
from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.core.session_state import SessionDoc, read_session_doc
from chameleon_mcp.profile.config import EnforcementConfig
from chameleon_mcp.stop import scheduler

REPO_ID = "sched-test-repo"
SID = "sched-sess-1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    # Check events (exec_log._exec_log_dir) key off TMPDIR, not
    # CHAMELEON_PLUGIN_DATA -- isolate it too so REPO_ID/SID, which are shared
    # module-level constants across every test in this file, never accumulate
    # events across test functions (or touch the developer's real /tmp).
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # route()'s decision.model comes from judge_model_for_route, which reads
    # these -- clear them so a developer's real shell env can never leak a
    # non-default model into a decision.model assertion below.
    for k in ("CHAMELEON_JUDGE_MODEL", "CHAMELEON_JUDGE_MODEL_HIGH", "CHAMELEON_JUDGE_TIERING"):
        monkeypatch.delenv(k, raising=False)
    yield


def _write_source(repo: Path, rel: str = "src/widget.ts", body: str = "export const x = 1;\n"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _stub_low_risk(monkeypatch, *, archetype: str | None = "Widget", blast_found: bool = True):
    monkeypatch.setattr(autopass, "security_surface_categories", lambda paths: set())
    monkeypatch.setattr(
        hh, "_archetype_resolver", lambda repo_root, daemon_state: lambda p: archetype
    )
    monkeypatch.setattr(
        tools,
        "query_symbol_importers",
        lambda repo, path: {"data": {"found": blast_found, "importers": []}},
    )


def _events(repo_id: str = REPO_ID) -> list[dict]:
    from chameleon_mcp.exec_log import read_check_events

    out = read_check_events(repo_id, SID, limit=200)
    return [e for e in out["events"] if e.get("check") == "review_job"]


# --- RouteDecision / route() ---------------------------------------------------


def test_route_subagent_never_spawns(tmp_path, monkeypatch):
    """SubagentStop refuses outright, before any digest/cap/risk logic runs."""
    repo = tmp_path / "repo"
    f = _write_source(repo)
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=True,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc()

    decision = scheduler.route(ctx, state, cfg)

    assert decision == scheduler.RouteDecision(spawn=False, reason="subagent_stop")


def test_route_mode_off_skips(tmp_path):
    repo = tmp_path / "repo"
    f = _write_source(repo)
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig(mode="off")
    state = SessionDoc()

    decision = scheduler.route(ctx, state, cfg)

    assert decision.spawn is False
    assert decision.reason == "mode_off"


def test_route_all_lenses_disabled_is_feature_disabled(tmp_path):
    repo = tmp_path / "repo"
    f = _write_source(repo)
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig(correctness_judge=False, duplication_review=False, idiom_review=False)
    state = SessionDoc()

    decision = scheduler.route(ctx, state, cfg)

    assert decision == scheduler.RouteDecision(spawn=False, reason="feature_disabled")


def test_route_skips_already_judged_digest(tmp_path):
    """A file already marked judged (.corr_judged.) this turn is not fresh."""
    repo = tmp_path / "repo"
    repo_data = tmp_path / "data"
    f = _write_source(repo)
    digest = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    rel = hh._repo_rel(repo, str(f))
    dr.mark_judged(repo_data, SID, rel, digest, prefix=hh._CORR_JUDGED_PREFIX)

    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=repo_data,
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc()

    decision = scheduler.route(ctx, state, cfg)

    assert decision == scheduler.RouteDecision(spawn=False, reason="digest_dup")
    assert any(e["status"] == "skipped_digest_dup" for e in _events())


def test_route_skips_at_session_cap(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    f = _write_source(repo)
    _stub_low_risk(monkeypatch)
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    cap = threshold_int("CORRECTNESS_JUDGE_MAX_SPAWNS_PER_SESSION")
    state = SessionDoc(review_spawns=cap)

    decision = scheduler.route(ctx, state, cfg)

    assert decision == scheduler.RouteDecision(spawn=False, reason="session_cap")
    assert any(e["status"] == "skipped_session_cap" for e in _events())


def test_route_intent_forced_ignores_risk_tier(tmp_path, monkeypatch):
    """Captured checkable intent forces a spawn regardless of the risk tier."""
    repo = tmp_path / "repo"
    f = _write_source(repo)
    monkeypatch.setattr(
        intent_capture, "checkable_tokens", lambda entries, since_ts=None: ["balance == 42"]
    )
    monkeypatch.setattr(
        intent_capture, "security_intent_seen", lambda entries, since_ts=None: False
    )
    # Deliberately do NOT stub risk facts low: intent-forced must win even if
    # the risk tier below would have routed high.
    monkeypatch.setattr(autopass, "security_surface_categories", lambda paths: {"auth"})

    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc()

    decision = scheduler.route(ctx, state, cfg)

    assert decision.spawn is True
    assert decision.reason == "intent_forced"
    assert decision.intent_tokens == ("balance == 42",)
    assert decision.files == (str(f),)
    assert decision.lens_names == ("correctness", "duplication", "idiom")
    # intent_forced is a high-risk route on the reviewer model ladder: the
    # base model must be escalated, end to end through route() itself, not
    # just at judge_model_for_route's own unit level.
    assert decision.model == "opus"


# --- intent contract (Task 4): scope lines / excerpts ride the decision --------


def test_route_intent_forced_carries_scope_lines_and_excerpts(tmp_path, monkeypatch):
    """The verbatim intent contract rides alongside the existing
    checkable-token intent trigger on a forced spawn."""
    repo = tmp_path / "repo"
    f = _write_source(repo)
    monkeypatch.setattr(
        intent_capture, "checkable_tokens", lambda entries, since_ts=None: ["balance == 42"]
    )
    monkeypatch.setattr(
        intent_capture, "security_intent_seen", lambda entries, since_ts=None: False
    )
    monkeypatch.setattr(
        intent_capture,
        "scope_lines",
        lambda entries, since_ts=None: ["don't touch the auth module"],
    )
    monkeypatch.setattr(
        intent_capture,
        "recent_excerpts",
        lambda entries, since_ts=None: ["don't touch the auth module"],
    )
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc()

    decision = scheduler.route(ctx, state, cfg)

    assert decision.spawn is True
    assert decision.reason == "intent_forced"
    assert decision.scope_lines == ("don't touch the auth module",)
    assert decision.intent_excerpts == ("don't touch the auth module",)


def test_route_first_low_risk_carries_scope_lines_and_excerpts(tmp_path, monkeypatch):
    """A non-intent-forced spawn (ordinary risk-tier routing) carries the
    same intent contract fields -- they are read once, on every route, not
    only on the intent_forced branch."""
    repo = tmp_path / "repo"
    f = _write_source(repo)
    _stub_low_risk(monkeypatch)
    monkeypatch.setattr(intent_capture, "checkable_tokens", lambda entries, since_ts=None: [])
    monkeypatch.setattr(
        intent_capture, "security_intent_seen", lambda entries, since_ts=None: False
    )
    monkeypatch.setattr(
        intent_capture,
        "scope_lines",
        lambda entries, since_ts=None: ["only change the retry count"],
    )
    monkeypatch.setattr(
        intent_capture,
        "recent_excerpts",
        lambda entries, since_ts=None: ["only change the retry count"],
    )
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc(review_spawns=0)

    decision = scheduler.route(ctx, state, cfg)

    assert decision.spawn is True
    assert decision.reason == "first_low_risk"
    assert decision.scope_lines == ("only change the retry count",)
    assert decision.intent_excerpts == ("only change the retry count",)


def test_route_scope_lines_alone_do_not_force_spawn(tmp_path, monkeypatch):
    """Additivity: the intent contract only rides ALONG an existing spawn
    decision. Captured scope lines with no checkable tokens and no security
    hit must not change whether the route spawns -- a low-risk, non-first
    turn still skips exactly as it did before Task 4."""
    repo = tmp_path / "repo"
    f = _write_source(repo)
    _stub_low_risk(monkeypatch)
    monkeypatch.setattr(intent_capture, "checkable_tokens", lambda entries, since_ts=None: [])
    monkeypatch.setattr(
        intent_capture, "security_intent_seen", lambda entries, since_ts=None: False
    )
    monkeypatch.setattr(
        intent_capture,
        "scope_lines",
        lambda entries, since_ts=None: ["don't touch the auth module"],
    )
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc(review_spawns=1)  # not the first spawn -> low risk skips

    decision = scheduler.route(ctx, state, cfg)

    # Equality against the plain skip decision also pins scope_lines and
    # intent_excerpts back to their () defaults on a non-spawning route.
    assert decision == scheduler.RouteDecision(spawn=False, reason="routed_skip_low_risk")


def test_route_first_low_risk_spawns_once(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    f = _write_source(repo)
    _stub_low_risk(monkeypatch)
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc(review_spawns=0)

    decision = scheduler.route(ctx, state, cfg)

    assert decision.spawn is True
    assert decision.reason == "first_low_risk"
    assert decision.files == (str(f),)
    # A low-risk route stays on the base model -- no escalation.
    assert decision.model == "sonnet"


def test_route_low_risk_skips_after_first_spawn(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    f = _write_source(repo)
    _stub_low_risk(monkeypatch)
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc(review_spawns=1)

    decision = scheduler.route(ctx, state, cfg)

    assert decision == scheduler.RouteDecision(spawn=False, reason="routed_skip_low_risk")
    assert any(e["status"] == "routed_skip_low_risk" for e in _events())


def test_route_security_surface_is_risk_high(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    f = _write_source(repo, rel="src/auth/login.ts")
    _stub_low_risk(monkeypatch)
    monkeypatch.setattr(autopass, "security_surface_categories", lambda paths: {"auth"})
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc(review_spawns=3)  # would routed_skip_low_risk if it weren't risk_high

    decision = scheduler.route(ctx, state, cfg)

    assert decision.spawn is True
    assert decision.reason == "risk_high"
    # risk_high is a high-risk route: escalated end to end through route().
    assert decision.model == "opus"


def test_route_unarchetyped_file_is_risk_elevated(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    f = _write_source(repo)
    _stub_low_risk(monkeypatch, archetype=None)
    ctx = scheduler.RouteContext(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        repo_data=tmp_path / "data",
        is_subagent=False,
        files=(str(f),),
    )
    cfg = EnforcementConfig()
    state = SessionDoc(review_spawns=3)

    decision = scheduler.route(ctx, state, cfg)

    assert decision.spawn is True
    assert decision.reason == "risk_elevated"
    # risk_elevated is NOT one of the high-risk routes that escalate.
    assert decision.model == "sonnet"


# --- JobRequest JSON round trip -------------------------------------------------


def test_job_request_json_round_trip(tmp_path):
    req = scheduler.JobRequest(
        repo_root=tmp_path / "repo",
        repo_id=REPO_ID,
        session_id=SID,
        files=(str(tmp_path / "repo" / "a.ts"),),
        intent_tokens=("tok",),
        lens_names=("correctness", "idiom"),
        model="sonnet",
        heartbeat_path=tmp_path / "data" / ".job_heartbeat.abc",
    )

    payload = json.loads(json.dumps(req.to_dict()))
    restored = scheduler.JobRequest.from_dict(payload)

    assert restored == req


def test_job_request_intent_contract_round_trip(tmp_path):
    """The intent contract fields (Task 4) round-trip through JSON exactly
    like every other JobRequest field."""
    req = scheduler.JobRequest(
        repo_root=tmp_path / "repo",
        repo_id=REPO_ID,
        session_id=SID,
        files=(str(tmp_path / "repo" / "a.ts"),),
        intent_tokens=("tok",),
        lens_names=("correctness", "idiom"),
        model="sonnet",
        heartbeat_path=tmp_path / "data" / ".job_heartbeat.abc",
        intent_excerpts=("don't touch the auth module",),
        scope_lines=("don't touch the auth module", "only change the retry count"),
    )

    payload = json.loads(json.dumps(req.to_dict()))
    restored = scheduler.JobRequest.from_dict(payload)

    assert restored == req
    assert restored.intent_excerpts == ("don't touch the auth module",)
    assert restored.scope_lines == (
        "don't touch the auth module",
        "only change the retry count",
    )


def test_job_request_from_dict_old_shape_defaults_to_empty_contract(tmp_path):
    """A request file written before Task 4 (no intent_excerpts/scope_lines
    keys, same shape ``shown_idiom_slugs`` had to handle for the prior
    field) round-trips those two fields to () rather than raising."""
    old_payload = {
        "repo_root": str(tmp_path / "repo"),
        "repo_id": REPO_ID,
        "session_id": SID,
        "files": [],
        "intent_tokens": [],
        "lens_names": ["correctness"],
        "model": "sonnet",
        "heartbeat_path": str(tmp_path / "data" / ".job_heartbeat.abc"),
    }

    restored = scheduler.JobRequest.from_dict(old_payload)

    assert restored.intent_excerpts == ()
    assert restored.scope_lines == ()
    assert restored.shown_idiom_slugs == ()


# --- try_acquire_job_slot -------------------------------------------------------


def test_try_acquire_job_slot_claims_and_spends_budget(tmp_path):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)

    assert heartbeat is not None
    assert heartbeat.is_file()
    doc = read_session_doc(REPO_ID, SID)
    assert doc.job_inflight == str(heartbeat)
    assert doc.job_started_at > 0
    assert doc.review_spawns == 1


def test_try_acquire_job_slot_loses_while_live(tmp_path):
    first = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert first is not None

    second = scheduler.try_acquire_job_slot(REPO_ID, SID)

    assert second is None
    # The budget was spent exactly once, not twice.
    assert read_session_doc(REPO_ID, SID).review_spawns == 1


def test_try_acquire_job_slot_reclaims_stale_heartbeat(tmp_path):
    first = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert first is not None

    stale_after = threshold_int("JOB_HEARTBEAT_STALE_SECONDS")
    old = time.time() - stale_after - 5
    os.utime(first, (old, old))

    second = scheduler.try_acquire_job_slot(REPO_ID, SID)

    assert second == first
    doc = read_session_doc(REPO_ID, SID)
    # Reclaiming spends a second unit of budget for the new job attempt.
    assert doc.review_spawns == 2


def test_try_acquire_job_slot_concurrent_exactly_one_wins(tmp_path):
    """spec §10 double-spawn property test: N racing threads, exactly one wins.

    Looped over fresh sessions at a tightened GIL switch interval because the
    detected failure mode was a narrow window: the heartbeat file used to be
    created AFTER update_session_doc returned (outside the flock), so a
    concurrent acquirer taking the flock in that gap saw job_inflight set with
    the heartbeat absent, read it as a dead job, and double-claimed. A single
    barrier race almost never lands in the window (0 hits in 800 iterations at
    default switching); 16 threads + 1us switching reproduced it within ~100
    iterations against the racy shape.
    """
    n = 16
    iterations = 60
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for it in range(iterations):
            sid = f"race-session-{it}"
            barrier = threading.Barrier(n)
            results: list[Path | None] = [None] * n

            def worker(i: int, sid: str = sid, barrier=barrier, results=results) -> None:
                barrier.wait()
                results[i] = scheduler.try_acquire_job_slot(REPO_ID, sid)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            winners = [r for r in results if r is not None]
            assert len(winners) == 1, f"iteration {it}: {len(winners)} winners"
            assert read_session_doc(REPO_ID, sid).review_spawns == 1
    finally:
        sys.setswitchinterval(old_interval)


def test_heartbeat_exists_whenever_claim_is_committed(tmp_path, monkeypatch):
    """Deterministic pin of the same double-claim class the loop above hunts
    probabilistically: at the instant update_session_doc commits a doc with
    job_inflight set, the heartbeat file must already exist. The racy shape
    (heartbeat touched after the flock released) fails this on every run,
    single-threaded -- no timing luck required."""
    from chameleon_mcp.core import session_state

    real = session_state.update_session_doc
    violations: list[str] = []

    def checked(repo_id, session_id, mutate):
        doc = real(repo_id, session_id, mutate)
        if doc.job_inflight and not Path(doc.job_inflight).exists():
            violations.append(doc.job_inflight)
        return doc

    monkeypatch.setattr(session_state, "update_session_doc", checked)

    assert scheduler.try_acquire_job_slot(REPO_ID, SID) is not None
    assert violations == []


# --- launch_job ------------------------------------------------------------------


def _make_request(
    tmp_path: Path, heartbeat: Path, *, model: str = "sonnet"
) -> scheduler.JobRequest:
    repo = tmp_path / "repo"
    f = _write_source(repo)
    return scheduler.JobRequest(
        repo_root=repo,
        repo_id=REPO_ID,
        session_id=SID,
        files=(str(f),),
        intent_tokens=(),
        lens_names=("correctness",),
        model=model,
        heartbeat_path=heartbeat,
    )


def test_conftest_guard_neutralizes_launch_job_by_default(tmp_path):
    """Without the opt-out marker, launch_job is the conftest's no-op stub."""
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request = _make_request(tmp_path, heartbeat)

    assert scheduler.launch_job(request) is False


@pytest.mark.real_judge_spawn
def test_launch_job_posix_detaches_with_start_new_session(tmp_path, monkeypatch):
    """This suite always runs on a POSIX host (Windows CI runs only the locks
    file), so launch_job's real ``os.name`` read takes the POSIX branch here.
    The Windows branch is covered by the pure ``_detach_kwargs`` tests below --
    mutating ``os.name`` to "nt" process-wide would flip pathlib's concrete
    ``Path`` class and break every filesystem call in the test."""
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request = _make_request(tmp_path, heartbeat)
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(scheduler.subprocess, "Popen", fake_popen)

    assert scheduler.launch_job(request) is True

    argv = captured["argv"]
    assert argv[0] == scheduler.sys.executable
    assert argv[1:3] == ["-m", "chameleon_mcp.stop.job"]
    assert argv[3].endswith(".json")
    assert Path(argv[3]).is_file()
    assert captured["kwargs"]["start_new_session"] is True
    assert "creationflags" not in captured["kwargs"]


def test_detach_kwargs_posix_uses_start_new_session():
    assert scheduler._detach_kwargs("posix") == {"start_new_session": True}


def test_detach_kwargs_windows_uses_detached_creationflags():
    kwargs = scheduler._detach_kwargs("nt")

    expected = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    assert kwargs == {"creationflags": expected}
    assert "start_new_session" not in kwargs


def test_detach_kwargs_unknown_platform_is_none():
    assert scheduler._detach_kwargs("java") is None


@pytest.mark.real_judge_spawn
def test_launch_job_unsupported_platform_never_falls_back_to_sync(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    request = _make_request(tmp_path, heartbeat)

    def fail_if_called(*a, **k):
        raise AssertionError("launch_job must never spawn on an unsupported platform")

    monkeypatch.setattr(scheduler, "_detach_kwargs", lambda os_name: None)
    monkeypatch.setattr(scheduler.subprocess, "Popen", fail_if_called)

    assert scheduler.launch_job(request) is False

    # The failed launch left nothing behind: heartbeat gone, slot released.
    assert not heartbeat.exists()
    assert read_session_doc(REPO_ID, SID).job_inflight == ""


@pytest.mark.real_judge_spawn
def test_launch_job_request_write_failure_cleans_heartbeat(tmp_path, monkeypatch):
    """A failed request-file write must leave nothing behind either: heartbeat
    unlinked and slot released, same as a failed detach."""
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    request = _make_request(tmp_path, heartbeat)
    monkeypatch.setattr(scheduler, "_write_request_file", lambda req: None)
    popen_calls: list = []
    monkeypatch.setattr(
        scheduler.subprocess, "Popen", lambda *a, **k: popen_calls.append(1) or SimpleNamespace()
    )

    assert scheduler.launch_job(request) is False

    assert popen_calls == []
    assert not heartbeat.exists()
    doc = read_session_doc(REPO_ID, SID)
    assert doc.job_inflight == ""
    assert doc.review_spawns == 0


@pytest.mark.real_judge_spawn
def test_launch_job_detach_failure_returns_false_and_rolls_back_slot(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    assert heartbeat is not None
    request = _make_request(tmp_path, heartbeat)

    def boom(*a, **k):
        raise OSError("no such interpreter")

    monkeypatch.setattr(scheduler.subprocess, "Popen", boom)

    assert scheduler.launch_job(request) is False

    assert not heartbeat.exists()
    doc = read_session_doc(REPO_ID, SID)
    assert doc.job_inflight == ""
    assert doc.job_started_at == 0.0
    assert doc.review_spawns == 0
    # The slot is free again: a later attempt can claim it.
    assert scheduler.try_acquire_job_slot(REPO_ID, SID) is not None


@pytest.mark.real_judge_spawn
def test_launch_job_env_disable_and_config_dir_inheritance(tmp_path, monkeypatch):
    """BUG-J1: the child inherits the real CLAUDE_CONFIG_DIR unchanged, never
    an empty throwaway dir (which strips OAuth/subscription auth), plus
    CHAMELEON_DISABLE=1 so the job's own reviewer spawns never recurse."""
    config_dir = str(tmp_path / "user-config" / ".claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", config_dir)
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request = _make_request(tmp_path, heartbeat)
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(scheduler.subprocess, "Popen", fake_popen)

    assert scheduler.launch_job(request) is True

    env = captured["env"]
    assert env["CHAMELEON_DISABLE"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == config_dir
    assert "chameleon-judge-" not in env.get("CLAUDE_CONFIG_DIR", "")


@pytest.mark.real_judge_spawn
def test_launch_job_rejects_invalid_model(tmp_path, monkeypatch):
    heartbeat = scheduler.try_acquire_job_slot(REPO_ID, SID)
    request = _make_request(tmp_path, heartbeat, model="not-a-real-model!!")
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(scheduler.subprocess, "Popen", fake_popen)

    assert scheduler.launch_job(request) is True

    request_path = Path(captured["argv"][3])
    written = json.loads(request_path.read_text(encoding="utf-8"))
    assert written["model"] == "sonnet"
