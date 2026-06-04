"""Stop-hook correctness-judge gate tests for stop_backstop().

The judge gate runs on the no-block stop path, after the idiom gate declines to
block. It is opt-in (`enforcement.correctness_judge`, default off), runs at most
once per session, and is ADVISORY ONLY: it never returns a Stop block, only
`additionalContext` carrying the reviewer's findings. The real `claude -p` spawn
is mocked here via judge.run_correctness_judge.

Isolation mirrors test_idiom_review: a real repo + config + plugin-data dir under
tmp_path with repo/trust/suppression resolution patched, and the lint cold-path
forced clean so the judge gate is the only thing reached.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState, save_state
from chameleon_mcp.judge import Finding


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory(*, mode: str = "enforce", correctness_judge: bool = True):
        repo_id = "judge_repo_id"
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
                        "correctness_judge": correctness_judge,
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

        session_id = "s-judge"

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


def _run_stop(payload, env, *, findings=None):
    cap = []
    rcj = patch(
        "chameleon_mcp.judge.run_correctness_judge",
        return_value=findings if findings is not None else [],
    )
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
        patch(
            "chameleon_mcp.hook_helper._stop_file_still_blockable",
            return_value=False,
        ),
        rcj as mock_rcj,
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return (json.loads(s) if s else {}), mock_rcj


def _touch_edited_file(file_path: str, data_dir: Path, session_id: str, content: str = "x = 1\n"):
    Path(file_path).write_text(content, encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState()
    save_state(st, data_dir, session_id)


def test_judge_findings_emit_advisory_context_never_block(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    findings = [
        Finding(message="dropped await on save()", confidence=0.9, file="src/Widget.ts", line=12)
    ]

    out, mock_rcj = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
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
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[],
    )
    mock_rcj.assert_called_once()
    assert out == {}


def test_judge_disabled_by_default_not_spawned(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(correctness_judge=False)
    _touch_edited_file(file_path, data_dir, sid)

    out, mock_rcj = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[Finding(message="x", confidence=0.9)],
    )
    mock_rcj.assert_not_called()
    assert out == {}


def test_judge_runs_once_per_session(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)
    findings = [Finding(message="bug", confidence=0.7)]

    out1, mock1 = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
    )
    assert mock1.call_count == 1
    assert "additionalContext" in out1.get("hookSpecificOutput", {})

    # Marker now present: second turn must not re-spawn the reviewer.
    out2, mock2 = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=findings,
    )
    mock2.assert_not_called()
    assert out2 == {}


def test_judge_off_mode_not_spawned(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="off")
    _touch_edited_file(file_path, data_dir, sid)

    out, mock_rcj = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
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
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
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
            {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
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
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        findings=[Finding(message="x", confidence=0.9)],
    )
    mock_rcj.assert_not_called()
    assert out == {}


def test_judge_fails_open_when_run_raises(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _touch_edited_file(file_path, data_dir, sid)

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
            "chameleon_mcp.judge.run_correctness_judge",
            side_effect=RuntimeError("spawn exploded"),
        ),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    result = json.loads(s) if s else {}
    # Fail open: no crash, no block, valid JSON.
    assert result.get("decision") != "block"
