"""SessionStart cwd-independence + daemon-upgrade tests for hook_helper.py.

These pin three behaviors that break when Claude is launched from a subdirectory
of a repo, plus the daemon-upgrade stop:

  (a) The statusline cache is written under the repo root's `.claude/`, not the
      launch subdir's, so bin/chameleon-statusline.sh (which reads at repo root)
      sees live trust/activity state.
  (b) When the repo root has its own profile, that profile populates the cache
      even though cwd is a deeper subdir.
  (c) On a code upgrade (running package path != installed package path), the
      stale daemon is stopped even when no profile exists at the root, so the new
      hooks never connect to the old daemon.

Isolation: no conftest. Each test pins CHAMELEON_PLUGIN_DATA + CLAUDE_PLUGIN_ROOT,
stubs drift banner / auto-refresh, and sets cwd and find_repo_root to DIFFERENT
paths to model the subdir-launch case.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import chameleon_mcp.hook_helper as hh

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _run_session_start(*, cwd: Path, repo_root, home: Path, monkeypatch, trust_for=None):
    """Drive session_start with cwd and find_repo_root resolved independently.

    ``repo_root`` is what find_repo_root returns (None models no repo). When a
    repo_root is given, _trust_for is stubbed so the profile entry resolves to a
    fixed trust string without touching the real trust store.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(home / "data"))

    patches = [
        patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"}))),
        patch("pathlib.Path.cwd", return_value=cwd),
        patch("pathlib.Path.home", return_value=home),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None),
        patch("chameleon_mcp.hook_helper._drift_banner_for_repo", return_value=None),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo_root),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_cwd"),
    ]
    if trust_for is not None:
        patches.append(patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_for))

    captured: list[str] = []
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.write = captured.append
        stack = []
        try:
            for p in patches:
                p.__enter__()
                stack.append(p)
            rc = hh.session_start()
        finally:
            for p in reversed(stack):
                p.__exit__(None, None, None)
    return rc, "".join(captured)


def test_statusline_cache_written_at_repo_root_not_subdir(tmp_path, monkeypatch):
    """Launched from repo/sub, the statusline cache lands under repo/.claude,
    not repo/sub/.claude, matching where the statusline script reads it."""
    repo = tmp_path / "repo"
    sub = repo / "tests"
    sub.mkdir(parents=True)
    profile_dir = repo / ".chameleon"
    profile_dir.mkdir()
    (profile_dir / "profile.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    trust_rec = MagicMock()
    trust_rec.grants_root.return_value = True

    rc, _ = _run_session_start(
        cwd=sub, repo_root=repo, home=home, monkeypatch=monkeypatch, trust_for=trust_rec
    )
    assert rc == 0

    repo_cache = repo / ".claude" / ".chameleon-statusline-cache"
    sub_cache = sub / ".claude" / ".chameleon-statusline-cache"
    assert repo_cache.is_file(), "cache must be written under the repo root"
    assert not sub_cache.exists(), "cache must NOT be written under the launch subdir"
    data = json.loads(repo_cache.read_text(encoding="utf-8"))
    assert data["profiles"][0]["name"] == "repo"


def test_daemon_stopped_on_upgrade_without_root_profile(tmp_path, monkeypatch):
    """A version/code mismatch stops the stale daemon even when the root has no
    profile (profiles list empty), so the new hooks never reuse the old daemon."""
    repo = tmp_path / "repo"  # no .chameleon/profile.json -> profiles stays empty
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    stop_called = MagicMock()
    # Force running_pkg != installed_pkg by relocating the package's __file__.
    fake_pkg_init = tmp_path / "elsewhere" / "chameleon_mcp" / "__init__.py"
    fake_pkg_init.parent.mkdir(parents=True)
    fake_pkg_init.write_text("__version__ = '9.9.9'\n", encoding="utf-8")

    with (
        patch("chameleon_mcp.__file__", str(fake_pkg_init)),
        patch("chameleon_mcp.daemon.stop_daemon", stop_called),
    ):
        rc, _ = _run_session_start(cwd=repo, repo_root=repo, home=home, monkeypatch=monkeypatch)

    assert rc == 0
    assert stop_called.called, "stale daemon must be stopped on code upgrade"
