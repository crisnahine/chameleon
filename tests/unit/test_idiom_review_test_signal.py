"""Tests for the test-run signal folded into the Stop idiom-review gate.

When the turn edited a real source file (not just tests/docs) and no passing
test runner was recorded in the session's exec log, the idiom-review directive
gains a soft "run the suite" line. The signal never blocks on its own and never
gates the idiom review; it only strengthens the directive text in enforce mode
and emits a separate would-block metric in shadow mode.

Reuses the make_trusted_repo harness shape from test_idiom_review.py and seeds
the HMAC exec log directly so the gate's session_test_run_seen read has real,
signed lines to consume.
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

_REPO_ID = "idiom_repo_id"


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory(*, mode: str = "enforce", stop_block_cap: int = 3):
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps({"enforcement": {"mode": mode, "stop_block_cap": stop_block_cap}}),
            encoding="utf-8",
        )
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )

        data_dir = tmp_path / _REPO_ID
        data_dir.mkdir(parents=True, exist_ok=True)

        src = repo / "src" / "Widget.ts"
        src.parent.mkdir(parents=True, exist_ok=True)
        session_id = "s-idiom"

        from chameleon_mcp.profile.trust import hash_profile

        trust_rec = MagicMock()
        trust_rec.grants_root.return_value = True
        trust_rec.hash_for_root.side_effect = lambda root: hash_profile(profile_dir)

        stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
        stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=_REPO_ID))
        stack.enter_context(
            patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec)
        )
        stack.enter_context(
            patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None)
        )
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path)
        )

        return repo, data_dir, session_id, str(src), profile_dir

    try:
        yield _factory
    finally:
        stack.close()


def _env(tmp_path: Path) -> dict:
    return {
        "CHAMELEON_ENFORCE": "1",
        "CHAMELEON_HMAC_KEY_PATH": str(tmp_path / "hmac.key"),
        "TMPDIR": str(tmp_path),
    }


def _run_stop(payload, env):
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
        patch(
            "chameleon_mcp.hook_helper._stop_file_still_blockable",
            return_value=False,
        ),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return json.loads(s) if s else {}


def _touch(file_path: str, data_dir: Path, session_id: str, content: str = "export const C = 1\n"):
    Path(file_path).write_text(content, encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState()
    save_state(st, data_dir, session_id)


def _idioms(profile_dir: Path):
    profile_dir.joinpath("idioms.md").write_text(
        "- Always wrap DB calls in a transaction.\n", encoding="utf-8"
    )


def _seed_exec(tmp_path: Path, session_id: str, command: str, exit_code: int):
    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(_REPO_ID, session_id=session_id, command=command, exit_code=exit_code)


def test_source_edit_no_test_strengthens_directive(make_trusted_repo, tmp_path):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    _touch(file_path, data_dir, sid)
    _idioms(profile_dir)
    # No exec log at all -> no passing test seen.

    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env=_env(tmp_path),
    )
    assert out.get("decision") == "block"
    assert "test run" in out.get("reason", "").lower()
    assert "run the suite" in out.get("reason", "").lower()


def test_passing_test_run_suppresses_nudge(make_trusted_repo, tmp_path):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    _touch(file_path, data_dir, sid)
    _idioms(profile_dir)
    _seed_exec(tmp_path, sid, "pytest -q", 0)

    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env=_env(tmp_path),
    )
    # Idiom review still fires, but the test-run line is gone.
    assert out.get("decision") == "block"
    assert "run the suite" not in out.get("reason", "").lower()


def test_failing_test_run_still_nudges(make_trusted_repo, tmp_path):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    _touch(file_path, data_dir, sid)
    _idioms(profile_dir)
    _seed_exec(tmp_path, sid, "pytest", 1)  # non-zero: not a passing run

    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env=_env(tmp_path),
    )
    assert out.get("decision") == "block"
    assert "run the suite" in out.get("reason", "").lower()


def test_only_test_file_edited_no_nudge(make_trusted_repo, tmp_path):
    repo, data_dir, sid, _file_path, profile_dir = make_trusted_repo(mode="enforce")
    test_file = str(repo / "src" / "Widget.test.ts")
    _touch(test_file, data_dir, sid)
    _idioms(profile_dir)
    # No test command ran, but only a test file was edited -> no source nudge.

    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env=_env(tmp_path),
    )
    assert out.get("decision") == "block"
    assert "run the suite" not in out.get("reason", "").lower()


def test_shadow_mode_emits_test_run_metric(make_trusted_repo, tmp_path):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    _touch(file_path, data_dir, sid)
    _idioms(profile_dir)

    seen = []
    with patch(
        "chameleon_mcp.metrics.emit_hook_metric",
        side_effect=lambda hook, **kw: seen.append(hook),
    ):
        out = _run_stop(
            {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
            env=_env(tmp_path),
        )
    assert out.get("decision") != "block"
    assert "stop-test-run-signal" in seen


def test_shadow_mode_no_metric_when_test_ran(make_trusted_repo, tmp_path):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    _touch(file_path, data_dir, sid)
    _idioms(profile_dir)
    _seed_exec(tmp_path, sid, "pnpm test", 0)

    seen = []
    with patch(
        "chameleon_mcp.metrics.emit_hook_metric",
        side_effect=lambda hook, **kw: seen.append(hook),
    ):
        _run_stop(
            {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
            env=_env(tmp_path),
        )
    assert "stop-test-run-signal" not in seen
