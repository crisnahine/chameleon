"""Both review skills must FOLLOW the superpowers code-review discipline.

chameleon-pr-review layers its repo-grounding on top of the superpowers
``code-reviewer`` template, and chameleon-receiving-code-review on top of the
superpowers ``receiving-code-review`` skill. These tests pin the specific
superpowers discipline elements into the chameleon skill bodies so a later edit
cannot quietly drop them. They assert on the procedure text (the skills are
LLM-driven procedures), the same way the other skill tests do.

The grounded passes chameleon adds beyond superpowers are covered by the other
test modules; this file only guards the imported superpowers discipline.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PR = REPO_ROOT / "plugin" / "skills" / "chameleon-pr-review" / "SKILL.md"
RECV = REPO_ROOT / "plugin" / "skills" / "chameleon-receiving-code-review" / "SKILL.md"
UC = REPO_ROOT / "plugin" / "skills" / "using-chameleon" / "SKILL.md"


def _pr() -> str:
    # Whitespace-normalized so phrase assertions match regardless of line wrapping.
    # Includes the lazily-loaded references/*.md — the skill's full procedure text.
    parts = [PR.read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted(PR.parent.glob("references/*.md"))]
    return " ".join("\n".join(parts).split())


def _recv() -> str:
    return " ".join(RECV.read_text(encoding="utf-8").split())


def _uc() -> str:
    return " ".join(UC.read_text(encoding="utf-8").split())


# --- using-chameleon carries the peer-routing contract -----------------------


def test_using_chameleon_states_process_first_sequencing():
    """The layering rule the SessionStart routing block compresses: superpowers
    owns process, chameleon owns output and facts.

    Asserts the layering SENTENCE, not the words. "Process-gating skills" and
    "superpowers" both appear in text that predates this section, so a
    presence-only check stayed green with the whole section deleted.
    """
    t = _uc()
    assert "Superpowers owns **process**" in t
    assert "Chameleon owns **output and facts**" in t
    assert "Process gates set the sequence" in t


def test_using_chameleon_names_the_review_supersedes_rules():
    """Assert the PAIRING, not the presence of each command name: both appear in
    the pre-existing command table, so a SKILL.md that inverted the direction
    would satisfy a presence-only check.

    Only the receiving side is a supersede. v4.5.15 claimed both were, and both
    halves of that were false for pr-review: `superpowers:requesting-code-review`
    exists to dispatch a reviewer SUBAGENT so the diff never enters the
    coordinator's window, while pr-review runs its passes in that same context
    unless fan-out is recommended; and that reviewer's "are all tests passing?"
    verdict input is out of scope for a static review that runs nothing.
    """
    t = _uc()
    assert "`/chameleon-receiving-code-review` supersedes `superpowers:receiving-code-review`" in t
    assert "`/chameleon-pr-review` supersedes `superpowers:requesting-code-review`" not in t
    assert 'it is NOT a superset, so say "prefer", not "supersedes"' in t


def test_using_chameleon_states_what_pr_review_does_not_carry():
    """Naming the non-supersede is not enough -- a reader needs to know WHICH
    two things are missing, or "not a superset" is unactionable."""
    t = _uc()
    assert "reviewer SUBAGENT" in t
    assert "are all tests passing?" in t


def test_using_chameleon_keeps_deep_work_out_of_brainstorming():
    """deep-work's no-questions contract and brainstorming's HARD-GATE cannot
    both run; the skill must say which wins.

    Both command names predate this section, so the pairing is what gets
    asserted -- "NOT in its path" alone would be satisfied by a sentence about
    some other skill entirely.
    """
    t = _uc()
    assert "`superpowers:brainstorming` - a gate of nothing but questions - is NOT in its path" in t
    assert "`/chameleon-deep-work` is self-contained" in t


def test_using_chameleon_documents_the_peer_routing_kill_switch():
    assert "CHAMELEON_PEER_ROUTING" in _uc()


# --- pr-review follows superpowers code-reviewer.md --------------------------


def test_pr_review_states_read_only_discipline():
    """superpowers code-reviewer.md: read-only on this checkout; never mutate the
    working tree / index / HEAD / branch; worktree for other revisions."""
    t = _pr()
    assert "Read-only review" in t
    assert "never mutates" in t
    assert "git worktree add" in t
    assert "never `git checkout`" in t


def test_pr_review_edge_cases_and_perf_run_always():
    """Edge cases / performance / type safety are unconditional in the superpowers
    template; 3c must run always (ticket or not), matching reviewer.md."""
    t = _pr()
    assert "Edge cases (3c) and callable-signature drift (3c-i) run ALWAYS" in t
    assert "Check edge cases, performance, and type safety (always)" in t
    # The superpowers code-quality / architecture checks chameleon was missing.
    assert "Performance / scalability (advisory)" in t
    assert "N+1" in t
    assert "Type safety (advisory)" in t
    assert "Documentation (advisory)" in t


def test_pr_review_flags_plan_level_issues():
    """superpowers calibration: flag issues with the PLAN itself, and frame a
    significant deviation as confirm-intent, not only the implementation."""
    t = _pr()
    assert "Plan-level concern (calibration)" in t
    assert "contradictory, infeasible, or wrong" in t
    assert "confirm-intent advisory" in t


def test_pr_review_verdict_carries_reasoning():
    """superpowers Assessment = Ready-to-merge + a 1-2 sentence Reasoning."""
    t = _pr()
    assert "Reasoning:" in t
    assert "decisive finding" in t


def test_pr_review_has_recommendations_section():
    """superpowers output ends with a Recommendations section (grounded here)."""
    t = _pr()
    assert "### Recommendations (advisory)" in t


def test_pr_review_states_tests_passing_out_of_scope():
    """superpowers asks 'all tests passing?'; the static review states it is out of
    scope and the test-integrity facts are the proxy."""
    t = _pr()
    assert "OUT OF SCOPE" in t
    assert "pass/fail" in t


# --- receiving follows superpowers receiving-code-review ---------------------


def test_receiving_has_five_external_reviewer_checks():
    """superpowers: before implementing an external suggestion, check 5 things."""
    t = _recv()
    assert "confirm five things" in t
    assert "technically correct for" in t
    assert "break existing functionality" in t
    assert "reason the code is currently written this way" in t
    assert "all platforms / runtime versions" in t
    assert "full context" in t


def test_receiving_lists_push_back_triggers():
    """superpowers: a concrete when-to-push-back list incl legacy/compat + context."""
    t = _recv()
    assert "Push back (with technical reasoning" in t
    assert "legacy / backward-compat reason exists" in t
    assert "reviewer lacks the full context" in t
    # The uncomfortable-pushing-back rule.
    assert "name that tension" in t


def test_receiving_corrects_own_pushback_gracefully():
    """superpowers: when your pushback was wrong, state it factually, no apology."""
    t = _recv()
    assert "your pushback was wrong" in t.lower() or "YOUR pushback was wrong" in t
    assert "no long apology" in t


def test_receiving_no_gratitude_is_emphatic():
    """superpowers is emphatic: no 'Great point!'/'Excellent feedback!', NO gratitude,
    DELETE a 'Thanks' before sending; 'Good catch - ...' is an allowed form."""
    t = _recv()
    assert "Excellent feedback!" in t
    assert "ANY gratitude" in t
    assert "DELETE IT" in t
    assert "Good catch -" in t


def test_receiving_spells_out_thread_reply_mechanism():
    """superpowers: reply IN the inline thread, not a top-level comment."""
    t = _recv()
    assert "pulls/{pr}/comments/{id}/replies" in t
    assert "NOT as a" in t and "top-level PR comment" in t


def test_receiving_verifies_no_regressions_after_last_item():
    """superpowers implementation-order step 4: verify no regressions across the set."""
    t = _recv()
    assert "no regressions across the whole set" in t
