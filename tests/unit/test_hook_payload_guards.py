"""Hostile-payload guards for the hook entry points.

json.loads accepts valid-but-non-object JSON (``[1,2,3]``, ``null``, ``42``,
``"x"``); the old code then crashed on ``payload.get(...)`` and relied on the
bash wrapper's ``|| printf '{}'`` to mask the traceback. These tests pin the
Python-layer fail-open contract directly: every entry point must emit a clean
JSON object and return 0 on any malformed stdin, with no exception.
"""
from __future__ import annotations

import io
import json
import os
from unittest.mock import patch

import pytest

from chameleon_mcp.hook_helper import (
    _as_dict,
    _read_payload_dict,
    callout_detector,
    posttool_recorder,
    posttool_verify,
    preflight_and_advise,
)

# Valid JSON that is not an object, plus invalid JSON, dict payloads carrying
# non-dict/non-string sub-fields, and empty input. Each must fail open.
HOSTILE_STDIN = [
    "[1, 2, 3]",
    "null",
    "42",
    '"just a string"',
    "true",
    '{"tool_input": "not-a-dict"}',
    '{"tool_input": [1, 2], "tool_response": "nope"}',
    # tool_name present so posttool_verify reaches _as_dict(tool_input) past the
    # _EDIT_TOOLS gate instead of bailing early.
    '{"tool_name": "Edit", "tool_input": "not-a-dict"}',
    '{"tool_name": "Write", "tool_input": [1, 2]}',
    # non-string user_prompt/prompt would crash callout_detector's re.search.
    '{"user_prompt": ["chameleon", "broke"]}',
    '{"prompt": 42}',
    "not json at all",
    "",
]

ENTRY_POINTS = [
    preflight_and_advise,
    posttool_recorder,
    posttool_verify,
    callout_detector,
]


def _run(entry, raw_stdin: str, *, env: dict) -> tuple[int, dict]:
    """Invoke an entry point with raw stdin; return (exit_code, emitted JSON)."""
    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(raw_stdin)),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, env, clear=False),
    ):
        mock_stdout.write = captured.append
        rc = entry()
    out = "".join(captured).strip()
    return rc, (json.loads(out) if out else {})


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=lambda e: e.__name__)
@pytest.mark.parametrize("raw", HOSTILE_STDIN)
def test_entry_points_fail_open_on_hostile_payload(entry, raw, tmp_path):
    """Each hook emits a JSON object and exits 0 on malformed input."""
    rc, out = _run(entry, raw, env={"CHAMELEON_PLUGIN_DATA": str(tmp_path)})
    assert rc == 0
    assert isinstance(out, dict)


# --- the helpers in isolation -------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ("{}", {}),
        ("[1, 2, 3]", None),
        ("null", None),
        ("42", None),
        ('"x"', None),
        ("true", None),
        ("not json", None),
        ("", None),
    ],
)
def test_read_payload_dict(raw, expected):
    with patch("sys.stdin", io.StringIO(raw)):
        assert _read_payload_dict() == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"k": "v"}, {"k": "v"}),
        ({}, {}),
        ("string", {}),
        ([1, 2], {}),
        (None, {}),
        (42, {}),
    ],
)
def test_as_dict_coerces_non_dicts(value, expected):
    assert _as_dict(value) == expected
