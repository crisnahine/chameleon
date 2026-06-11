"""SessionStart stale-marker sweep must cover every per-session marker family.

Per-session marker files are zero-byte (or tiny) touch files written during a
session and never explicitly cleaned at SessionEnd (there is no SessionEnd hook).
They age out via `reap_stale_prefixed` at the next SessionStart. If a prefix is
missing from the sweep tuple, those files accumulate forever.

Before this fix, `.dup_judged.` markers were absent from the sweep.
"""

from __future__ import annotations

import os
import time


def test_dup_judged_markers_are_reaped(tmp_path, monkeypatch):
    """SessionStart's stale-marker sweep must include .dup_judged. markers.

    They are per-(session,file,digest) dedup touch markers with no other
    cleanup path; before this fix they accumulated forever.
    """
    from chameleon_mcp.hook_helper import SESSION_REAP_PREFIXES
    from chameleon_mcp.intent_capture import reap_stale_prefixed

    assert ".dup_judged." in SESSION_REAP_PREFIXES
    assert ".corr_judged." in SESSION_REAP_PREFIXES  # unchanged

    repo_data = tmp_path / "repoid"
    repo_data.mkdir()
    stale = repo_data / ".dup_judged.deadbeef"
    stale.touch()
    old = time.time() - 90 * 86400
    os.utime(stale, (old, old))
    fresh = repo_data / ".dup_judged.cafebabe"
    fresh.touch()

    reap_stale_prefixed(repo_data, SESSION_REAP_PREFIXES, max_age_seconds=30 * 86400)
    assert not stale.exists()
    assert fresh.exists()
