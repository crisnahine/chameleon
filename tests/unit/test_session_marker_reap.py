"""SessionStart stale-marker sweep must cover every per-session marker family.

Per-session marker files are zero-byte (or tiny) touch files written during a
session and never explicitly cleaned at SessionEnd (there is no SessionEnd hook).
They age out via `reap_stale_prefixed` at the next SessionStart. If a prefix is
missing from the sweep tuple, those files accumulate forever.
"""

from __future__ import annotations

import os
import time


def test_dup_judged_markers_are_reaped(tmp_path):
    """SessionStart's stale-marker sweep must include .dup_judged. markers.

    They are per-(session,file,digest) dedup touch markers with no other
    cleanup path.
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


# Every family below was writing one file per session with nothing to age it
# out. On a machine that has run Claude for months that is the bulk of a repo's
# state directory -- one measured repo held 101 .attestation_last. markers, 87
# of them over a week old.
_UNSWEPT_FAMILIES = (
    ".attestation_last.",
    ".testint_judged.",
    ".trust_prompted.",
    ".job_heartbeat.",
    ".crossfile_deleted.",
)


def test_every_per_session_marker_family_is_swept(tmp_path):
    from chameleon_mcp.hook_helper import SESSION_REAP_PREFIXES
    from chameleon_mcp.intent_capture import reap_stale_prefixed

    for prefix in _UNSWEPT_FAMILIES:
        assert prefix in SESSION_REAP_PREFIXES, prefix

    repo_data = tmp_path / "repoid"
    repo_data.mkdir()
    old = time.time() - 90 * 86400
    stale = []
    for prefix in _UNSWEPT_FAMILIES:
        marker = repo_data / f"{prefix}deadbeef"
        marker.touch()
        os.utime(marker, (old, old))
        stale.append(marker)

    reap_stale_prefixed(repo_data, SESSION_REAP_PREFIXES, max_age_seconds=30 * 86400)
    assert [m for m in stale if m.exists()] == []


def test_a_live_marker_survives_the_sweep(tmp_path):
    """The sweep is mtime-gated, so a marker a running session is still
    touching -- a job heartbeat is rewritten every 10s -- is never reaped."""
    from chameleon_mcp.hook_helper import SESSION_REAP_PREFIXES
    from chameleon_mcp.intent_capture import reap_stale_prefixed

    repo_data = tmp_path / "repoid"
    repo_data.mkdir()
    live = [repo_data / f"{prefix}cafebabe" for prefix in _UNSWEPT_FAMILIES]
    for marker in live:
        marker.touch()

    reap_stale_prefixed(repo_data, SESSION_REAP_PREFIXES, max_age_seconds=30 * 86400)
    assert [m for m in live if not m.exists()] == []


def test_the_durable_attestation_ledger_is_never_reaped(tmp_path):
    """The sweep matches on a filename prefix, so a durable file whose name
    starts like a marker family would be deleted with them. The attestation
    ledger is the security-relevant record autopass reads -- the per-session
    .attestation_last. touch marker is not."""
    from chameleon_mcp.hook_helper import SESSION_REAP_PREFIXES
    from chameleon_mcp.intent_capture import reap_stale_prefixed

    repo_data = tmp_path / "repoid"
    repo_data.mkdir()
    durable = [
        repo_data / "session_attestations.ndjson",
        repo_data / "review_ledger.ndjson",
        repo_data / "findings_ledger.json",
        repo_data / "drift.db",
    ]
    old = time.time() - 900 * 86400
    for path in durable:
        path.touch()
        os.utime(path, (old, old))

    reap_stale_prefixed(repo_data, SESSION_REAP_PREFIXES, max_age_seconds=30 * 86400)
    assert [p for p in durable if not p.exists()] == []
