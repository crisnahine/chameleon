"""Every honesty-bearing skill carries a tailored '## Honesty Rules' section.

Ported from graphify's skill-body Honesty Rules, but each rule set is valid for
its skill's purpose: following injected conventions (using-chameleon), grounding
findings (pr-review, receiving-code-review), evidence-backed idioms (auto-idiom,
teach), and honest state reporting (explain, status, doctor). A skill that
produces model-facing claims or reports state must keep its honesty rules
explicit so they cannot silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "plugin" / "skills"

# skill name -> a rule phrase that must appear inside its Honesty Rules and is
# specific to that skill's purpose (not a generic copy).
EXPECTED: dict[str, str] = {
    "using-chameleon": "never invent a convention",
    "chameleon-pr-review": "never invent a violation",
    "chameleon-receiving-code-review": "verify each reviewer comment",
    "chameleon-auto-idiom": "evidence or it doesn't exist",
    "chameleon-teach": "capture only a real rule",
    "chameleon-explain": "report only the real recorded state",
    "chameleon-status": "report the real profile state",
    "chameleon-doctor": "report the real health",
}


@pytest.mark.parametrize("skill", sorted(EXPECTED))
def test_skill_has_one_honesty_rules_section(skill: str):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    assert text.count("## Honesty Rules") == 1, (
        f"{skill} SKILL.md must have exactly one '## Honesty Rules' section"
    )


@pytest.mark.parametrize("skill,phrase", sorted(EXPECTED.items()))
def test_honesty_rules_are_purpose_specific(skill: str, phrase: str):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").lower()
    assert phrase.lower() in text, (
        f"{skill} Honesty Rules missing its purpose-specific rule: {phrase!r}"
    )


def test_honesty_rules_are_bulleted_imperatives():
    # graphify-style: a bulleted list of imperative rules, not a prose paragraph.
    for skill in EXPECTED:
        text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("## Honesty Rules", 1)[1]
        # stop at the next top-level heading if any
        section = section.split("\n## ", 1)[0]
        bullets = [ln for ln in section.splitlines() if ln.lstrip().startswith("- ")]
        assert len(bullets) >= 3, f"{skill} Honesty Rules should have >= 3 bulleted rules"
