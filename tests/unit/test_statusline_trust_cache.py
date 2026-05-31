"""trust_profile / refresh must update the per-project statusline cache so the
status line reflects a /chameleon-trust immediately, not the SessionStart
snapshot (which showed `(stale)` until the next session)."""

from __future__ import annotations

import json

from chameleon_mcp import tools as t


def _cache(tmp_path, trust):
    repo = tmp_path / "myrepo"
    cdir = repo / ".claude"
    cdir.mkdir(parents=True)
    cache = cdir / ".chameleon-statusline-cache"
    cache.write_text(
        json.dumps({"profiles": [{"name": "myrepo", "trust": trust}]}), encoding="utf-8"
    )
    return repo, cache


def test_update_statusline_trust_flips_stale_to_trusted(tmp_path):
    repo, cache = _cache(tmp_path, "stale")
    t._update_statusline_trust(repo, "trusted")
    assert json.loads(cache.read_text())["profiles"][0]["trust"] == "trusted"


def test_update_statusline_trust_no_cache_is_noop(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    t._update_statusline_trust(repo, "trusted")  # must not raise


def test_update_statusline_trust_ignores_other_repo_names(tmp_path):
    repo = tmp_path / "myrepo"
    cdir = repo / ".claude"
    cdir.mkdir(parents=True)
    cache = cdir / ".chameleon-statusline-cache"
    cache.write_text(
        json.dumps({"profiles": [{"name": "other", "trust": "stale"}]}), encoding="utf-8"
    )
    t._update_statusline_trust(repo, "trusted")
    # only the matching repo name is updated; 'other' stays as-is
    assert json.loads(cache.read_text())["profiles"][0]["trust"] == "stale"
