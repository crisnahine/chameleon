"""Unit tests for tools.get_shadow_report — the MCP-facing shadow report.

get_shadow_report resolves the repo arg to a repo_id, delegates to
shadow_report.build_shadow_report, and wraps the result in the standard
envelope. These tests pin the envelope, the no_repo / bad-input paths, and the
fail-open contract when the reader raises (a corrupt log must not crash the
status call).

Isolation: repo resolution and the reader are patched; no real data dir is read.
"""

from __future__ import annotations

from unittest.mock import patch

from chameleon_mcp import tools


def test_rejects_empty_repo():
    out = tools.get_shadow_report("")
    assert out["data"]["status"] == "failed"


def test_no_repo_when_unresolvable():
    with patch("chameleon_mcp.tools._resolve_repo_arg", return_value=(None, None)):
        out = tools.get_shadow_report("/nope")
    assert out["data"]["status"] == "no_repo"


def test_envelope_wraps_reader_result():
    report = {
        "repo_id": "R",
        "window_days": 21,
        "window_truncated": False,
        "total_edits": 5,
        "rules": {"x": {"would_blocks": 0, "verdict": "insufficient_data"}},
        "idiom_review": {"would_blocks": 1},
        "sample": [],
    }
    with (
        patch("chameleon_mcp.tools._resolve_repo_arg", return_value=(None, "R")),
        patch("chameleon_mcp.shadow_report.build_shadow_report", return_value=report) as b,
    ):
        out = tools.get_shadow_report("/some/repo", 21)
    assert out["api_version"] == "1"
    assert out["data"] == report
    b.assert_called_once_with("R", 21)


def test_fail_open_when_reader_raises():
    with (
        patch("chameleon_mcp.tools._resolve_repo_arg", return_value=(None, "R")),
        patch("chameleon_mcp.shadow_report.build_shadow_report", side_effect=OSError("boom")),
    ):
        out = tools.get_shadow_report("/some/repo")
    data = out["data"]
    assert data["rules"] == {}
    assert data["total_edits"] == 0
    assert data["window_truncated"] is False
