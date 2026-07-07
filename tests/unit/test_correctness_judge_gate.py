"""Stop-hook correctness-judge gate tests for stop_backstop().

The judge gate runs on the no-block stop path, after the idiom gate declines to
block. It is on by default (`enforcement.correctness_judge`, set false to opt
out) and ADVISORY ONLY: it never returns a Stop block, only `additionalContext`
carrying the reviewer's findings. Routing is per-turn and digest-keyed: a Stop
only spawns when at least one touched file is fresh at its current content
digest, fresh turns are risk-routed (security surface, unarchetyped files,
blast radius with unknown-escalates) under a per-session spawn budget, and
captured intent tokens force a spawn. The real `claude -p` spawn is mocked here
via judge.run_correctness_judge.

Isolation mirrors test_idiom_review: a real repo + config + plugin-data dir under
tmp_path with repo/trust/suppression resolution patched, the lint cold-path
forced clean, and TMPDIR/HMAC pointed at tmp_path so check events are readable.
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

from chameleon_mcp.enforcement import EnforcementState, FileState, load_state, save_state
from chameleon_mcp.judge import Finding

REPO_ID = "judge_repo_id"


@pytest.fixture(autouse=True)
def _event_isolation(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # A known bare-auth failure routes the judge through launch_async_judge,
    # so the sync run_correctness_judge mock would never fire. Isolate the
    # marker dir (judge reads plugin_paths.plugin_data_dir directly, not the
    # hook_helper alias patched below) and reset the process-global cache so
    # the developer's real marker can never flip these tests to the async
    # route.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_JUDGE_ASYNC", raising=False)
    from chameleon_mcp import judge

    monkeypatch.setattr(judge, "_BARE_AUTH_OK", None, raising=False)
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", False, raising=False)
    yield


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory(*, mode: str = "enforce", correctness_judge: bool = True):
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps(
                {
                    "enforcement": {
                        "mode": mode,
                        # idiom_review off so the idiom gate never blocks first and
                        # the judge gate is the surface under test.
                        "idiom_review": False,
                        # multi_lens_review is default-on but REPLACES the single
                        # correctness-judge spawn this gate exercises; disable it so
                        # the correctness-judge route is the surface under test.
                        "multi_lens_review": False,
                        "correctness_judge": correctness_judge,
                    }
                }
            ),
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

        return repo, data_dir, session_id, file_path, profile_dir

    try:
        yield _factory
    finally:
        stack.close()


def _run_stop(payload, env, *, findings=None, side_effect=None, low_risk=False):
    cap = []
    if side_effect is not None:
        rcj = patch("chameleon_mcp.judge.run_correctness_judge", side_effect=side_effect)
    else:
        rcj = patch(
            "chameleon_mcp.judge.run_correctness_judge",
            return_value=findings if findings is not None else [],
        )
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
        mock_rcj = stack.enter_context(rcj)
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return (json.loads(s) if s else {}), mock_rcj


def _touch_edited_file(file_path: str, data_dir: Path, session_id: str, content: str = "x = 1\n"):
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text(content, encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState()
    save_state(st, data_dir, session_id)


def _events(sid: str) -> list[dict]:
    from chameleon_mcp.exec_log import read_check_events

    out = read_check_events(REPO_ID, sid, limit=200)
    return [e for e in out["events"] if e.get("check") == "correctness_judge"]


def _payload(repo, sid, **extra):
    p = {"session_id": sid, "cwd": str(repo), "stop_hook_active": False}
    p.update(extra)
    return p


def test_judge_findings_emit_advisory_context_never_block(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    findings = [
        Finding(message="dropped await on save()", confidence=0.9, file="src/Widget.ts", line=12)
    ]

    out, mock_rcj = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
    )
    mock_rcj.assert_called_once()
    # Advisory only: no Stop block, findings ride out as additionalContext.
    assert out.get("decision") != "block"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "independent review" in ctx
    assert "dropped await on save()" in ctx
    assert "src/Widget.ts:12" in ctx


def test_judge_no_findings_allows_clean_stop(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    out, mock_rcj = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[],
    )
    mock_rcj.assert_called_once()
    assert out == {}


def test_judge_disabled_by_default_not_spawned(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(correctness_judge=False)
    _touch_edited_file(file_path, data_dir, sid)

    out, mock_rcj = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[Finding(message="x", confidence=0.9)],
    )
    mock_rcj.assert_not_called()
    assert out == {}


def test_same_content_second_stop_does_not_respawn(make_trusted_repo):
    # Per-turn digest dedup: a successful spawn marks each fresh file judged at
    # its content digest, so an unchanged second Stop routes to no-spawn.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    findings = [Finding(message="bug", confidence=0.7)]

    out1, mock1 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, findings=findings)
    assert mock1.call_count == 1
    assert "additionalContext" in out1.get("hookSpecificOutput", {})

    out2, mock2 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, findings=findings)
    # The spawn-dedup contract: no RESPAWN on unchanged content (+ the
    # skipped_digest_dup event). The finding ledger (default on) separately
    # surfaces this unaddressed high-severity finding once more here -- a distinct
    # #9 concern covered in test_finding_ledger -- so out2 is not required empty.
    mock2.assert_not_called()
    assert any(e["status"] == "skipped_digest_dup" for e in _events(sid))


def test_changed_security_surface_content_respawns(make_trusted_repo):
    # Changed content re-routes; a security-surface path rides the high tier and
    # re-spawns even though the session already spent a spawn.
    repo, data_dir, sid, _ignored, profile_dir = make_trusted_repo()
    auth_path = str(repo / "src" / "auth" / "login.ts")
    _touch_edited_file(auth_path, data_dir, sid, content="export const a = 1\n")

    _, mock1 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})
    assert mock1.call_count == 1

    _touch_edited_file(auth_path, data_dir, sid, content="export const a = 2\n")
    _, mock2 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})
    assert mock2.call_count == 1


def test_low_risk_second_turn_skipped_with_event(make_trusted_repo):
    # The first low-risk routed turn still spawns (at-least-once coverage);
    # later low-risk turns skip and record the un-run check.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid, content="const a = 1\n")

    _, mock1 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, low_risk=True)
    assert mock1.call_count == 1

    _touch_edited_file(file_path, data_dir, sid, content="const a = 2\n")
    _, mock2 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, low_risk=True)
    mock2.assert_not_called()
    assert any(e["status"] == "routed_skip_low_risk" for e in _events(sid))


def test_session_spawn_cap_honored_with_event(make_trusted_repo):
    repo, data_dir, sid, _ignored, profile_dir = make_trusted_repo()
    auth_path = str(repo / "src" / "auth" / "login.ts")
    _touch_edited_file(auth_path, data_dir, sid, content="export const a = 1\n")
    env = {"CHAMELEON_ENFORCE": "1", "CHAMELEON_CORRECTNESS_JUDGE_MAX_SPAWNS_PER_SESSION": "1"}

    _, mock1 = _run_stop(_payload(repo, sid), env=env)
    assert mock1.call_count == 1

    # High-risk and fresh, but the budget is spent.
    _touch_edited_file(auth_path, data_dir, sid, content="export const a = 2\n")
    _, mock2 = _run_stop(_payload(repo, sid), env=env)
    mock2.assert_not_called()
    assert any(e["status"] == "skipped_session_cap" for e in _events(sid))


def test_intent_tokens_force_spawn_on_low_risk_turn(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid, content="const a = 1\n")

    _, mock1 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, low_risk=True)
    assert mock1.call_count == 1

    # Capture intent AFTER the first spawn so the tokens are newer than the
    # last spawned event and survive the since_ts filter.
    time.sleep(0.02)
    from chameleon_mcp.intent_capture import capture_intent

    capture_intent(data_dir, sid, "set retryLimit to 25")

    _touch_edited_file(file_path, data_dir, sid, content="const a = 2\n")
    _, mock2 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, low_risk=True)
    assert mock2.call_count == 1
    tokens = mock2.call_args.kwargs.get("intent_tokens")
    assert "25" in tokens and "retryLimit" in tokens
    assert any(
        e["status"] == "spawned" and e.get("reason") == "intent_forced" for e in _events(sid)
    )


def test_security_worded_intent_forces_spawn_on_low_risk_turn(make_trusted_repo):
    # No extractable tokens, but the prompt named a guard construct: the
    # security lens forces the review on an otherwise-skipped low-risk turn.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid, content="const a = 1\n")

    _, mock1 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, low_risk=True)
    assert mock1.call_count == 1

    time.sleep(0.02)
    from chameleon_mcp.intent_capture import capture_intent

    capture_intent(data_dir, sid, "make sure authorization still runs on every request")

    _touch_edited_file(file_path, data_dir, sid, content="const a = 2\n")
    _, mock2 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, low_risk=True)
    assert mock2.call_count == 1
    assert any(
        e["status"] == "spawned" and e.get("reason") == "intent_forced" for e in _events(sid)
    )


def test_subagent_stop_never_spawns(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    out, mock_rcj = _run_stop(
        _payload(repo, sid, hook_event_name="SubagentStop"),
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[Finding(message="x", confidence=0.9)],
    )
    mock_rcj.assert_not_called()
    assert out == {}


def test_spawn_failure_leaves_files_unmarked_for_retry(make_trusted_repo):
    repo, data_dir, sid, _ignored, profile_dir = make_trusted_repo()
    auth_path = str(repo / "src" / "auth" / "login.ts")
    _touch_edited_file(auth_path, data_dir, sid, content="export const a = 1\n")

    def failing_judge(*a, **k):
        sink = k.get("event_sink")
        if sink is not None:
            sink("spawn_timeout", None)
        return []

    _, mock1 = _run_stop(
        _payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, side_effect=failing_judge
    )
    assert mock1.call_count == 1
    events = _events(sid)
    assert any(e["status"] == "spawned" for e in events)
    assert any(
        e["status"] == "degraded_spawn" and e.get("reason") == "spawn_timeout" for e in events
    )

    # Same content, but the failed spawn left it unmarked: the next Stop
    # re-routes and retries under the session cap.
    _, mock2 = _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"})
    assert mock2.call_count == 1


def test_judge_facts_sink_kind_recorded_as_own_check(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    def facts_judge(*a, **k):
        sink = k.get("event_sink")
        if sink is not None:
            sink("judge_facts_included", None)
        return []

    _run_stop(_payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, side_effect=facts_judge)

    from chameleon_mcp.exec_log import read_check_events

    all_events = read_check_events(REPO_ID, sid, limit=200)["events"]
    # The facts outcome lands under its own check name, never as a spawn
    # degradation of correctness_judge.
    assert any(e["check"] == "judge_facts" and e["status"] == "included" for e in all_events)
    assert not any(
        e["check"] == "correctness_judge"
        and e["status"] == "degraded_spawn"
        and str(e.get("reason", "")).startswith("judge_facts")
        for e in all_events
    )


def test_correctness_spawns_persisted_before_spawn(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    seen = {}

    def crashing_judge(*a, **k):
        # Read the state file the way a parallel process would: the counter must
        # already be persisted before the (potentially slow) spawn runs.
        seen["spawns"] = load_state(data_dir, sid).correctness_spawns
        raise RuntimeError("interrupted mid-spawn")

    out, mock_rcj = _run_stop(
        _payload(repo, sid), env={"CHAMELEON_ENFORCE": "1"}, side_effect=crashing_judge
    )
    mock_rcj.assert_called_once()
    assert seen["spawns"] == 1
    # Fail open: the raising spawn never blocks the turn.
    assert out.get("decision") != "block"


def test_judge_off_mode_not_spawned(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="off")
    _touch_edited_file(file_path, data_dir, sid)

    out, mock_rcj = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[Finding(message="x", confidence=0.9)],
    )
    mock_rcj.assert_not_called()
    assert out == {}


def test_judge_shadow_mode_still_runs(make_trusted_repo):
    # The judge never blocks, so it runs in shadow as well as enforce; the
    # findings are advisory context either way.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    _touch_edited_file(file_path, data_dir, sid)

    out, mock_rcj = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[Finding(message="off by one", confidence=0.6)],
    )
    mock_rcj.assert_called_once()
    assert out.get("decision") != "block"
    assert "off by one" in out["hookSpecificOutput"]["additionalContext"]


def test_judge_findings_shadow_logged_as_metrics(make_trusted_repo, tmp_path):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    findings = [Finding(message="missing guard", confidence=0.8, file="src/Widget.ts", line=3)]

    with patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}, clear=False):
        out, _ = _run_stop(
            _payload(repo, sid),
            env={"CHAMELEON_ENFORCE": "1"},
            findings=findings,
        )

    metrics = tmp_path / "metrics.jsonl"
    assert metrics.is_file()
    rows = [json.loads(line) for line in metrics.read_text().splitlines() if line.strip()]
    judge_rows = [r for r in rows if r.get("hook") == "stop-correctness-judge"]
    assert len(judge_rows) == 1
    row = judge_rows[0]
    assert row["would_block"] is False
    assert row["advisory_emitted"] is True
    assert row["rule"] == "correctness-judge-finding"
    assert row["file_rel"] == "src/Widget.ts"
    assert row["line"] == 3


def test_judge_inline_bare_ignore_skips(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(
        file_path,
        data_dir,
        sid,
        content="// chameleon-ignore\nexport const C = 1\n",
    )

    out, mock_rcj = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[Finding(message="x", confidence=0.9)],
    )
    mock_rcj.assert_not_called()
    assert out == {}


def test_judge_fails_open_when_run_raises(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

    out, mock_rcj = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        side_effect=RuntimeError("spawn exploded"),
    )
    mock_rcj.assert_called_once()
    # Fail open: no crash, no block, valid JSON.
    assert out.get("decision") != "block"


# --- VERIFY stage wiring (sync gate) -------------------------------------------


def _fresh_budget(monkeypatch):
    """Anchor the sync VERIFY budget clock to 'now' so it behaves as it would in a
    fresh one-shot hook process (the pytest process has been alive far longer)."""
    from chameleon_mcp import hook_helper

    monkeypatch.setattr(hook_helper, "_PROCESS_START_MONOTONIC", time.monotonic())


def _stub_gate_refuter(monkeypatch, verdicts_by_id):
    calls = {"run_batch": 0}

    def fake_run_batch(repo_root, findings, excerpts, *, model, timeout, max_spawns, **kw):
        calls["run_batch"] += 1
        return [
            {"id": f.get("id"), "verdict": verdicts_by_id.get(f.get("id"), "unverified")}
            for f in findings[:max_spawns]
        ]

    import chameleon_mcp.refuter as refuter

    monkeypatch.setattr(refuter, "refuter_cli_absent", lambda: None)
    monkeypatch.setattr(refuter, "run_batch", fake_run_batch)
    return calls


def test_verify_drops_refuted_tags_confirmed_end_to_end(make_trusted_repo, monkeypatch, tmp_path):
    """The full sync pipeline: judge findings -> VERIFY refutes one, confirms one ->
    the advisory renders only survivors with the grounding banner, the raw findings
    are still shadow-logged, and only survivors persist to the ledger."""
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    _fresh_budget(monkeypatch)
    calls = _stub_gate_refuter(monkeypatch, {"0": "refuted", "1": "confirmed"})
    findings = [
        Finding(message="false alarm", confidence=0.9, file="src/Widget.ts", line=1),
        Finding(message="real dropped await", confidence=0.9, file="src/Widget.ts", line=1),
    ]

    with patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}, clear=False):
        out, mock_rcj = _run_stop(
            _payload(repo, sid),
            env={"CHAMELEON_ENFORCE": "1"},
            findings=findings,
        )

    mock_rcj.assert_called_once()
    assert calls["run_batch"] == 1
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "real dropped await" in ctx
    assert "false alarm" not in ctx  # refuted -> dropped from the advisory
    assert "1 refuted and dropped, 1 confirmed" in ctx
    assert "src/Widget.ts:1 [confirmed]" in ctx
    # Raw findings (both) shadow-logged for precision sampling.
    metrics = tmp_path / "metrics.jsonl"
    rows = [json.loads(line) for line in metrics.read_text().splitlines() if line.strip()]
    judge_rows = [r for r in rows if r.get("hook") == "stop-correctness-judge"]
    assert len(judge_rows) == 2
    # Only the survivor persists to the finding->fix ledger (rows carry a message
    # fingerprint, not the text, so assert the count: 1 survivor, not 2 raw).
    from chameleon_mcp.drift.observations import open_judge_findings

    open_rows = open_judge_findings(REPO_ID, ws_root=str(repo))
    assert len(open_rows) == 1
    # The verified check event is recorded for attestation replay.
    assert any(e["status"] == "verified" for e in _events(sid))


def test_verify_exhausted_budget_passes_findings_through(make_trusted_repo, monkeypatch):
    """A long-running Stop (no budget left under the 55s wrapper) must surface the
    raw findings unverified -- never spawn, never drop, no banner."""
    from chameleon_mcp import hook_helper

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    monkeypatch.setattr(hook_helper, "_PROCESS_START_MONOTONIC", time.monotonic() - 60)
    calls = _stub_gate_refuter(monkeypatch, {"0": "refuted"})

    out, _ = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[Finding(message="kept anyway", confidence=0.9, file="src/Widget.ts", line=1)],
    )

    assert calls["run_batch"] == 0
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "kept anyway" in ctx
    assert "refuted and dropped" not in ctx  # no banner when VERIFY did not run


def test_verify_kill_switch_passes_findings_through(make_trusted_repo, monkeypatch):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    _fresh_budget(monkeypatch)
    calls = _stub_gate_refuter(monkeypatch, {"0": "refuted"})

    out, _ = _run_stop(
        _payload(repo, sid),
        env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_STOP_VERIFY": "0"},
        findings=[Finding(message="kept anyway", confidence=0.9, file="src/Widget.ts", line=1)],
    )

    assert calls["run_batch"] == 0
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "kept anyway" in ctx
    assert "refuted and dropped" not in ctx
