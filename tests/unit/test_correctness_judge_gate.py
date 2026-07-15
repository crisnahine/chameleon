"""Stop-hook model-review wiring tests for stop_backstop() (post phase-3 cutover).

The sync correctness-judge route/gate, the multi-lens pass, and the standalone
duplication gate are gone from the Stop pipeline. In their place, `stop_gates`
calls `stop/scheduler.py`'s `route()` and, on a spawn decision, claims the
session's single job slot and detaches a review job covering whichever lenses
(correctness/duplication/idiom) the repo's config enables -- ONE spawn per
Stop, never a sync `claude -p`. Findings arrive at the next UserPromptSubmit
(or a later SessionStart), never in-turn, UNLESS `CHAMELEON_JUDGE_WAIT=1`
(the harness/eval synchronous-wait path over the same detached job).

`stop.scheduler.launch_job` is neutralized to a no-op (returns False) by the
autouse conftest guard, mirroring the old judge-spawn guard -- these tests
observe the JobRequest a launch attempt WOULD have carried by monkeypatching
`chameleon_mcp.stop.scheduler.launch_job` themselves, never by mocking
`judge.run_correctness_judge` (nothing in the live Stop path calls it
anymore; that function lives on, uncalled, for a later deletion task).

Isolation: a real repo + config + plugin-data dir under tmp_path with
repo/trust/suppression resolution patched, the lint cold-path forced clean,
and TMPDIR/HMAC pointed at tmp_path so check events are readable.
"""

from __future__ import annotations

import io
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState, save_state

REPO_ID = "judge_repo_id"


@pytest.fixture(autouse=True)
def _event_isolation(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    yield


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory(*, mode: str = "enforce", correctness_judge: bool = True):
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps({"enforcement": {"mode": mode, "correctness_judge": correctness_judge}}),
            encoding="utf-8",
        )
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )

        data_dir = tmp_path / REPO_ID
        data_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        session_id = "s-judge"

        from chameleon_mcp.profile.trust import hash_profile

        trust_rec = MagicMock()
        trust_rec.grants_root.return_value = True
        trust_rec.hash_for_root.side_effect = lambda root: hash_profile(profile_dir)

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

        # A passing test run isolates this gate's own surface (the review job)
        # from the independent "no passing test run" reminder (its own
        # standalone advisory now -- see test_idiom_review_test_signal.py).
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(REPO_ID, session_id=session_id, command="pytest -q", exit_code=0)

        return repo, data_dir, session_id, file_path, profile_dir

    try:
        yield _factory
    finally:
        stack.close()


def _default_failed_launch(request):
    """Mirrors ``scheduler.launch_job``'s OWN failure contract: a launch that
    never became a real job releases the slot it claimed (heartbeat unlink +
    ``review_spawns`` refund), so a stub that skips this -- unlike the real
    function's internal ``_cleanup_failed_launch`` -- would leave the session
    permanently "job_inflight" and wedge every later Stop's routing."""
    from chameleon_mcp.stop.scheduler import _release_job_slot

    _release_job_slot(request.repo_id, request.session_id)
    return False


def _succeed_and_clear(request):
    """A launch that succeeds AND completes instantly, mirroring the real
    detached job runner's ``finally`` block (``stop/job.py::main``) clearing
    the session-doc job slot as it exits -- without this, a successful
    launch's slot claim stays set forever (nothing else in a mocked-launch
    test ever runs the real job to free it), and a SECOND Stop in the same
    test would misread a completed review as still in flight."""
    from chameleon_mcp.stop.scheduler import clear_job_slot

    clear_job_slot(request.repo_id, request.session_id)
    return True


def _run_stop(payload, env, *, launch_job=None, low_risk=False):
    """Drive stop_backstop with stop.scheduler.launch_job mocked.

    ``launch_job`` defaults to a stub recording every JobRequest and
    returning False (no real detach, slot released -- see
    ``_default_failed_launch``); pass a callable to observe/control the
    outcome. Returns (emitted_json, launch_mock).
    """
    cap = []
    calls: list = []
    _orig = launch_job if launch_job is not None else _default_failed_launch

    def launch_job(request):  # noqa: F811 -- wrap to still record calls
        calls.append(request)
        return _orig(request)

    with ExitStack() as stack:
        stack.enter_context(patch("sys.stdin", io.StringIO(json.dumps(payload))))
        out = stack.enter_context(patch("sys.stdout"))
        stack.enter_context(patch.dict(os.environ, env, clear=False))
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._stop_file_still_blockable", return_value=False)
        )
        if low_risk:
            # Archetype resolves, the reverse index answers with zero importers,
            # and the path carries no security tokens: the lowest routing tier.
            stack.enter_context(
                patch(
                    "chameleon_mcp.hook_helper._archetype_resolver",
                    return_value=lambda _p: "component",
                )
            )
            stack.enter_context(
                patch(
                    "chameleon_mcp.tools.query_symbol_importers",
                    return_value={"api_version": "1", "data": {"found": True, "importers": []}},
                )
            )
        stack.enter_context(patch("chameleon_mcp.stop.scheduler.launch_job", launch_job))
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return (json.loads(s) if s else {}), calls


def _touch_edited_file(file_path: str, data_dir: Path, session_id: str, content: str = "x = 1\n"):
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text(content, encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState()
    save_state(st, data_dir, session_id)


def _events(sid: str) -> list[dict]:
    from chameleon_mcp.exec_log import read_check_events

    out = read_check_events(REPO_ID, sid, limit=200)
    return [e for e in out["events"] if e.get("check") == "review_job"]


def _payload(repo, sid, **extra):
    p = {"session_id": sid, "cwd": str(repo), "stop_hook_active": False}
    p.update(extra)
    return p


def _session_doc(sid: str):
    from chameleon_mcp.core.session_state import read_session_doc

    return read_session_doc(REPO_ID, sid)


def test_qualifying_turn_attempts_job_launch_no_in_turn_block(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert len(calls) == 1
    request = calls[0]
    assert request.files == (file_path,)
    assert set(request.lens_names) == {"correctness", "duplication", "idiom"}
    assert request.model  # a validated model string, not ""
    # Job launched, no in-turn model-review block (async-first): the async
    # delivery path (stop/delivery.py) is what a next turn observes, not this
    # Stop's own additionalContext.
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "independent review" not in ctx
    assert out.get("decision") != "block"


def test_launch_failure_emits_degraded_event_and_refunds_budget(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert len(calls) == 1
    assert out.get("decision") != "block"
    events = _events(sid)
    # "spawned" records the decision (fires before the detach attempt) even
    # though the detach itself then fails -- a distinct "degraded" event
    # discloses the failure, matching the pre-phase-3 gate's two-signal shape
    # (spawned = decided+attempted, a separate status = the outcome).
    assert any(e["status"] == "spawned" for e in events)
    assert any(
        e["status"] == "degraded" and e.get("reason") == "platform_unavailable" for e in events
    )
    # launch_job's own rollback (heartbeat unlink + slot release, simulated by
    # _default_failed_launch above) refunds the spend on a failed launch -- a
    # mere detach hiccup must not cost the session a real review. Exercised
    # for real (not simulated) in test_stop_scheduler.py's launch_job suite.
    assert _session_doc(sid).review_spawns == 0
    assert _session_doc(sid).job_inflight == ""


def test_successful_launch_emits_spawned_event_and_charges_budget(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    out, calls = _run_stop(
        _payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, launch_job=lambda req: True
    )

    assert len(calls) == 1
    assert out.get("decision") != "block"
    events = _events(sid)
    spawned = [e for e in events if e["status"] == "spawned"]
    assert len(spawned) == 1
    assert spawned[0].get("detail", {}).get("lenses")
    assert spawned[0].get("detail", {}).get("files") == 1
    # A successful launch is NOT a failed launch: the spend stands.
    assert _session_doc(sid).review_spawns == 1


def test_correctness_disabled_still_launches_for_other_lenses(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(correctness_judge=False)
    _touch_edited_file(file_path, data_dir, sid)

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert len(calls) == 1
    assert "correctness" not in calls[0].lens_names
    assert set(calls[0].lens_names) == {"duplication", "idiom"}


def test_all_lenses_disabled_never_launches(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    profile_dir.joinpath("config.json").write_text(
        json.dumps(
            {
                "enforcement": {
                    "mode": "enforce",
                    "correctness_judge": False,
                    "duplication_review": False,
                    "idiom_review": False,
                }
            }
        ),
        encoding="utf-8",
    )
    _touch_edited_file(file_path, data_dir, sid)

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert calls == []
    assert out == {}


def test_already_judged_digest_skips_launch(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    import hashlib

    from chameleon_mcp import duplication_review as dr
    from chameleon_mcp import hook_helper as hh

    digest = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()[:16]
    rel = hh._repo_rel(repo, file_path)
    dr.mark_judged(data_dir, sid, rel, digest, prefix=hh._CORR_JUDGED_PREFIX)

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert calls == []
    assert any(e["status"] == "skipped_digest_dup" for e in _events(sid))


def test_session_spawn_cap_skips_launch(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.core.session_state import update_session_doc

    cap = threshold_int("CORRECTNESS_JUDGE_MAX_SPAWNS_PER_SESSION")
    update_session_doc(REPO_ID, sid, lambda doc: setattr(doc, "review_spawns", cap))

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert calls == []
    assert any(e["status"] == "skipped_session_cap" for e in _events(sid))


def test_low_risk_second_turn_skipped_with_event(make_trusted_repo):
    # The first low-risk routed turn still launches a job (at-least-once
    # coverage); a later low-risk turn skips and records the un-run check.
    # The first launch must SUCCEED (not the default failed-and-refunded
    # stub): review_spawns has to stay charged past 0, or every turn reads
    # as "the first spawn this session" and never reaches the skip branch.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid, content="const a = 1\n")

    _, calls1 = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        low_risk=True,
        launch_job=_succeed_and_clear,
    )
    assert len(calls1) == 1

    _touch_edited_file(file_path, data_dir, sid, content="const a = 2\n")
    _, calls2 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, low_risk=True)
    assert calls2 == []
    assert any(e["status"] == "routed_skip_low_risk" for e in _events(sid))


def test_intent_tokens_force_launch_on_low_risk_turn(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid, content="const a = 1\n")

    # A successful first launch (see test_low_risk_second_turn_skipped_with_event
    # for why a failed-and-refunded one would not exercise the skip path the
    # intent trigger is meant to override).
    _, calls1 = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        low_risk=True,
        launch_job=_succeed_and_clear,
    )
    assert len(calls1) == 1

    # Capture intent AFTER the first launch so the tokens are newer than the
    # "spawned" event's timestamp and survive the since_ts filter.
    time.sleep(0.02)
    from chameleon_mcp.intent_capture import capture_intent

    capture_intent(data_dir, sid, "set retryLimit to 25")

    _touch_edited_file(file_path, data_dir, sid, content="const a = 2\n")
    _, calls2 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, low_risk=True)
    assert len(calls2) == 1
    assert "25" in calls2[0].intent_tokens and "retryLimit" in calls2[0].intent_tokens
    assert any(
        e["status"] == "spawned" and e.get("reason") == "intent_forced" for e in _events(sid)
    )


def test_subagent_stop_never_launches(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    out, calls = _run_stop(
        _payload(repo, sid, hook_event_name="SubagentStop"), env={"CHAMELEON_ENFORCE": "1"}
    )

    assert calls == []
    assert out == {}


def test_inline_bare_ignore_skips(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(
        file_path, data_dir, sid, content="// chameleon-ignore\nexport const C = 1\n"
    )

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert calls == []
    assert out == {}


def test_judge_off_mode_never_launches(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="off")
    _touch_edited_file(file_path, data_dir, sid)

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert calls == []
    assert out == {}


def test_fails_open_when_route_raises(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    with patch("chameleon_mcp.hook_helper._scheduler_route", side_effect=RuntimeError("boom")):
        out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert calls == []
    assert out.get("decision") != "block"


def test_non_oserror_from_launch_job_still_releases_the_slot(make_trusted_repo):
    # launch_job's own cleanup (_cleanup_failed_launch) only catches OSError
    # around subprocess.Popen. A non-OSError escaping it must not leave the
    # session's single-inflight slot claimed (job_inflight set, review_spawns
    # charged) forever -- _run_review_job must release it itself.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    def _raise(_request):
        raise RuntimeError("launch exploded, not an OSError")

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, launch_job=_raise)

    assert len(calls) == 1
    assert out.get("decision") != "block"
    events = _events(sid)
    assert any(
        e["status"] == "degraded" and e.get("reason") == "platform_unavailable" for e in events
    )
    # The slot claim is fully rolled back, not left wedged for the rest of
    # the heartbeat-staleness window.
    assert _session_doc(sid).review_spawns == 0
    assert _session_doc(sid).job_inflight == ""


def test_judge_wait_renders_findings_in_turn(make_trusted_repo):
    """CHAMELEON_JUDGE_WAIT=1: a job that clears its slot instantly (as the
    real detached runner's ``finally`` block does) and leaves a ledger row
    behind is observed IN-TURN, via the same delivery renderer next-turn
    UserPromptSubmit uses."""
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    def _instant_job(request):
        from chameleon_mcp.core.finding import Finding, compute_match_key
        from chameleon_mcp.stop.scheduler import clear_job_slot

        clear_job_slot(request.repo_id, request.session_id)
        finding = Finding(
            id=compute_match_key("dropped await on save()", "src/Widget.ts", "correctness"),
            kind="correctness",
            severity="high",
            confidence=0.9,
            file="src/Widget.ts",
            span=(12, 12),
            claim="dropped await on save()",
            evidence="",
            excerpt_sha="",
            excerpt="",
            source_lens="correctness",
            status="pending",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        from chameleon_mcp import review_ledger

        review_ledger.record_findings(request.repo_id, str(request.repo_root), [finding])
        return True

    out, calls = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_JUDGE_WAIT": "1"},
        launch_job=_instant_job,
    )

    assert len(calls) == 1
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "dropped await on save()" in ctx
    assert "src/Widget.ts:12" in ctx


def test_shown_idiom_names_translate_to_slugs_in_job_request(make_trusted_repo):
    # spec section 10.1's Tier-2/memory-channel dedup must-keep: the shown
    # signal the per-edit hook actually populates is idioms_shown_names (the
    # idiom's TITLE, per core.idiom_store's title/slug split), so the
    # scheduler must translate it into the taught idiom's real slug before
    # handing it to the job the idiom lens will read it from.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()

    from chameleon_mcp.core.idiom_store import IdiomRecord, upsert_idiom

    upsert_idiom(
        profile_dir,
        IdiomRecord(
            slug="wrap-fetches",
            title="wrap-fetches",
            rationale="Always wrap fetches in the apiClient helper.",
            languages=["typescript"],
            archetypes=[],
            paths=[],
            status="active",
            added_date="2026-07-15",
            rank=1,
        ),
    )
    _touch_edited_file(file_path, data_dir, sid)
    st = EnforcementState()
    st.files[file_path] = FileState()
    st.idioms_shown_names = {"wrap-fetches"}
    save_state(st, data_dir, sid)

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert len(calls) == 1
    assert calls[0].shown_idiom_slugs == ("wrap-fetches",)
    # The route decision itself is unaffected -- the shown-slug exclusion is
    # the idiom LENS's own job, not the scheduler's route.
    assert out.get("decision") != "block"


def test_no_shown_idiom_names_yields_empty_shown_slugs(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    assert len(calls) == 1
    assert calls[0].shown_idiom_slugs == ()


def test_judge_wait_multiple_findings_fold_into_one_review_block(make_trusted_repo):
    # Single-emit: several surviving findings from one job must still render
    # as ONE model-review context block (one header), never one per finding
    # -- structurally guaranteed by _run_review_job being the only call site
    # and render_findings emitting exactly one header per call, pinned here
    # end-to-end through the real Stop pipeline.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    def _instant_job_two_findings(request):
        from chameleon_mcp.core.finding import Finding, compute_match_key
        from chameleon_mcp.stop.scheduler import clear_job_slot

        clear_job_slot(request.repo_id, request.session_id)
        findings = [
            Finding(
                id=compute_match_key(f"issue {i}", "src/Widget.ts", "correctness"),
                kind="correctness",
                severity="high",
                confidence=0.9,
                file="src/Widget.ts",
                span=(i, i),
                claim=f"issue {i}",
                evidence="",
                excerpt_sha="",
                excerpt="",
                source_lens="correctness",
                status="pending",
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            for i in (10, 20)
        ]
        from chameleon_mcp import review_ledger

        review_ledger.record_findings(request.repo_id, str(request.repo_root), findings)
        return True

    out, calls = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_JUDGE_WAIT": "1"},
        launch_job=_instant_job_two_findings,
    )

    assert len(calls) == 1
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "issue 10" in ctx and "issue 20" in ctx
    assert ctx.count("[\U0001f98e") == 1


def test_resurface_wired_into_live_stop_pipeline(make_trusted_repo):
    """The finding->fix loop's resurface re-check must run through the REAL
    stop_backstop -> stop_gates chain, not just review_ledger's own
    unit-level API -- a naive cutover left pipeline.py calling the dead
    drift.db-backed helper while findings landed in the new review_ledger
    store, so nothing ever resurfaced in production even though the
    ledger-level logic was correct in isolation."""
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    from chameleon_mcp import review_ledger
    from chameleon_mcp.core.finding import Finding, compute_match_key

    finding = Finding(
        id=compute_match_key("leftover bug from last turn", "src/Widget.ts", "correctness"),
        kind="correctness",
        severity="high",
        confidence=0.9,
        file="src/Widget.ts",
        span=(12, 12),
        claim="leftover bug from last turn",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    review_ledger.record_findings(REPO_ID, str(repo), [finding])

    out, calls = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})

    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "unaddressed high-severity" in ctx
    assert "src/Widget.ts:12" in ctx

    rows = review_ledger._read_findings_rows(REPO_ID)
    assert rows[finding.match_key]["status"] == "resurfaced"


def test_resurface_kill_switch_suppresses_resurface_line(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    from chameleon_mcp import review_ledger
    from chameleon_mcp.core.finding import Finding, compute_match_key

    finding = Finding(
        id=compute_match_key("leftover bug, ledger off", "src/Widget.ts", "correctness"),
        kind="correctness",
        severity="high",
        confidence=0.9,
        file="src/Widget.ts",
        span=(12, 12),
        claim="leftover bug, ledger off",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    review_ledger.record_findings(REPO_ID, str(repo), [finding])

    out, calls = _run_stop(
        _payload(repo, sid), env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_FINDING_LEDGER": "0"}
    )

    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "unaddressed high-severity" not in ctx

    # The kill switch skips the recheck entirely -- the row is left untouched
    # rather than silently marked resurfaced/addressed behind the operator's
    # back.
    rows = review_ledger._read_findings_rows(REPO_ID)
    assert rows[finding.match_key]["status"] == "pending"


def test_judge_wait_off_by_default_no_in_turn_render(make_trusted_repo):
    """Without CHAMELEON_JUDGE_WAIT, an instantly-completing job's finding
    still does NOT render in-turn -- it stays pending for the real delivery
    point (next UserPromptSubmit / a later SessionStart)."""
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    def _instant_job(request):
        from chameleon_mcp.core.finding import Finding, compute_match_key
        from chameleon_mcp.stop.scheduler import clear_job_slot

        clear_job_slot(request.repo_id, request.session_id)
        finding = Finding(
            id=compute_match_key("dropped await on save()", "src/Widget.ts", "correctness"),
            kind="correctness",
            severity="high",
            confidence=0.9,
            file="src/Widget.ts",
            span=(12, 12),
            claim="dropped await on save()",
            evidence="",
            excerpt_sha="",
            excerpt="",
            source_lens="correctness",
            status="pending",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        from chameleon_mcp import review_ledger

        review_ledger.record_findings(request.repo_id, str(request.repo_root), [finding])
        return True

    out, calls = _run_stop(
        _payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, launch_job=_instant_job
    )

    assert len(calls) == 1
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "dropped await on save()" not in ctx
