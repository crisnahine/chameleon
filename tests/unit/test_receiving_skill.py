"""The inbound receiving-code-review skill: superpowers spine (verify before
implement, no performative agreement), chameleon adjudication with a trust gate,
grounding BEFORE drafting, never auto-post, never call the ledger."""

from __future__ import annotations

from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[2]
    / "plugin"
    / "skills"
    / "chameleon-receiving-code-review"
    / "SKILL.md"
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


def test_builds_hunk_map_and_gates_pre_existing():
    """The skill must fetch the PR diff, build a hunk map, and use it to tell
    PR-introduced code from pre-existing code (so a comment on an untouched line
    is correctly called out as not-introduced-here)."""
    t = _t()
    assert "hunk map" in t.lower()
    assert "unified" in t.lower() and "diff" in t.lower()
    assert "PRE-EXISTING" in t
    # The Step 6 dangling "hunk/severity gates" reference is retired.
    assert "hunk/severity gates" not in t


def test_runs_engine_tools_to_ground_adjudication():
    """Step 4 named tools but never ran them; the skill must now instruct running
    get_callers / lint_file / get_crossfile_context / get_duplication_candidates to
    ground apply-vs-pushback with data, not just conventions."""
    t = _t()
    # "remove this / unused" -> get_callers, with the empty-result honesty caveat.
    assert "get_callers" in t
    assert "NOT proof of dead code" in t
    # "this is fine" -> lint_file sink/secret check.
    assert "get_crossfile_context" in t
    assert "get_duplication_candidates" in t
    for sink in ("eval-call", "command-injection"):
        assert sink in t, f"receiving skill omits sink {sink!r}"


def test_refute_finding_call_specifies_shape_baseref_and_disabled_envelope():
    """The refuter call must carry the finding shape (id/file/line so verdicts map
    back and the excerpt scopes), base_ref (non-main PRs), and the disabled-envelope
    handling (empty verdicts list -> treat as unverified)."""
    t = _t()
    # Direct kwarg form (base_ref=...) or the chameleon_review dispatcher form
    # ("base_ref": ... inside params) — either carries the base to the refuter.
    assert "base_ref=" in t or '"base_ref"' in t
    for field in ("id", "file", "line", "claim", "evidence"):
        assert field in t
    # The disabled envelope returns an EMPTY verdicts list -> read the refuter field.
    assert "disabled" in t
    assert "EMPTY" in t or "empty" in t
    assert "refuter" in t
