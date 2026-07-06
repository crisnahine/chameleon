"""The new command and its env vars must be documented consistently."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_using_chameleon_lists_receiving():
    t = (ROOT / "skills" / "using-chameleon" / "SKILL.md").read_text(encoding="utf-8")
    assert "/chameleon-receiving-code-review" in t


def test_claude_md_count_and_list_and_env():
    t = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "14 user-invocable" in t
    assert "receiving-code-review" in t
    assert (
        "CHAMELEON_REVIEW_REFUTER" in t
        and "CHAMELEON_REVIEW_FANOUT" in t
        and "CHAMELEON_REFUTER_MODEL" in t
    )


def test_architecture_and_readme():
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "(14 commands)" in arch
    assert "skill_triggering_test.sh" not in arch  # stale CI ref removed
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/chameleon-receiving-code-review" in readme
    assert "/chameleon-explain" in readme  # was missing


def test_deep_work_command_wired():
    skill = (ROOT / "skills" / "chameleon-deep-work" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---")
    assert "name: chameleon-deep-work" in skill
    # the four contract rules the skill encodes
    assert "Do not ask questions" in skill
    assert "worktree" in skill
    assert "Understanding Brief" in skill
    # honesty: empty results are not clearance
    assert "absence of evidence" in skill
    # round-2: the in-flight re-plan obligation and failure-path worktree report
    assert "re-issue the brief" in skill
    assert "on FAILURE too" in skill
    # listed everywhere a command must be listed
    using = (ROOT / "skills" / "using-chameleon" / "SKILL.md").read_text(encoding="utf-8")
    assert "/chameleon-deep-work" in using
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "deep-work" in claude_md
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/chameleon-deep-work" in readme
