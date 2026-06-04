"""The chameleon-pr-review skill must run a security pass on every changed file.

The no-ticket PR-review case used to do zero security: ``lint_file`` ran only on
archetype-matched source files in the convention loop, and the secrets it found
were folded into the generic violation list, never escalated. The security pass
(Step 2.6) fixes the one reliable slice and labels the rest honestly:

- 2.6a escalates ``secret-detected-in-content`` to BLOCK. The scanner runs
  before the archetype match and before the trust gate, so a secret is a
  witnessed fact, the only one in this pass.
- 2.6b is a presence-only Ruby controller authz advisory capped at FIX. No
  profile data maps a callback to the actions it guards, so the finding must
  never claim a structured divergence and must stay advisory.
- 2.6c is an LLM-judge taint/SSRF/traversal heuristic capped at FIX, scoped to a
  single hunk, with the cited line required inside the diff.

If any of these instructions, or the severity caps, are lost in an edit the
skill regresses to either no security signal or an over-confident one. The skill
is an LLM-driven procedure, so the test asserts on the procedure text the same
way the dependency-review and hunk-aware tests do.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "skills" / "chameleon-pr-review" / "SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_security_pass_step_present_and_always_runs():
    text = _skill_text()
    assert "Step 2.6: Security pass" in text
    # The pass is not gated on a ticket; it covers the no-ticket open-source case.
    assert "every changed source file regardless of whether a Jira ticket" in text


def test_lint_runs_on_every_source_file_even_with_no_archetype():
    """The secret scan needs lint_file to run even when no archetype matches."""
    text = _skill_text()
    assert "Run this on every changed source file, even when no archetype matches" in text
    # The reason: the secret scan precedes the archetype match and trust gate.
    assert "scans for secrets before it looks at the archetype" in text
    assert 'just because `match_quality` is "none"' in text


def test_secret_escalation_is_block():
    text = _skill_text()
    assert "secret-detected-in-content" in text
    assert "Escalate every `secret-detected-in-content` violation to **BLOCK**" in text
    # A secret is the one witnessed fact in the pass, not a judgment.
    assert "witnessed fact, not a judgment" in text


def test_secret_pass_notes_false_positive_tail():
    """detect-secrets has a known FP tail; the author must be able to override."""
    text = _skill_text()
    assert "known false-positive tail" in text
    for needle in ("test fixtures", "UUIDs"):
        assert needle in text, f"secret FP tail note omits {needle!r}"
    # The override is the author's, not the review silently dropping the finding.
    assert "if it is a test fixture, it is safe to keep" in text


def test_ruby_authz_is_presence_only_advisory_fix_never_block():
    text = _skill_text()
    assert "Ruby controller authorization (advisory FIX, presence-only)" in text
    assert "Raise a **FIX** (never BLOCK)" in text
    # The honest label: presence-only, cannot confirm the action is covered.
    assert "cannot confirm the new action is covered" in text
    # No fabricated structured cite: the profile does not map callbacks to actions.
    assert "does NOT map a callback to the action methods it guards" in text
    assert 'do not cite a "witness authz divergence"' in text


def test_ts_authz_is_skipped():
    """No route/middleware extraction for TS, so there is no honest presence cite."""
    text = _skill_text()
    assert "Skip this check entirely for TypeScript" in text
    assert "no route/middleware/controller extraction for those languages" in text


def test_taint_ssrf_traversal_capped_at_fix_single_hunk():
    text = _skill_text()
    assert "Tainted input, SSRF, path traversal (advisory FIX, single-hunk scope)" in text
    assert "Cap every one at **FIX** (never BLOCK)" in text
    # The honest scope label: single-hunk, may miss cross-file, may FP on
    # out-of-hunk sanitization.
    assert "advisory, single-hunk scope" in text
    assert "may miss a flow whose source and sink are in different files" in text


def test_taint_cited_line_must_be_inside_the_diff():
    text = _skill_text()
    assert "The cited tainted line MUST be inside the diff" in text
    # A flow whose source/sink is off the change is the cross-file case this pass
    # cannot see, so it is dropped, not guessed.
    assert "do not raise the finding" in text


def test_judgments_never_claim_the_witnessed_fact_guarantee():
    text = _skill_text()
    # The pass must keep the secret (witnessed) and authz/taint (judgment) tiers
    # separate; the weaker two never borrow the secret's confidence.
    assert "do not claim they honor the integrity/calibration guarantee" in text
    assert "the secret finding (2.6a) is the only witnessed fact in this pass" in text


def test_security_findings_have_their_own_output_section():
    text = _skill_text()
    assert "### Security findings" in text


def test_severity_table_caps_authz_and_taint_at_fix():
    text = _skill_text()
    assert "Authz and taint/SSRF/traversal findings are capped at FIX" in text
    assert "only a secret detection blocks from the security pass" in text


def test_hunk_gate_covers_taint_but_not_secret_findings():
    text = _skill_text()
    # 2.6c findings go through the hunk gate like every other per-line finding.
    assert "the taint/SSRF/traversal findings from Step 2.6c" in text
    # Secret BLOCKs carry their own scanner line and are in-change by construction.
    assert "The secret BLOCKs from Step 2.6a carry their own line" in text


def test_verdict_rule_makes_secret_drive_block():
    text = _skill_text()
    assert "A secret detected in the diff (Step 2.6a) is a BLOCK and drives a BLOCK verdict" in text
    # The advisory findings never force a BLOCK verdict on their own.
    assert "never force a BLOCK verdict on their own" in text
