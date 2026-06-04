"""The Stop backstop loads the profile once and reuses it across candidates.

Re-checking N unresolved files must not re-read the profile N times. The backstop
preloads it once and threads it into each per-file re-lint via the ``loaded``
argument, so _lint_file_in_process never re-reads the profile per candidate.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState, save_state


@pytest.fixture
def trusted_repo(tmp_path):
    stack = ExitStack()
    repo_id = "stop_reuse_repo_id"
    repo = tmp_path / "repo"
    profile_dir = repo / ".chameleon"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.joinpath("config.json").write_text(
        json.dumps({"enforcement": {"mode": "enforce", "stop_block_cap": 10}}),
        encoding="utf-8",
    )
    profile_dir.joinpath("profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    data_dir = tmp_path / repo_id
    data_dir.mkdir(parents=True, exist_ok=True)

    from chameleon_mcp.profile.trust import hash_profile

    trust_rec = MagicMock()
    trust_rec.grants_root.return_value = True
    trust_rec.hash_for_root.return_value = hash_profile(profile_dir)

    stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
    stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id))
    stack.enter_context(
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec)
    )
    stack.enter_context(patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None))
    stack.enter_context(patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path))
    try:
        yield repo, data_dir, repo_id
    finally:
        stack.close()


def test_backstop_reuses_one_loaded_profile_across_candidates(trusted_repo):
    repo, data_dir, repo_id = trusted_repo
    sid = "s-reuse"

    # Three unresolved L2 candidates.
    st = EnforcementState()
    paths = []
    for i in range(3):
        fp = repo / "src" / f"W{i}.ts"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("export const C = 1\n", encoding="utf-8")
        st.files[str(fp)] = FileState(level=2, blockable_unresolved=True)
        paths.append(str(fp))
    save_state(st, data_dir, sid)

    sentinel = object()
    seen_loaded = []

    # Stub the real re-lint helper: record the ``loaded`` argument each call gets.
    def fake_blockable(
        repo_root, file_path, loaded=None, active=None, daemon_state=None, out_rules=None, level=2
    ):
        seen_loaded.append(loaded)
        return True

    payload = {"session_id": sid, "cwd": str(repo), "stop_hook_active": False}
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, {"CHAMELEON_ENFORCE": "1"}, clear=False),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=sentinel),
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", side_effect=fake_blockable),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()

    # Every candidate got the SAME preloaded profile object, not a fresh load.
    assert len(seen_loaded) == 3
    assert all(x is sentinel for x in seen_loaded)


def test_lint_in_process_uses_passed_loaded_without_reloading(tmp_path):
    """_lint_file_in_process must not call load_profile_dir when handed a profile."""
    from chameleon_mcp.profile.loader import LoadedProfile

    loaded = LoadedProfile(
        profile={},
        archetypes={},
        canonicals={"canonicals": {}},
        rules={},
        conventions={"conventions": {}},
        idioms_text="",
        generation=1,
        profile_dir=tmp_path / ".chameleon",
    )

    load_calls = MagicMock()
    with patch("chameleon_mcp.profile.loader.load_profile_dir", load_calls):
        from chameleon_mcp.hook_helper import _lint_file_in_process

        _lint_file_in_process(
            tmp_path, "component", "export const C = 1\n", str(tmp_path / "x.ts"), loaded=loaded
        )

    load_calls.assert_not_called()
