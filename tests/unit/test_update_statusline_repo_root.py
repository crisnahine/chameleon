"""_update_statusline must target the repo root's cache, not the launch cwd.

When Claude runs from a subdirectory, the live activity/trust update has to land
in the same cache file SessionStart wrote (repo root), or the statusline goes
stale for the whole session. The repo_root argument pins that placement.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import chameleon_mcp.hook_helper as hh


def _seed_cache(base: Path) -> Path:
    cache_dir = base / ".claude"
    cache_dir.mkdir(parents=True)
    cache = cache_dir / ".chameleon-statusline-cache"
    cache.write_text(
        json.dumps({"profiles": [{"name": "repo", "trust": "untrusted"}]}),
        encoding="utf-8",
    )
    return cache


def test_update_statusline_writes_repo_root_cache_when_cwd_is_subdir(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "tests"
    sub.mkdir(parents=True)
    cache = _seed_cache(repo)

    # cwd is the subdir; the update must still reach the repo-root cache.
    with patch("pathlib.Path.cwd", return_value=sub):
        hh._update_statusline("editing", repo_name="repo", trust_state="trusted", repo_root=repo)

    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["activity"] == "editing"
    assert data["profiles"][0]["trust"] == "trusted"
    # No stray cache was created under the subdir.
    assert not (sub / ".claude" / ".chameleon-statusline-cache").exists()


def test_update_statusline_falls_back_to_cwd_without_repo_root(tmp_path):
    base = tmp_path / "proj"
    base.mkdir()
    cache = _seed_cache(base)

    with patch("pathlib.Path.cwd", return_value=base):
        hh._update_statusline("editing")

    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["activity"] == "editing"
