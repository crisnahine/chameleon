"""A silent PreToolUse must not leave the statusline reading like a live one.

``_update_statusline`` was called only on the two paths that emit something, so
the activity segment simply went stale and decayed after 30s. A session where
chameleon injects nothing on every single edit therefore renders identically to
a session where the user is just idle -- ``chameleon | repo (trusted)`` -- and
the plugin's whole value is invisible by default, so there is no other cue.

Two silent paths carry a diagnosis worth showing, and both are states the user
did not choose:

- the untrusted repo after its one prompt per session has been spent, which is
  every subsequent edit for the rest of that session;
- a trusted repo where the file resolved to no archetype at all.

A deliberate pause is excluded on purpose: /chameleon-disable and
/chameleon-pause-15m already have their own statusline treatment, and echoing a
choice back at the user is noise rather than signal.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def _result(*, trust_state: str, archetype: str | None) -> dict:
    arch = (
        {
            "archetype": archetype,
            "confidence_band": "high",
            "match_quality": "ast",
            "sub_buckets_count": 1,
            "summary": "Feature module.",
        }
        if archetype
        else None
    )
    return {
        "data": {
            "archetype": arch,
            "canonical_excerpt": {"content": "export const x = 1;", "witness_path": "y.ts"},
            "rules": [],
            "idioms": "",
            "repo": {
                "id": "real-repo",
                "trust_state": trust_state,
                "profile_status": "profile_present",
            },
        },
    }


def _run(tmp_path: Path, result: dict, session_id: str) -> list[tuple]:
    file_path = str(tmp_path / "src" / "users.ts")
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text("export const x = 1;\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "new_string": "export const x = 2;"},
        "session_id": session_id,
    }
    calls: list[tuple] = []

    def _record(activity, *a, **kw):
        calls.append((activity, a, kw))

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path / "data")}, clear=False),
        patch("chameleon_mcp.daemon_client.call", MagicMock(return_value=None)),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=tmp_path),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="real-repo"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.tools.get_pattern_context", MagicMock(return_value=result)),
        patch("chameleon_mcp.drift.observations.record_edit_observation"),
        patch("chameleon_mcp.metrics.emit_hook_metric"),
        patch("chameleon_mcp.hook_helper._update_statusline", _record),
    ):
        mock_stdout.write = lambda s: None
        from chameleon_mcp.hook_helper import preflight_and_advise

        preflight_and_advise()
        # The untrusted prompt fires once per session; the SILENT path is every
        # edit after it, so drive a second edit in the same session.
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            preflight_and_advise()
    return calls


def test_untrusted_dedup_path_marks_the_statusline(tmp_path: Path) -> None:
    calls = _run(tmp_path, _result(trust_state="untrusted", archetype="feature"), "sess-untrusted")
    assert calls, "silent untrusted edit left the statusline untouched"
    assert any("untrusted" in str(c[0]).lower() for c in calls)


def test_no_archetype_path_marks_the_statusline(tmp_path: Path) -> None:
    calls = _run(tmp_path, _result(trust_state="trusted", archetype=None), "sess-no-arch")
    assert calls, "silent no-archetype edit left the statusline untouched"
    assert any("archetype" in str(c[0]).lower() for c in calls)
