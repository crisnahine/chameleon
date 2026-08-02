"""The Stop re-check must not disarm what the per-edit hooks armed.

`_stop_file_still_blockable` runs at turn end and CLEARS `blockable_unresolved`
whenever it returns False. It was gated on `detect_language`, so widening only
the arming sites bought nothing: a `.go` file with a hardcoded credential armed
at PostToolUse and was then quietly disarmed here, and the net effect on the
block surface for the five extraction-tier languages was zero.

This is the end-to-end evidence for that chain, not a restatement of the gate
helper: it drives the real re-check against a real file on disk and asserts the
verdict, so reverting the gate to `detect_language` fails it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chameleon_mcp.stop.gates import _stop_file_still_blockable

# AWS's own published documentation example key. Not a real credential.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # chameleon-ignore secret-detected-in-content

_LEAKS: dict[str, tuple[str, str]] = {
    "go": ("leak.go", 'package m\n\nconst k = "%s"\n'),
    "rust": ("leak.rs", 'pub const K: &str = "%s";\n'),
    "java": ("Leak.java", 'class Leak { static String k = "%s"; }\n'),
    "csharp": ("Leak.cs", 'class Leak { const string K = "%s"; }\n'),
    "php": ("leak.php", '<?php\n$k = "%s";\n'),
    "ruby": ("leak.rb", 'K = "%s"\n'),
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal profiled repo the Stop re-check can resolve."""
    root = tmp_path / "repo"
    profile = root / ".chameleon"
    profile.mkdir(parents=True)
    (profile / "COMMITTED").write_text("committed-at=1\npid=1\n", encoding="utf-8")
    (profile / "profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    (profile / "config.json").write_text(
        json.dumps({"enforcement": {"mode": "enforce", "correctness_judge": False}}),
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize("language", sorted(_LEAKS), ids=sorted(_LEAKS))
def test_a_credential_still_blocks_at_turn_end(repo: Path, language: str):
    """Every source language, not just the three with a dimension extractor."""
    filename, template = _LEAKS[language]
    target = repo / filename
    target.write_text(template % _AWS_KEY, encoding="utf-8")

    rules: list[str] = []
    verdict = _stop_file_still_blockable(repo, str(target), out_rules=rules)

    assert verdict is True, (
        f"{language}: the turn-end re-check cleared a hardcoded credential in "
        f"{filename}, disarming the Stop backstop the per-edit hooks armed"
    )
    assert "secret-detected-in-content" in rules, rules


def test_prose_is_still_cleared_at_turn_end(repo: Path):
    """The narrow gate's real job, which widening must not undo.

    A credential-shaped token in markdown has no inline `chameleon-ignore`
    escape, so blocking the turn on it would leave no way out.
    """
    target = repo / "NOTES.md"
    target.write_text(f"An example key looks like {_AWS_KEY} in the docs.\n", encoding="utf-8")

    assert _stop_file_still_blockable(repo, str(target)) is False


def test_an_inline_ignore_still_clears_the_block(repo: Path):
    """The escape hatch has to work for the languages that just gained the block.

    Go's comment syntax is `//`, and the deny message offers exactly that form,
    so a fixture credential must be suppressible the same way it is in Ruby.
    """
    target = repo / "fixture.go"
    target.write_text(
        f'package m\n\nconst k = "{_AWS_KEY}"  // chameleon-ignore secret-detected-in-content\n',
        encoding="utf-8",
    )

    assert _stop_file_still_blockable(repo, str(target)) is False
