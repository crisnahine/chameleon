"""pr-review runs a 3-round grounding loop. Round 3 is the independent refuter
(refute_finding), scoped to model-judgment findings; tool-grounded findings are
verified inline. The degraded ladder + 'never print 3/3 when round 3 didn't run'
are pinned so an edit can't silently weaken the anti-hallucination guarantee."""

from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills" / "chameleon-pr-review" / "SKILL.md"


def _t():
    """Body plus lazily-loaded references — the skill's full procedure text."""
    refs = sorted(SKILL.parent.glob("references/*.md"))
    parts = [SKILL.read_text(encoding="utf-8")] + [p.read_text(encoding="utf-8") for p in refs]
    return "\n".join(parts)


def test_three_round_loop():
    t = _t()
    assert "3-round" in t or "Round 3" in t
    assert "refute_finding" in t


def test_round3_scope_and_exempt():
    t = _t()
    assert "model-judgment" in t.lower()
    assert "tool-grounded" in t.lower() and "inline" in t.lower()


def test_degraded_ladder_and_banner_rule():
    t = _t()
    assert "round 3 unavailable" in t.lower()
    assert "never" in t.lower() and "3/3" in t
