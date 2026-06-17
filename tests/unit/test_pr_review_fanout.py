"""pr-review fans out only for large diffs, gated by get_autopass_verdict.fan_out
(never by env the markdown can't read). Only PER-FILE passes are sliced;
whole-diff passes (2.8, 2.9a-d, 3a, 3b, 3f-i, 3g, 3h) run once in synthesis."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "skills" / "chameleon-pr-review"
SKILL = ROOT / "SKILL.md"
REVIEWER = ROOT / "reviewer.md"


def test_fanout_gated_by_autopass():
    t = SKILL.read_text(encoding="utf-8")
    assert "get_autopass_verdict" in t and "fan_out" in t
    assert "recommended" in t


def test_whole_diff_passes_not_sliced():
    t = SKILL.read_text(encoding="utf-8")
    assert "2.9a" in t  # layering must be named as whole-diff (not "2.9b-d")
    assert "run once" in t.lower() or "once during synthesis" in t.lower()


def test_reviewer_template_exists_and_is_read_only():
    r = REVIEWER.read_text(encoding="utf-8")
    assert "Read" in r
    assert "never" in r.lower() and "Edit" in r and "Write" in r  # no-mutation posture
