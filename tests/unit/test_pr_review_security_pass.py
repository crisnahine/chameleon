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
    """Body plus lazily-loaded references — the skill's full procedure text."""
    refs = sorted(SKILL.parent.glob("references/*.md"))
    parts = [SKILL.read_text(encoding="utf-8")] + [p.read_text(encoding="utf-8") for p in refs]
    return "\n".join(parts)


def test_security_pass_step_present_and_always_runs():
    text = _skill_text()
    assert "Step 2.6: Security pass" in text
    # The pass is not gated on a ticket; it covers the no-ticket open-source case.
    assert "every changed source file regardless of whether a Jira ticket" in text


def test_lint_runs_on_every_file_even_with_no_archetype():
    """The secret scan needs lint_file to run on every changed file (source or not),
    even when no archetype matches."""
    text = _skill_text()
    assert "Run this on every changed FILE (source or not), even when no archetype matches" in text
    # The reason: the secret scan precedes the archetype match and trust gate.
    assert "scans for secrets before it looks at the archetype" in text
    assert 'just because `match_quality` is "none"' in text


def test_secret_escalation_is_block():
    text = _skill_text()
    assert "secret-detected-in-content" in text
    # Kind gate: only secret_hard violations may BLOCK; the soft heuristics cap at NIT.
    assert "Escalate to **BLOCK** only violations whose `secret_hard` field is true" in text
    assert "report them at most as a **NIT**" in text
    # Hunk gate: a hard-kind secret inside an added/changed hunk is a BLOCK; an
    # out-of-hunk hard secret goes to the repo-hygiene note and does not affect the verdict.
    assert "falls inside an added/changed hunk of this diff is a **BLOCK**" in text
    assert '"Pre-existing repo hygiene" note' in text


def test_secret_pass_notes_false_positive_tail():
    """The low-precision secret heuristics have a known FP tail. They cap at NIT
    and the hard-kind override stays the author's, not a silent drop."""
    text = _skill_text()
    # The soft kinds (base64 runs, hex, password assignments) match ordinary code,
    # so they are capped at NIT with a verify-by-eye label, never FIX or BLOCK.
    assert '"low-precision secret heuristic, verify by eye"' in text
    assert "never as FIX or BLOCK, and never let them influence the verdict" in text
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
    assert (
        "the secret finding (2.6a) and the deterministic lint sinks (2.6d below) "
        "are the witnessed facts in this pass" in text
    )


def test_security_findings_have_their_own_output_section():
    text = _skill_text()
    assert "### Security findings" in text


def test_severity_table_caps_authz_and_taint_at_fix():
    text = _skill_text()
    assert "Authz and taint/SSRF/traversal findings (2.6b/2.6c) are capped at FIX" in text
    # Two witnessed facts block from the security pass on an added/changed line: a
    # hard-kind secret AND a deterministic eval-call/command-injection sink (2.6d).
    # Soft heuristics cap at NIT; out-of-hunk hard secrets/sinks go to repo-hygiene.
    assert "Two witnessed facts in the security pass DO block on an added/changed line" in text
    assert "Low-precision secret heuristics cap at NIT" in text
    assert "out-of-hunk hard secrets and out-of-hunk sinks go to the repo-hygiene note" in text


def test_hunk_gate_covers_taint_and_secret_findings():
    text = _skill_text()
    # 2.6c findings go through the hunk gate like every other per-line finding.
    assert "the taint/SSRF/traversal findings from Step 2.6c" in text
    # Secrets are now hunk-gated too: the scanner reads the full file, so an
    # out-of-hunk hard secret is routed to the repo-hygiene note, not the verdict.
    assert "AND the secret findings from Step 2.6a" in text
    assert (
        'an out-of-hunk hard-kind secret goes to the "Pre-existing repo hygiene" '
        "note (Step 2.6a) instead of the verdict" in text
    )


def test_verdict_rule_makes_secret_drive_block():
    text = _skill_text()
    # Only a hard-kind secret on an added/changed line drives a BLOCK verdict:
    # the kind gate keeps low-precision heuristics out of the verdict, the hunk
    # gate keeps pre-existing repo hygiene out of it.
    assert (
        "A hard-kind secret on an added/changed line (Step 2.6a, both gates passed) "
        "is a BLOCK and drives a BLOCK verdict" in text
    )
    assert "Pre-existing-hygiene secret/sink notes never affect the verdict" in text
    # The advisory findings never force a BLOCK verdict on their own.
    assert "never force a BLOCK verdict on their own" in text
