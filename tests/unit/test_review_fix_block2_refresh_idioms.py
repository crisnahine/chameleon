"""BLOCK#2 regression: refresh must serialize its idioms.md read+commit against
teach via .idioms.lock, or a concurrent /chameleon-teach is silently clobbered
by the profile dir-swap.

This pins the wiring deterministically (no timing): during a refresh the
.idioms.lock is acquired, with a blocking timeout (so an in-flight teach is
waited out, not raced), and only AFTER the .refresh.lock.
"""

from __future__ import annotations

import contextlib

from chameleon_mcp import locks as locks_mod
from chameleon_mcp import tools
from chameleon_mcp.profile import trust as trust_mod


def test_refresh_holds_idioms_lock_around_rederive(tmp_path, monkeypatch):
    repo = tmp_path
    (repo / ".chameleon").mkdir()

    monkeypatch.setattr(tools, "_validate_file_path_arg", lambda r: True)
    monkeypatch.setattr(tools, "_resolve_repo_arg", lambda r: (repo, "rid"))
    monkeypatch.setattr(tools, "_unsafe_root_refusal", lambda p: None)
    monkeypatch.setattr(tools, "_compute_repo_id", lambda p: "rid")
    monkeypatch.setattr(trust_mod, "repo_data_dir", lambda rid: tmp_path / "data")

    acquired: list[tuple[str, float | None]] = []

    @contextlib.contextmanager
    def fake_lock(path, *, stale_after_seconds=3600, blocking_timeout=None):
        from pathlib import Path

        acquired.append((Path(path).name, blocking_timeout))
        yield

    monkeypatch.setattr(locks_mod, "acquire_advisory_lock", fake_lock)
    monkeypatch.setattr(tools, "_capture_pre_refresh_state", lambda p: None)
    monkeypatch.setattr(tools, "_maybe_fetch_production_ref", lambda p: None)
    monkeypatch.setattr(
        tools,
        "_refresh_repo_locked",
        lambda p, *, force, analysis_root=None: {"status": "ok"},
    )
    monkeypatch.setattr(tools, "_inject_production_ref_fetch", lambda e, f: None)
    monkeypatch.setattr(tools, "_inject_archetype_diff", lambda e, p, s: None)
    monkeypatch.setattr(tools, "_maybe_preserve_trust_across_refresh", lambda p, s, e: None)
    monkeypatch.setattr(tools, "detect_repo", lambda x: {"data": {}})
    monkeypatch.setattr(tools, "_notify_daemon_cache_invalidation", lambda: None)

    tools.refresh_repo(str(repo))

    names = [n for n, _ in acquired]
    assert ".refresh.lock" in names, names
    assert ".idioms.lock" in names, names
    # idioms lock taken AFTER the refresh lock (no deadlock-prone reverse order)
    assert names.index(".refresh.lock") < names.index(".idioms.lock")
    # idioms lock is blocking so an in-flight teach finishes before the re-derive
    # reads idioms.md, instead of racing it.
    idioms_timeout = next(to for n, to in acquired if n == ".idioms.lock")
    assert idioms_timeout is not None and idioms_timeout > 0
