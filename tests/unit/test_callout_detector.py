"""Unit tests for the UserPromptSubmit frustration callout detector."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from chameleon_mcp.hook_helper import callout_detector


def _run(prompt: str) -> str:
    out = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"prompt": prompt}))),
        patch("sys.stdout", out),
    ):
        callout_detector()
    return out.getvalue()


def test_generic_profanity_without_chameleon_does_not_fire():
    assert "detected frustration" not in _run("ugh this fucking code is broken")
    assert "detected frustration" not in _run("damn it, this isn't right")


def test_chameleon_specific_complaint_fires():
    assert "detected frustration" in _run("chameleon is so slow")
    assert "detected frustration" in _run("stop injecting all this context")
    assert "detected frustration" in _run("don't inject that again")


def test_generic_frustration_with_chameleon_mention_fires():
    assert "detected frustration" in _run("ugh chameleon is annoying")
    assert "detected frustration" in _run("I hate chameleon's constant injection")


def test_neutral_prompt_does_not_fire():
    assert "detected frustration" not in _run("please add a new endpoint to the API")


def test_empty_prompt_is_safe():
    assert _run("").strip() == "{}"
