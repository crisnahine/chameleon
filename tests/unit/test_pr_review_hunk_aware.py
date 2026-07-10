"""The chameleon-pr-review skill must stay hunk-aware.

The review's logic findings are only trustworthy if they are anchored to lines
the change actually introduced. Two pieces make that work and both live in the
skill text: capturing the full unified diff (not just ``--name-only``) so the
model has a delta to reason over, and a hard hunk gate that drops any per-line
logic finding whose anchor falls outside an added/changed range. If either
instruction is lost in an edit, the skill silently regresses to whole-file
review and the pre-existing-issue false positives the integrity rule forbids
come back. These tests pin the load-bearing instructions in place.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "skills" / "chameleon-pr-review" / "SKILL.md"


def _skill_text() -> str:
    """Body plus lazily-loaded references — the skill's full procedure text."""
    refs = sorted(SKILL.parent.glob("references/*.md"))
    parts = [SKILL.read_text(encoding="utf-8")] + [p.read_text(encoding="utf-8") for p in refs]
    return "\n".join(parts)


def test_branch_case_captures_full_unified_diff():
    """The no-args branch path must capture the full diff, not only file names."""
    text = _skill_text()
    # The name-listing command for the branch case (same base as the full diff).
    # It uses --name-status -M (not bare --name-only) so a Deleted/Renamed file is
    # distinguishable -- the per-file loop must not treat a deletion as a normal
    # source file. The base is production_ref-aware, so the command is written with
    # a <base> placeholder — both invocations must share it.
    assert "git diff <base>...HEAD --name-status -M" in text
    assert "git diff <base>...HEAD` (same base)" in text
    # The locked production branch is the preferred base.
    assert "production_ref" in text
    # The bare name listing must no longer be the only diff the branch path runs:
    # there is an explicit instruction to also get the unified diff.
    assert "full unified diff" in text


def test_hunk_parsing_step_present():
    text = _skill_text()
    assert "Parse hunks from the unified diff" in text
    # The hunk header shape the model parses to derive line ranges.
    assert "@@ -old_start" in text
    # Both halves of the hunk map the later steps consume.
    assert "Added/changed line ranges" in text
    assert "Removed lines" in text


def test_change_delta_pass_references_removed_lines_not_witness():
    text = _skill_text()
    assert "Change-delta logic pass" in text
    # The reference for the delta pass is the removed lines, explicitly NOT the
    # canonical witness. Both the directive and the negation must be present.
    assert "removed (`-`) lines" in text
    assert "NOT the canonical witness" in text
    # The construct classes a human catches from a diff.
    for needle in ("Removed guard", "early return", "await", "Inverted condition"):
        assert needle in text, f"change-delta pass omits {needle!r}"


def test_hunk_gate_is_a_hard_gate_on_logic_findings():
    text = _skill_text()
    assert "Hunk gate" in text
    # The gate must be mechanical, not a judgment call.
    assert "No exceptions and no judgment call" in text
    # It must drop findings whose anchor is outside the changed ranges.
    assert "drop the finding" in text
    # Missing-requirement BLOCKs flag absence and are explicitly exempt.
    assert "those flag the ABSENCE of code" in text


def test_integrity_loop_defers_to_the_hunk_gate():
    """The 2-round loop must use the deterministic gate, not by-hand judgment."""
    text = _skill_text()
    assert "do not override it by judgment" in text


def test_hunk_gate_covers_line_anchored_convention_findings():
    """A line-anchored lint finding must run through the hunk gate too.

    lint_file reads the whole file, so a style-rule-violation (e.g. a too-long
    line) can sit outside the change. The gate must route any convention/style
    finding that carries a parseable line through the same map as a logic
    finding, so a pre-existing out-of-hunk style nit is dropped, not reported.
    """
    text = _skill_text()
    assert "line-anchored convention/style finding from `lint_file`" in text
    # The named rule shapes that carry a line and so must be gated.
    assert "style-rule-violation" in text
    # Only truly file-anchored convention findings stay exempt.
    assert "Only convention findings with NO parseable line" in text


def test_placeholder_name_nit_present_and_capped_at_nit():
    text = _skill_text()
    assert "Placeholder-name NIT" in text
    # The specific placeholder shapes called out.
    for needle in ("data2", "temp", "foo"):
        assert needle in text, f"placeholder NIT omits {needle!r}"
    # Loop counters and idiomatic short scopes are explicitly exempt so the NIT
    # does not misfire on `i`/`e`/`_`.
    assert "loop counters" in text
    # It must never escalate above NIT.
    assert "Never escalate above NIT" in text
