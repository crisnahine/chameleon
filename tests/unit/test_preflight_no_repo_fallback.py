"""Regression: a daemon ``no_repo`` result must fall back to in-process.

The version+fingerprint-keyed daemon socket is shared across every session on
the machine, and the daemon's environment is frozen at spawn time. Repo
resolution is environment-sensitive (e.g. ``CHAMELEON_ALLOW_TMP_REPO``, HOME),
so a daemon spawned in a divergent environment can return ``no_repo`` for a
path the in-process path resolves to a real, trusted profile. If the hook
trusts that ``no_repo`` it silently skips BOTH injection and the enforcement
deny -- the daemon stops being a pure latency layer and becomes a correctness
layer, which the fast-path contract forbids.

The daemon-result guard already discards ``profile_corrupted`` /
``profile_unsupported_schema_version`` / ``no_profile`` for the same reason
(BUG-029). ``no_repo`` is the repo-resolution sibling and must be discarded
too.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def _no_repo_daemon_result() -> dict:
    """What the daemon returns when it cannot resolve the path to a repo."""
    return {
        "data": {
            "archetype": {},
            "canonical_excerpt": {},
            "rules": [],
            "idioms": "",
            "repo": {"id": None, "trust_state": "n/a", "profile_status": "no_repo"},
        },
    }


def _in_process_result() -> dict:
    """What the in-process path returns for the SAME path once resolved."""
    return {
        "data": {
            "archetype": {
                "archetype": "feature-module",
                "confidence_band": "high",
                "match_quality": "ast",
                "sub_buckets_count": 1,
                "summary": "Feature module. src/features/**/*.ts",
            },
            "canonical_excerpt": {
                "content": "export const x = 1;",
                "witness_path": "src/features/users.ts",
            },
            "rules": [{"id": "r0"}],
            "idioms": "",
            "repo": {
                "id": "real-repo",
                "trust_state": "trusted",
                "profile_status": "profile_present",
            },
        },
    }


def test_daemon_no_repo_falls_back_to_in_process(tmp_path: Path) -> None:
    file_path = str(tmp_path / "src" / "features" / "users.ts")
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text("export const x = 1;\n", encoding="utf-8")

    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "new_string": "export const x = 2;"},
        "session_id": "sess-norepo",
    }

    captured: list[str] = []

    in_process = MagicMock(return_value=_in_process_result())

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path / "data")}, clear=False),
        patch("chameleon_mcp.daemon_client.call", return_value=_no_repo_daemon_result()),
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

    # The daemon said no_repo; the hook MUST re-check in-process rather than
    # trust that negative and go silent.
    assert in_process.called, "in-process get_pattern_context was not called after daemon no_repo"
    # And the in-process (trusted, archetyped) result is what reaches the model.
    assert "feature-module" in output, f"expected injected archetype context, got: {output!r}"
