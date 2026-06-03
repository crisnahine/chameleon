"""Stop / SubagentStop backstop tests for stop_backstop().

The backstop refuses to end the turn while a touched file still holds an
unresolved hard-class violation (FileState.blockable_unresolved at L2). It is
bounded by the stop_hook_active flag, the per-session stop_block_cap, the
enforcement mode (only "enforce" blocks), and the CHAMELEON_ENFORCE kill switch.

Isolation follows the sibling enforcement tests (no shared conftest): the
make_trusted_repo factory builds a real repo + config + plugin-data dir under
tmp_path and patches repo/trust/suppression resolution for the duration of the
stop_backstop call, so the handler reaches the real EnforcementState on disk.
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


@pytest.fixture
def make_trusted_repo(tmp_path):
    """Factory: a trusted repo with an enforcement config and an isolated data dir.

    Returns ``(repo, data_dir, session_id, file_path, profile_dir)``. The repo's
    resolution (find_repo_root / _compute_repo_id), trust grant, suppression
    check, and plugin-data dir are all patched so stop_backstop sees a trusted,
    non-suppressed repo and loads the EnforcementState saved under ``data_dir``.
    """
    stack = ExitStack()

    def _factory(*, mode: str = "enforce", stop_block_cap: int = 3):
        repo_id = "stop_repo_id"
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps({"enforcement": {"mode": mode, "stop_block_cap": stop_block_cap}}),
            encoding="utf-8",
        )

        data_dir = tmp_path / repo_id
        data_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        session_id = "s-stop"

        trust_rec = MagicMock()
        trust_rec.grants_root.return_value = True

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


def _run_stop(payload, env):
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return json.loads(s) if s else {}


def test_stop_hook_active_bails(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    st = EnforcementState()
    st.files[file_path] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)
    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": True},
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out == {}  # never re-block while already continuing


def test_unresolved_blockable_blocks_stop(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)
    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") == "block"


def test_cap_reached_allows_stop(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(
        mode="enforce", stop_block_cap=1
    )
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.stop_hook_blocks = 1
    st.files[file_path] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)
    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") != "block"


def test_shadow_does_not_block_stop(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)
    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") != "block"
