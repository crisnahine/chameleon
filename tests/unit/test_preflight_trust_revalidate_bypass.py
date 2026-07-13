"""Regression: CHAMELEON_TRUST_REVALIDATE=1 must bypass the daemon proxy.

The daemon's environment is frozen at spawn time and never observes an env
override set only on a later hook invocation. If CHAMELEON_TRUST_REVALIDATE=1
is set for THIS call but the running daemon spawned without it, proxying to
the daemon silently defeats the per-call trust re-check the caller just asked
for -- a stale-vs-trusted mismatch never surfaces.

The daemon-result guard already discards ``no_repo`` / ``profile_corrupted``
/ ``profile_unsupported_schema_version`` for a similar frozen-env reason
(see test_preflight_no_repo_fallback.py). This is the trust-revalidate
sibling: the daemon must not even be CALLED when the flag is set, because a
trusted-looking daemon response would never be distinguished from a stale
one that the flag demands be re-checked.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def _daemon_result_trusted() -> dict:
    """What a daemon spawned WITHOUT the revalidate flag would return -- it
    never re-checks staleness, so it reports "trusted" even after a
    trust-hashed artifact changed underneath it."""
    return {
        "data": {
            "archetype": {
                "archetype": "feature-module",
                "confidence_band": "high",
                "match_quality": "ast",
                "sub_buckets_count": 1,
                "summary": "Feature module. src/features/**/*.ts",
            },
            "canonical_excerpt": {"content": "export const x = 1;", "witness_path": "y.ts"},
            "rules": [],
            "idioms": "",
            "repo": {
                "id": "real-repo",
                "trust_state": "trusted",
                "profile_status": "profile_present",
            },
        },
    }


def _in_process_result_stale() -> dict:
    """What the in-process path reports for the SAME repo once it actually
    re-checks: the trust grant is stale."""
    return {
        "data": {
            "archetype": {
                "archetype": "feature-module",
                "confidence_band": "high",
                "match_quality": "ast",
                "sub_buckets_count": 1,
                "summary": "Feature module. src/features/**/*.ts",
            },
            "canonical_excerpt": {"content": "export const x = 1;", "witness_path": "y.ts"},
            "rules": [],
            "idioms": "",
            "repo": {
                "id": "real-repo",
                "trust_state": "stale",
                "profile_status": "profile_present",
            },
        },
    }


def test_trust_revalidate_flag_bypasses_daemon(tmp_path: Path) -> None:
    file_path = str(tmp_path / "src" / "features" / "users.ts")
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text("export const x = 1;\n", encoding="utf-8")

    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "new_string": "export const x = 2;"},
        "session_id": "sess-revalidate",
    }

    captured: list[str] = []

    daemon_call = MagicMock(return_value=_daemon_result_trusted())
    in_process = MagicMock(return_value=_in_process_result_stale())

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(
            os.environ,
            {"CHAMELEON_PLUGIN_DATA": str(tmp_path / "data"), "CHAMELEON_TRUST_REVALIDATE": "1"},
            clear=False,
        ),
        patch("chameleon_mcp.daemon_client.call", daemon_call),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=tmp_path),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="real-repo"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.tools.get_pattern_context", in_process),
        patch("chameleon_mcp.drift.observations.record_edit_observation"),
        patch("chameleon_mcp.metrics.emit_hook_metric"),
    ):
        mock_stdout.write = lambda s: captured.append(s)
        from chameleon_mcp.hook_helper import preflight_and_advise

        preflight_and_advise()

    output = "".join(captured)

    # The flag must skip the daemon proxy entirely -- calling it and then
    # discarding the response is not enough, since discard logic is keyed off
    # profile_status values the daemon has no reason to report incorrectly.
    assert not daemon_call.called, "daemon was called despite CHAMELEON_TRUST_REVALIDATE=1"
    assert in_process.called, "in-process get_pattern_context was not used for the re-check"
    assert "stale" in output.lower(), f"expected the stale trust state to surface, got: {output!r}"
