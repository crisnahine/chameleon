"""Regression tests for review-fix findings in loader.py and daemon.py.

Covers two behavioral fixes:
  - find_repo_root_with_refusal must honor a CHAMELEON_ALLOW_TMP_REPO flip
    within a long-lived process (env is part of the cache key, not just dir
    mtime), so a cached temp-dir refusal does not stick after the operator
    opts in.
  - the daemon dispatcher no longer carries a dead 'ping' branch; ping is
    answered upstream in _handle_connection.
"""

import os
import tempfile
import uuid

from chameleon_mcp import daemon
from chameleon_mcp.profile import loader


def test_repo_root_refusal_cache_honors_mid_session_allow_tmp_flip(monkeypatch):
    """A cached temp-dir refusal must not survive a CHAMELEON_ALLOW_TMP_REPO=1
    flip in the same process (warm-daemon scenario)."""
    loader.clear_profile_cache()
    monkeypatch.delenv("CHAMELEON_ALLOW_TMP_REPO", raising=False)

    # A .git-marked dir directly under the system temp dir is a real candidate
    # root that the unsafe-root guard refuses while the opt-out env is unset.
    base = os.path.join(tempfile.gettempdir(), f"chameleon-reviewfix-{uuid.uuid4().hex}")
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
    target = os.path.join(repo, "module.py")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")

    try:
        root, reason = loader.find_repo_root_with_refusal(loader.Path(target))
        # A genuine refusal, not a vacuous "no marker found" miss.
        assert root is None
        assert reason is not None and "temp dir" in reason

        # Operator opts in mid-session. Without clearing the cache, the next
        # resolution must reflect the flip rather than serving the stale refusal.
        monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
        root2, reason2 = loader.find_repo_root_with_refusal(loader.Path(target))
        assert reason2 is None
        assert root2 is not None and root2.name == "repo"
    finally:
        loader.clear_profile_cache()
        import shutil

        shutil.rmtree(base, ignore_errors=True)


def test_daemon_dispatch_has_no_dead_ping_branch():
    """ping is intercepted by _handle_connection; the dispatcher must report it
    as unknown rather than carrying a divergent {ok, ts} reply."""
    result = daemon._dispatch("ping", {})
    assert result == {"error": "unknown method 'ping'"}
