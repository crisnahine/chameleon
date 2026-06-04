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
        # profile.json gives hash_profile() a non-empty hash; the trust record
        # below mirrors it so the backstop's not-stale gate reads "trusted".
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )

        data_dir = tmp_path / repo_id
        data_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        session_id = "s-stop"

        from chameleon_mcp.profile.trust import hash_profile

        trust_rec = MagicMock()
        trust_rec.grants_root.return_value = True
        trust_rec.hash_for_root.return_value = hash_profile(profile_dir)

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


def _run_stop(payload, env, *, still_blockable: bool = True):
    """Drive stop_backstop. ``still_blockable`` stubs the cold-path live re-lint
    so these tests stay focused on the backstop's decision logic (cap, mode,
    stale gate) rather than the lint internals, which the false-positive battery
    covers end-to-end."""
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
        patch(
            "chameleon_mcp.hook_helper._stop_file_still_blockable",
            return_value=still_blockable,
        ),
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


def test_l0_blockable_file_is_a_recheck_candidate(make_trusted_repo):
    # A single-edit deterministic secret/phantom sits at L0 with the cached
    # blockable flag set. The candidate loop must re-check it regardless of level
    # so the documented turn-end refusal fires; the level gate lives inside the
    # re-lint (for archetype-dependent rules), not in the candidate filter.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=0, blockable_unresolved=True)
    save_state(st, data_dir, sid)
    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") == "block"


def test_l0_file_without_blockable_flag_is_skipped(make_trusted_repo):
    # The candidate filter still requires the cached flag: an L0 file the verifier
    # never armed (no hard violation) must not reach the re-lint and must not
    # block. still_blockable would say True if reached, so a non-block proves the
    # file was filtered out before the re-lint.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=0, blockable_unresolved=False)
    save_state(st, data_dir, sid)
    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") != "block"


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


def test_resolved_candidate_clears_flag_and_allows_stop(make_trusted_repo):
    # A candidate whose live re-lint comes back clean must not block, and its
    # stale blockable_unresolved flag must be cleared so it isn't re-checked.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)
    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        still_blockable=False,
    )
    assert out.get("decision") != "block"
    from chameleon_mcp.enforcement import load_state

    healed = load_state(data_dir, sid).files[file_path]
    assert healed.blockable_unresolved is False


def test_stale_trust_does_not_block_stop(make_trusted_repo):
    # A stale grant (granted hash no longer matches the live profile) must not
    # block even with an unresolved blockable violation on a still-live file.
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    Path(file_path).write_text("export const C = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)

    # Override the fixture's matching hash so the not-stale gate reads "stale".
    with patch("chameleon_mcp.profile.trust.hash_profile", return_value="DRIFTED-DOES-NOT-MATCH"):
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
