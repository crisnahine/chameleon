"""The new command and its env vars must be documented consistently."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_using_chameleon_lists_receiving():
    t = (ROOT / "skills" / "using-chameleon" / "SKILL.md").read_text(encoding="utf-8")
    assert "/chameleon-receiving-code-review" in t


def test_claude_md_count_and_list_and_env():
    t = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "13 user-invocable" in t
    assert "receiving-code-review" in t
    assert (
        "CHAMELEON_REVIEW_REFUTER" in t
        and "CHAMELEON_REVIEW_FANOUT" in t
        and "CHAMELEON_REFUTER_MODEL" in t
    )


def test_architecture_and_readme():
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "(13 commands)" in arch
    assert "skill_triggering_test.sh" not in arch  # stale CI ref removed
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/chameleon-receiving-code-review" in readme
    assert "/chameleon-explain" in readme  # was missing
