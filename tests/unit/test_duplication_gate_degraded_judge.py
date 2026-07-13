"""The duplication gate must not be starved by a FAILED correctness-judge spawn.

The gate defers whenever the judge spawns this Stop so a turn never pays for
two reviewer model calls. That deferral is only honest when the judge spawn
actually produced a reviewable result: a permanently failing spawn (auth
broken, binary missing, timeout) routed every Stop and therefore suppressed
duplication review forever. These tests pin the repaired contract: a degraded
spawn lets the duplication gate run this same Stop; a healthy spawn still
defers.

Harness mirrors test_duplication_gate_stop: real repo + config + plugin-data
dir under tmp_path, repo/trust/suppression resolution patched, the lint cold
path forced clean, and correctness_judge ON so its spawn outcome drives the
deferral decision.
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
from chameleon_mcp.enforcement import EnforcementState, FileState, save_state


@pytest.fixture(autouse=True)
def _isolate_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "metrics-isolated"))
    # exec_log (the "no passing test run" reminder's session_test_run_seen)
    # reads/writes under ${TMPDIR}/.chameleon_exec_log/<repo_id>/ with an
    # HMAC key at CHAMELEON_HMAC_KEY_PATH (fail-loud, no unsigned mode) --
    # isolate both so this never touches the developer's real key/tmp state.
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # A known bare-auth failure routes the judge through launch_async_judge,
    # so the sync run_correctness_judge mock would never fire. The marker dir
    # is already isolated above; also reset the process-global cache, which an
    # earlier test in the same process can poison from its own marker read.
    monkeypatch.delenv("CHAMELEON_JUDGE_ASYNC", raising=False)
    from chameleon_mcp import judge

    monkeypatch.setattr(judge, "_BARE_AUTH_OK", None, raising=False)
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", False, raising=False)


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory():
        repo_id = "dup_degraded_repo"
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps(
                {
                    "enforcement": {
                        "mode": "shadow",
                        "idiom_review": False,
                        # multi_lens_review (default-on) merges correctness +
                        # duplication into one pass; disable it so this test
                        # exercises the separate correctness/duplication gates.
                        "multi_lens_review": False,
                        "correctness_judge": True,
                        "duplication_review": True,
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

        session_id = "s-dup-degraded"

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

        # idiom_review=False only silences idiom/principle content; the
        # independent "no passing test run" reminder still fires for a real
        # source edit with no recorded test run. Record one so this gate's own
        # surface (correctness-judge/duplication deferral) is reached
        # undisturbed, mirroring test_idiom_review.py's isolation for the same
        # reminder.
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(repo_id, session_id=session_id, command="pytest -q", exit_code=0)

        return repo, data_dir, session_id, file_path

    try:
        yield _factory
    finally:
        stack.close()


def _seed_edited(file_path: str, data_dir: Path, session_id: str):
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
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
    return json.dumps({"type": "result", "result": payload}) + "\n"


def _run_stop(repo, sid, *, corr_judge_behavior, findings):
    """Drive stop_backstop with the correctness judge mocked per behavior.

    ``corr_judge_behavior`` is the side_effect/return for
    judge.run_correctness_judge; the duplication confirm spawn is mocked to
    confirm every planted finding.
    """
    cap: list[str] = []
    verdict = json.dumps([{"new_name": f.new_name, "is_duplicate": True} for f in findings])
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
        patch("chameleon_mcp.judge.run_correctness_judge", side_effect=corr_judge_behavior),
        patch(
            "chameleon_mcp.duplication_review.gather_body_match_findings",
            return_value=findings,
        ) as gather,
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
        patch(
            "chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(verdict)
        ) as dup_spawn,
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    raw = "".join(cap).strip()
    return (json.loads(raw) if raw else {}), gather, dup_spawn


def _degraded_run(*args, **kwargs):
    """A judge pipeline whose reviewer spawn failed (nonzero exit)."""
    sink = kwargs.get("event_sink")
    if sink is not None:
        sink("spawn_nonzero_exit")
    return []


def test_degraded_judge_spawn_lets_duplication_gate_run(make_trusted_repo):
    repo, data_dir, sid, file_path = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out, gather, dup_spawn = _run_stop(
        repo, sid, corr_judge_behavior=_degraded_run, findings=findings
    )

    gather.assert_called_once()
    dup_spawn.assert_called_once()
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "re-implements" in ctx
    assert "toDisplayDate" in ctx
    assert out.get("decision") != "block"


def test_judge_timeout_skips_duplication_gate_to_stay_in_budget(make_trusted_repo):
    repo, data_dir, sid, file_path = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    def timed_out(*args, **kwargs):
        sink = kwargs.get("event_sink")
        if sink is not None:
            sink("spawn_timeout")
        return []

    out, gather, dup_spawn = _run_stop(repo, sid, corr_judge_behavior=timed_out, findings=findings)

    # A judge that fails by TIMEOUT already consumed the full wall-clock budget;
    # spawning the duplication reviewer after it would blow the 55s cap and be
    # SIGKILLed mid-review, so the gate skips the second spawn this turn. (The
    # fast unparseable-output failure, tested below, still runs duplication.)
    dup_spawn.assert_not_called()
    assert "re-implements" not in (out.get("hookSpecificOutput", {}).get("additionalContext", ""))


def test_unparseable_judge_output_lets_duplication_gate_run(make_trusted_repo):
    # The reviewer ran but produced no reviewable verdict; deferral would
    # still starve the gate, so it runs.
    repo, data_dir, sid, file_path = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    def unparseable(*args, **kwargs):
        sink = kwargs.get("event_sink")
        if sink is not None:
            sink("unparseable_output")
        return []

    out, gather, dup_spawn = _run_stop(
        repo, sid, corr_judge_behavior=unparseable, findings=findings
    )

    gather.assert_called_once()
    dup_spawn.assert_called_once()


def test_healthy_judge_spawn_still_defers_duplication(make_trusted_repo):
    # Pin the existing contract: a clean spawn (no degradation sink calls)
    # keeps the deferral so a turn never fires two reviewers.
    repo, data_dir, sid, file_path = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)
    findings = [_planted_finding(file_path, repo)]

    out, gather, dup_spawn = _run_stop(
        repo, sid, corr_judge_behavior=lambda *a, **k: [], findings=findings
    )

    gather.assert_not_called()
    dup_spawn.assert_not_called()
    assert out == {}
