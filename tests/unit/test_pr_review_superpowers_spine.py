"""pr-review carries the superpowers reviewer discipline (be specific, explain
WHY, no 'looks good' without checking, no nitpick-as-BLOCK, give a clear verdict,
strengths first) and maps its BLOCK/FIX/NIT vocabulary to Critical/Important/Minor."""

from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills" / "chameleon-pr-review" / "SKILL.md"


def _t():
    return SKILL.read_text(encoding="utf-8")


def test_reviewer_philosophy_spine_present():
    t = _t()
    assert "## Reviewer discipline" in t
    # Check for the nitpick/BLOCK phrase (may have whitespace/newlines)
    t_lower = t.lower()
    assert (
        "mark a" in t_lower and "nitpick as block" in t_lower
    ) or "do not mark nitpicks as block" in t_lower
    assert "code you" in t_lower and "read" in t_lower


def test_severity_mapping_note():
    t = _t()
    assert "Critical" in t and "Important" in t and "Minor" in t


def test_strengths_and_banner_in_template():
    t = _t()
    assert "### Strengths / verified clean" in t
    assert "Grounding: rounds 1-2 self-verified" in t
