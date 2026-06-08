"""Tests for the duplication judge prompt, coercer, and judge_body_matches (Task 7)."""

from __future__ import annotations

import json
from unittest.mock import patch

from chameleon_mcp.duplication_review import Finding, judge_body_matches

FINDINGS = [Finding("renamed", "a.rb", 7, "do_work(x)", "original", "b.rb")]


def _result_line(payload: str) -> str:
    """Produce a single stream-json line whose type=result carries payload.

    Mirrors the real judge._spawn_reviewer stdout shape as confirmed by
    reading judge._parse_findings (judge.py:227-264) and the test_judge.py helper.
    """
    return json.dumps({"type": "result", "result": payload})


def test_judge_confirms(tmp_path):
    out = json.dumps([{"new_name": "renamed", "is_duplicate": True}])
    with patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(out)):
        confirmed = judge_body_matches(tmp_path, FINDINGS)
    assert len(confirmed) == 1 and confirmed[0].new_name == "renamed"


def test_judge_rejects(tmp_path):
    out = json.dumps([{"new_name": "renamed", "is_duplicate": False}])
    with patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(out)):
        assert judge_body_matches(tmp_path, FINDINGS) == []


def test_judge_fails_open_on_dead_spawn(tmp_path):
    with patch("chameleon_mcp.judge._spawn_reviewer", return_value=None):
        assert judge_body_matches(tmp_path, FINDINGS) == []


def test_judge_empty_findings_skips_spawn(tmp_path):
    with patch("chameleon_mcp.judge._spawn_reviewer") as mock_spawn:
        result = judge_body_matches(tmp_path, [])
    assert result == []
    mock_spawn.assert_not_called()


def test_judge_malformed_output_fails_open(tmp_path):
    with patch("chameleon_mcp.judge._spawn_reviewer", return_value="not json at all"):
        assert judge_body_matches(tmp_path, FINDINGS) == []


def test_judge_coerce_skips_non_duplicate(tmp_path):
    # Only items with is_duplicate=True are kept; others ignored.
    out = json.dumps(
        [
            {"new_name": "renamed", "is_duplicate": False},
            {"new_name": "other", "is_duplicate": True},
        ]
    )
    with patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(out)):
        confirmed = judge_body_matches(tmp_path, FINDINGS)
    # "other" is not in FINDINGS, "renamed" is False -> nothing
    assert confirmed == []
