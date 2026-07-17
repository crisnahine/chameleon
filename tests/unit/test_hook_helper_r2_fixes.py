"""R2-hooks defect fixes for hook_helper.py.

Covers, in one module, the behaviors that were previously unverified:
  - trust-prompt marker dir/file are created owner-only (0o700 / 0o600)
  - the statusline settings.local.json write goes through the atomic helper
  - a PreToolUse enforce-deny still records the archetype as seen, so a later
    successful edit to the same archetype is not re-shown as first-in-archetype
  - the Stop backstop loads the repo's block rules once and threads them into
    every per-candidate re-lint instead of re-reading enforcement.json per file
  - the Stop backstop drops state entries for files that no longer exist on disk
  - the Stop backstop's per-file daemon fallback skips the daemon for the rest of
    the pass once a call comes back empty, so a hung daemon cannot stack timeouts
"""

from __future__ import annotations

import io
import json
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState, load_state, save_state

# --- trust-prompt marker permissions ---------------------------------------


def test_trust_prompt_marker_is_owner_only(tmp_path):
    from chameleon_mcp import hook_helper

    with patch.object(hook_helper, "_plugin_data_dir", return_value=tmp_path):
        assert hook_helper._should_emit_untrusted_prompt("repo-x", "sess-1") is True

    marker_dir = tmp_path / "repo-x"
    assert marker_dir.is_dir()
    assert stat.S_IMODE(marker_dir.stat().st_mode) == 0o700

    markers = list(marker_dir.iterdir())
    assert markers, "trust-prompt marker file was not created"
    assert stat.S_IMODE(markers[0].stat().st_mode) == 0o600


# --- settings.local.json atomic write --------------------------------------


def test_settings_local_json_write_is_atomic(tmp_path, monkeypatch):
    """The statusline wiring must write settings.local.json via _atomic_write_text."""
    from chameleon_mcp import hook_helper

    repo = tmp_path / "repo"
    plugin_root = tmp_path / "plugin"
    (plugin_root / "bin").mkdir(parents=True)
    script = plugin_root / "bin" / "chameleon-statusline.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    seen: list[Path] = []
    real_atomic = hook_helper._atomic_write_text

    def _spy(path, text):
        seen.append(Path(path))
        return real_atomic(path, text)

    monkeypatch.setattr(hook_helper, "_atomic_write_text", _spy)

    hook_helper._wire_statusline_settings(repo, str(plugin_root))

    local_settings = repo / ".claude" / "settings.local.json"
    assert local_settings.is_file()
    assert local_settings in seen, "settings.local.json was not written atomically"
    data = json.loads(local_settings.read_text(encoding="utf-8"))
    assert data["statusLine"]["command"] == str(script)


def test_settings_local_dir_created_owner_only(tmp_path):
    from chameleon_mcp import hook_helper

    repo = tmp_path / "repo"
    plugin_root = tmp_path / "plugin"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "bin" / "chameleon-statusline.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    hook_helper._wire_statusline_settings(repo, str(plugin_root))

    claude_dir = repo / ".claude"
    assert claude_dir.is_dir()
    assert stat.S_IMODE(claude_dir.stat().st_mode) == 0o700


# --- PreToolUse deny records archetype as seen -----------------------------


def test_pretool_deny_seeds_archetypes_seen(tmp_path):
    """A denied edit must still record its archetype in the enforcement state.

    Otherwise a later successful edit to the same archetype is treated as the
    first one seen and re-shown the verbose Tier-2 advisory.
    """
    from chameleon_mcp import hook_helper

    repo_id = "deny_repo_id"
    repo_data = tmp_path / repo_id
    repo_data.mkdir(parents=True)
    session_id = "sess-deny"

    with patch.object(hook_helper, "_plugin_data_dir", return_value=tmp_path):
        hook_helper._seed_archetype_seen(repo_id, session_id, "component")

    state = load_state(repo_data, session_id)
    assert "component" in state.archetypes_seen


# --- Stop backstop loads block rules once ----------------------------------


@pytest.fixture
def trusted_repo(tmp_path):
    stack = ExitStack()
    repo_id = "r2_stop_repo_id"
    repo = tmp_path / "repo"
    profile_dir = repo / ".chameleon"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "COMMITTED").write_text("committed-at=1\npid=1\n", encoding="utf-8")
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


def _drive_stop(repo, sid, *, blockable=True):
    cap: list[str] = []
    payload = {"session_id": sid, "cwd": str(repo), "stop_hook_active": False}
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, {"CHAMELEON_ENFORCE": "1"}, clear=False),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=object()),
        patch(
            "chameleon_mcp.hook_helper._stop_file_still_blockable",
            return_value=blockable,
        ),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return json.loads(s) if s else {}


def test_backstop_loads_block_rules_once(trusted_repo):
    repo, data_dir, repo_id = trusted_repo
    sid = "s-rules-once"

    st = EnforcementState()
    for i in range(3):
        fp = repo / "src" / f"W{i}.ts"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("export const C = 1\n", encoding="utf-8")
        st.files[str(fp)] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)

    seen_active = []

    def fake_blockable(
        repo_root, file_path, loaded=None, active=None, daemon_state=None, out_rules=None, level=2
    ):
        seen_active.append(active)
        return True

    cap: list[str] = []
    payload = {"session_id": sid, "cwd": str(repo), "stop_hook_active": False}
    sentinel = {"phantom-import"}
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, {"CHAMELEON_ENFORCE": "1"}, clear=False),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=object()),
        patch(
            "chameleon_mcp.enforcement_calibration.active_block_rules",
            return_value=sentinel,
        ) as active_mock,
        patch(
            "chameleon_mcp.hook_helper._stop_file_still_blockable",
            side_effect=fake_blockable,
        ),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()

    # active_block_rules read enforcement.json once for the whole pass, not per file.
    assert active_mock.call_count == 1
    assert len(seen_active) == 3
    assert all(a is sentinel for a in seen_active)


def test_backstop_drops_deleted_file_entries(trusted_repo):
    repo, data_dir, repo_id = trusted_repo
    sid = "s-deleted"

    alive = repo / "src" / "Alive.ts"
    alive.parent.mkdir(parents=True, exist_ok=True)
    alive.write_text("export const C = 1\n", encoding="utf-8")
    ghost = str(repo / "src" / "Ghost.ts")  # never created on disk

    st = EnforcementState()
    st.files[str(alive)] = FileState(level=2, blockable_unresolved=True)
    st.files[ghost] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)

    _drive_stop(repo, sid, blockable=True)

    after = load_state(data_dir, sid)
    assert ghost not in after.files, "deleted file entry should be pruned from state"
    assert str(alive) in after.files


def test_backstop_skips_daemon_after_first_miss(trusted_repo):
    """A hung daemon must not stack per-file timeouts across the candidate loop."""
    from chameleon_mcp import hook_helper

    repo, data_dir, repo_id = trusted_repo
    sid = "s-daemon-skip"

    st = EnforcementState()
    for i in range(4):
        fp = repo / "src" / f"D{i}.ts"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("export const C = 1\n", encoding="utf-8")
        st.files[str(fp)] = FileState(level=2, blockable_unresolved=True)
    save_state(st, data_dir, sid)

    calls = {"n": 0}

    def fake_call(method, payload=None, **kwargs):
        calls["n"] += 1
        return None  # daemon unreachable / timed out

    cap: list[str] = []
    payload = {"session_id": sid, "cwd": str(repo), "stop_hook_active": False}
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, {"CHAMELEON_ENFORCE": "1"}, clear=False),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=object()),
        patch("chameleon_mcp.daemon_client.call", side_effect=fake_call),
        patch("chameleon_mcp.hook_helper._lint_file_in_process", return_value=[]),
        patch("chameleon_mcp.tools.get_archetype", return_value={"data": {}}),
    ):
        out.write = cap.append
        hook_helper.stop_backstop()

    # The daemon was tried at most once for the whole pass; after it came back
    # empty the remaining files went straight to the in-process path.
    assert calls["n"] <= 1


# --------------------------------------------------------------------------
# _ignore_hint — block messages must hand each language its own comment token;
# a Ruby developer told to add `// chameleon-ignore` gets a directive that is
# a syntax error in .rb (qa25 P2)

from chameleon_mcp import hook_helper as hh  # noqa: E402


class TestIgnoreHintLanguageAware:
    def test_ruby_file_gets_hash_token(self):
        assert (
            hh._ignore_hint("/repo/app/models/user.rb", "naming-convention-violation")
            == "`# chameleon-ignore naming-convention-violation`"
        )

    def test_ts_file_gets_slash_token(self):
        assert (
            hh._ignore_hint("/repo/src/api.ts", "import-preference-violation")
            == "`// chameleon-ignore import-preference-violation`"
        )

    def test_default_rule_placeholder(self):
        assert hh._ignore_hint("/repo/src/api.tsx") == "`// chameleon-ignore <rule>`"

    def test_mixed_language_paths_show_both_forms(self):
        hint = hh._ignore_hint(["/r/a.ts", "/r/b.rb"])
        assert "`// chameleon-ignore <rule>`" in hint
        assert "`# chameleon-ignore <rule>` in Ruby" in hint

    def test_all_ruby_paths_show_hash_only(self):
        hint = hh._ignore_hint(["/r/a.rb", "/r/b.rake"])
        assert hint == "`# chameleon-ignore <rule>`"

    def test_unknown_extension_defaults_to_slash(self):
        assert hh._ignore_hint("/r/Makefile") == "`// chameleon-ignore <rule>`"

    def test_none_and_empty_default_to_slash(self):
        assert hh._ignore_hint(None) == "`// chameleon-ignore <rule>`"
        assert hh._ignore_hint([]) == "`// chameleon-ignore <rule>`"
