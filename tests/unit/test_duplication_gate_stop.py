"""Stop-hook turn-end duplication-gate tests for stop_backstop().

The duplication gate runs on the no-block stop path, after the idiom and
correctness gates. On by default (`enforcement.duplication_review`, set false to
opt out), mode-gated (shadow/enforce), and ADVISORY ONLY: it never returns a Stop
block, only `additionalContext` naming functions the turn re-implements. It is
heavily bounded -- skipped on SubagentStop, skipped when the correctness judge is
already spawning this Stop, capped per session, and per-(file, content-digest)
deduplicated. The body-match gather and the real `claude -p` confirm spawn are
mocked here; the wiring (gating, cap-increment + persist, mark_judged, the
single-emit fold) is exercised end-to-end through stop_backstop.

Isolation mirrors test_correctness_judge_gate: a real repo + config + plugin-data
dir under tmp_path with repo/trust/suppression resolution patched, the lint
cold-path forced clean, and correctness_judge off so the duplication gate is the
surface under test.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.duplication_review import Finding as DupFinding
from chameleon_mcp.enforcement import EnforcementState, FileState, load_state, save_state


@pytest.fixture(autouse=True)
def _isolate_metrics(tmp_path, monkeypatch):
    """Keep emit_hook_metric (env-resolved) off the developer's real metrics.jsonl."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "metrics-isolated"))


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory(
        *,
        mode: str = "shadow",
        duplication_review: bool = True,
        correctness_judge: bool = False,
    ):
        repo_id = "dup_repo_id"
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps(
                {
                    "enforcement": {
                        "mode": mode,
                        # idiom_review off so the idiom gate never blocks first and
                        # the duplication gate is reached.
                        "idiom_review": False,
                        # correctness_judge off by default in these tests so its
                        # spawn does not suppress the duplication gate (the gate
                        # defers when the correctness judge fires this Stop).
                        "correctness_judge": correctness_judge,
                        "duplication_review": duplication_review,
                    }
                }
            ),
            encoding="utf-8",
        )
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )

        data_dir = tmp_path / repo_id
        data_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        session_id = "s-dup"

        from chameleon_mcp.profile.trust import hash_profile

        trust_rec = MagicMock()
        trust_rec.grants_root.return_value = True
        trust_rec.hash_for_root.side_effect = lambda root: hash_profile(profile_dir)

        stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
        stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id))
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


def _seed_edited(
    file_path: str, data_dir: Path, session_id: str, content: str = "export const C = 1\n"
):
    Path(file_path).write_text(content, encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState()
    save_state(st, data_dir, session_id)


def _planted_finding(file_path: str, repo) -> DupFinding:
    rel = Path(file_path).resolve().relative_to(Path(repo).resolve()).as_posix()
    return DupFinding(
        new_name="toDisplayDate",
        new_file=rel,
        line=7,
        excerpt="return d.toISOString()",
        existing_name="formatDate",
        existing_file="src/dates.ts",
    )


def _result_line(payload: str) -> str:
    """A claude -p stream-json result line carrying ``payload`` as the result."""
    return json.dumps({"type": "result", "result": payload}) + "\n"


def _run_stop(
    payload,
    env,
    *,
    findings=None,
    confirm=True,
):
    """Drive stop_backstop with the body-match gather and the judge spawn mocked.

    ``findings`` is the list gather returns (default: none -> clean turn).
    ``confirm`` controls the mocked reviewer verdict for those findings.
    Returns (emitted_json, spawn_mock, raw_emit_count).
    """
    cap = []
    findings = findings if findings is not None else []
    # The reviewer confirms each finding by new_name when ``confirm`` is True.
    verdict = json.dumps(
        [{"new_name": f.new_name, "is_duplicate": bool(confirm)} for f in findings]
    )
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", return_value=False),
        patch(
            "chameleon_mcp.duplication_review.gather_body_match_findings",
            return_value=findings,
        ),
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
        patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(verdict)) as spawn,
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    raw = "".join(cap).strip()
    # Count distinct top-level JSON objects emitted (single-emit property).
    emit_count = 0
    if raw:
        decoder = json.JSONDecoder()
        idx = 0
        s = raw
        while idx < len(s):
            while idx < len(s) and s[idx].isspace():
                idx += 1
            if idx >= len(s):
                break
            _, end = decoder.raw_decode(s, idx)
            emit_count += 1
            idx = end
    return (json.loads(raw) if raw else {}), spawn, emit_count


def test_duplication_advisory_fires_and_single_emit(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out, spawn, emit_count = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=True,
    )

    # Advisory only: never a block.
    assert out.get("decision") != "block"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "re-implements" in ctx
    assert "toDisplayDate" in ctx
    assert "formatDate" in ctx
    spawn.assert_called_once()
    # Exactly one JSON object emitted for the whole Stop.
    assert emit_count == 1


def test_subagentstop_skips_no_spawn(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out, spawn, _ = _run_stop(
        {
            "session_id": sid,
            "cwd": str(repo),
            "stop_hook_active": False,
            "hook_event_name": "SubagentStop",
        },
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=True,
    )

    spawn.assert_not_called()
    assert out == {}


def test_clean_turn_no_spawn_no_advisory(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)

    out, spawn, _ = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[],  # no body match
    )

    spawn.assert_not_called()
    assert out == {}


def test_duplication_disabled_no_advisory(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(duplication_review=False)
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out, spawn, _ = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=True,
    )

    spawn.assert_not_called()
    assert out == {}


def test_off_mode_no_advisory(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="off")
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out, spawn, _ = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=True,
    )

    spawn.assert_not_called()
    assert out == {}


def test_confirm_false_emits_no_advisory(make_trusted_repo):
    # A body match the judge declines is not surfaced; the spawn still happens
    # (the gather found candidates) and the file is marked judged.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out, spawn, _ = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=False,
    )

    spawn.assert_called_once()
    assert out == {}


def test_spawn_counted_and_persisted_before_judge(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=True,
    )

    # The spawn was counted and persisted to disk.
    persisted = load_state(data_dir, sid)
    assert persisted.duplication_spawns == 1


def test_repeat_unchanged_turn_does_not_respawn(make_trusted_repo):
    # Same content twice: the second turn's file is already judged at its digest,
    # so no second spawn fires (the marker suppresses it).
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out1, spawn1, _ = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=True,
    )
    spawn1.assert_called_once()
    assert "re-implements" in out1["hookSpecificOutput"]["additionalContext"]

    # Re-arm the edit record (the prior pass pruned/kept state); content unchanged.
    st = load_state(data_dir, sid)
    st.files[file_path] = FileState()
    save_state(st, data_dir, sid)

    out2, spawn2, _ = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=True,
    )
    spawn2.assert_not_called()
    assert out2 == {}


def test_spawn_cap_blocks_further_advisories(make_trusted_repo):
    # When duplication_spawns is already at the cap, no spawn fires.
    from chameleon_mcp._thresholds import threshold_int

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.duplication_spawns = threshold_int("DUPLICATION_REVIEW_MAX_SPAWNS_PER_SESSION")
    st.files[file_path] = FileState()
    save_state(st, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out, spawn, _ = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
        confirm=True,
    )

    spawn.assert_not_called()
    assert out == {}


def test_correctness_judge_spawning_defers_duplication(make_trusted_repo):
    # When the correctness judge fires this Stop, the duplication gate defers so a
    # turn never pays for two reviewer model spawns. The correctness judge's own
    # spawn is mocked clean (no findings), so the only emit, if any, would be the
    # duplication advisory -- and it must not appear.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(correctness_judge=True)
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    cap = []
    with (
        patch(
            "sys.stdin",
            io.StringIO(
                json.dumps({"session_id": sid, "cwd": str(repo), "stop_hook_active": False})
            ),
        ),
        patch("sys.stdout") as out,
        patch.dict(os.environ, {"CHAMELEON_ENFORCE": "1"}, clear=False),
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", return_value=False),
        patch("chameleon_mcp.judge.run_correctness_judge", return_value=[]),
        patch(
            "chameleon_mcp.duplication_review.gather_body_match_findings",
            return_value=findings,
        ) as gather,
        patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line("[]")) as dup_spawn,
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()

    raw = "".join(cap).strip()
    result = json.loads(raw) if raw else {}
    # The duplication gate returned early before gathering or spawning.
    gather.assert_not_called()
    dup_spawn.assert_not_called()
    assert result == {}


def test_fails_open_when_gather_raises(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)

    cap = []
    with (
        patch(
            "sys.stdin",
            io.StringIO(
                json.dumps({"session_id": sid, "cwd": str(repo), "stop_hook_active": False})
            ),
        ),
        patch("sys.stdout") as out,
        patch.dict(os.environ, {"CHAMELEON_ENFORCE": "1"}, clear=False),
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", return_value=False),
        patch(
            "chameleon_mcp.duplication_review.gather_body_match_findings",
            side_effect=RuntimeError("gather exploded"),
        ),
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()

    raw = "".join(cap).strip()
    result = json.loads(raw) if raw else {}
    # Fail open: no crash, no block, valid JSON.
    assert result.get("decision") != "block"
