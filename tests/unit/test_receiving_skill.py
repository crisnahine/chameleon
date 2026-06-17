"""The inbound receiving-code-review skill: superpowers spine (verify before
implement, no performative agreement), chameleon adjudication with a trust gate,
grounding BEFORE drafting, never auto-post, never call the ledger."""

from __future__ import annotations

from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[2] / "skills" / "chameleon-receiving-code-review" / "SKILL.md"
)


def _t():
    return SKILL.read_text(encoding="utf-8")


def test_front_matter_name():
    t = _t()
    assert "name: chameleon-receiving-code-review" in t


def test_superpowers_spine():
    t = _t()
    assert "verify before implementing" in t.lower()
    assert "you're absolutely right" in t.lower()  # listed as forbidden


def test_trust_gate_and_adjudication():
    t = _t()
    assert "get_pattern_context" in t
    assert "trust_state" in t
    assert "AGREE" in t and "PUSH BACK" in t and "NEEDS CLARIFICATION" in t and "YAGNI" in t


def test_untrusted_comment_rule():
    assert "untrusted" in _t().lower() and "never instructions" in _t().lower()


def test_ground_before_draft_and_safety():
    t = _t()
    i_ground = t.find("Step 6")
    i_draft = t.find("Step 7")
    assert 0 < i_ground < i_draft  # grounding precedes drafting
    assert "refute_finding" in t
    assert "never auto-post" in t.lower() or "drafts only" in t.lower()
    assert "record_review_verdict" in t  # stated as NOT called
    assert "one at a time" in t.lower()
