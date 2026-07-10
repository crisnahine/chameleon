"""Act 12b: PR review deep paths (secret / migration / dependency / sink) (Phases 40, 41).

act_12 proves only the Step 2 convention lens on a TS fixture. This act proves the
deterministic, BLOCK-eligible paths that had no end-to-end evidence, on the
already-bootstrapped + trusted rails_basic fixture (trust granted in act_07 /
phase 21):

  - Step 2.6a secret BLOCK: a hard-kind AWS access id (AKIA...) on an added line.
  - Step 2.7a irreversible-migration BLOCK: a `drop_table` inside a `def change`
    block under db/migrate/.
  - Step 2.6d deterministic sink BLOCK: a Ruby `eval(params[...])` on an added line.
  - Step 2.5a dependency ACK (NOT a verdict-driving BLOCK): a new `gem` in the
    Gemfile, which must land in the "Acknowledge before merge" section and must
    NOT appear as a BLOCK finding line.

The defective files are staged via Bash heredocs, NOT the Write tool: chameleon's
PreToolUse enforcement correctly DENIES writing a hard secret or an eval sink, so
the review reads them from the committed diff (the real PR shape). The two builder
lines below carry rule-scoped chameleon-ignore directives because the values are
known-fake fixtures; those Python comments stay in THIS file and never enter the
staged Ruby (which gets the real forms for the review under test to catch).

Phase 40 asserts, with evidence prose alone cannot fake:
  - real tool_use of lint_file AND scan_dependency_changes AND get_autopass_verdict,
  - inside the <<<CHAMELEON_REVIEW>>> ... <<<END_REVIEW>>> span: a BLOCK verdict,
    and BLOCK findings naming the migration (drop_table / irreversible), the
    secret (AKIA / secret / rotate), and the eval sink (eval-call / eval).

Phase 41 asserts the dependency ACK channel: the new gem appears in an ACK /
"Acknowledge before merge" context and is NOT on any BLOCK finding line.
"""

from __future__ import annotations

import re

from tests.journey.acts.act_base import ActResult, build_act_prompt, dispatcher_actions
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext

# A new migration with an irreversible op inside `def change` (Step 2.7a BLOCK).
_MIGRATION_PATH = "db/migrate/20260101000000_drop_legacy_things.rb"
_MIGRATION_CONTENT = """\
class DropLegacyThings < ActiveRecord::Migration[7.0]
  def change
    drop_table :legacy_things
  end
end
"""

# Known-fake fixture builders for the secret + eval sink lines. The rule-scoped
# ignores keep THIS test file clean; the comments are source-only and never enter
# the staged Ruby value below.
_SECRET_LINE = (
    '  LEGACY_ID = "AKIAIOSFODNN7EXAMPLE"\n'  # chameleon-ignore secret-detected-in-content
)
_EVAL_LINE = "    eval(params[:code])\n"  # chameleon-ignore eval-call

_SINK_PATH = "app/services/legacy_runner.rb"
_SINK_CONTENT = (
    "class LegacyRunner\n" + _SECRET_LINE + "  def run(params)\n" + _EVAL_LINE + "  end\n" + "end\n"
)

# The new direct dependency added to the Gemfile (Step 2.5a ACK, NOT a BLOCK).
_NEW_GEM = "leftpad"

_PROMPT_BODY = (
    """\
You are exercising /chameleon-pr-review against working/rails_basic. That fixture
already has a bootstrapped AND trusted .chameleon/ profile from an earlier act —
do NOT re-bootstrap and do NOT re-trust. Use ABSOLUTE paths for every file
reference and run git with `git -C <abs path to rails_basic>`.

PHASE 40 - pr-review surfaces the deterministic BLOCK paths:
  FIRST: emit checkpoint started phase 40 NOW.

  STEP 1 - stage a defective branch diff. Stage every file below with a Bash
  heredoc (cat > "$RB/<path>" <<'EOF' ... EOF), NOT the Edit/Write tools:
  chameleon's PreToolUse enforcement correctly denies writing a hard secret and
  an eval sink, and this PR must contain them for the review to catch from the
  committed diff.
    Let RB=<absolute path to working/rails_basic>.
    a. git -C "$RB" checkout -b pr-review-deep-fixture
    b. Create (via `cat > "$RB/<path>" <<'EOF' ... EOF`, NOT the Write tool) $RB/"""
    + _MIGRATION_PATH
    + """ with EXACTLY this content:
"""
    + _MIGRATION_CONTENT
    + """
    c. Create (via `cat > "$RB/<path>" <<'EOF' ... EOF`, NOT the Write tool) $RB/"""
    + _SINK_PATH
    + """ with EXACTLY this content:
"""
    + _SINK_CONTENT
    + """
    d. Append a new dependency line to $RB/Gemfile (append only, remove nothing):
         gem \""""
    + _NEW_GEM
    + """\"
    e. git -C "$RB" add -A && git -C "$RB" commit -q -m "deep review fixture: migration + secret + eval + new gem"
    f. Confirm: git -C "$RB" diff main...HEAD --name-only

  STEP 2 - run the review:
    From inside working/rails_basic, run /chameleon-pr-review with NO arguments.
    It MUST call chameleon-mcp::lint_file on the changed source files,
    chameleon-mcp::chameleon_review with action="scan_dependency_changes" for
    the Gemfile change, and chameleon-mcp::chameleon_review with
    action="get_autopass_verdict" once. Let it produce the full review.

  STEP 3 - re-emit the FULL review ONCE between these exact sentinel lines (each
  on its own line, OUTSIDE any code fence):
      <<<CHAMELEON_REVIEW>>>
      ...the entire review verbatim: Verdict line, every BLOCK/FIX/NIT finding,
      and the Acknowledge-before-merge section...
      <<<END_REVIEW>>>
    The review MUST be a BLOCK verdict and MUST contain BLOCK findings for:
      - the irreversible migration (drop_table inside def change),
      - the hard-kind secret (AKIA... / rotate),
      - the eval sink (eval-call).
    If any of those three is missing, emit phase 40 FAILED with notes. Otherwise
    emit phase 40 PASSED.

  emit checkpoint completed phase 40 (passed or failed per STEP 3).

PHASE 41 - the new dependency is an ACK, not a BLOCK:
  emit checkpoint started phase 41
  In the SAME review, confirm the new gem """
    + f'"{_NEW_GEM}"'
    + """ appears in the
  "Acknowledge before merge" / ACK section and is NOT on any BLOCK finding line
  (a new dependency is a provenance acknowledgement, never a verdict-driving
  BLOCK). If the gem is rendered as a BLOCK finding, emit phase 41 FAILED.
  Otherwise emit phase 41 PASSED.
  emit checkpoint completed phase 41.

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
"""
)


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("rails_basic")
    transcript = ctx.run_dir / "transcripts" / "act_12b.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={
            **ctx.env,
            "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file),
        },
        transcript_path=transcript,
        max_turns=50,
        allowed_tools=[
            "Bash",
            "Read",
            "Edit",
            "Write",
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__lint_file",
            "mcp__plugin_chameleon_chameleon-mcp__get_crossfile_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_contract_breaks",
            # scan_dependency_changes / get_autopass_verdict /
            # record_review_verdict route via the review dispatcher.
            "mcp__plugin_chameleon_chameleon-mcp__chameleon_review",
        ],
        plugin_root=ctx.plugin_root,
        permission_mode="bypassPermissions",
        timeout_s=1200,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[40, 41]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}
    transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""

    span_match = re.search(
        r"<<<CHAMELEON_REVIEW>>>(.*?)<<<END_REVIEW>>>", transcript_text, re.DOTALL
    )
    review_span = span_match.group(1) if span_match else ""
    span_lower = review_span.lower()

    # ---- Phase 40: real tool evidence + the three deterministic BLOCK findings ----
    try:
        problems: list[str] = []
        tool_uses = session.tool_uses
        # lint_file is still a top-level tool (name-matched); the two review
        # operations route through the chameleon_review dispatcher, so their
        # evidence is the tool_use block's `action` input, not its name.
        if not any("lint_file" in name for name in tool_uses):
            problems.append(
                f"no real lint_file tool_use observed (seen: {sorted(set(tool_uses))!r})"
            )
        review_actions = dispatcher_actions(session, "chameleon_review")
        for needed in ("scan_dependency_changes", "get_autopass_verdict"):
            if needed not in review_actions:
                problems.append(
                    f"no real chameleon_review tool_use with action={needed!r} "
                    f"observed (actions seen: {sorted(set(review_actions))!r})"
                )

        if not review_span:
            problems.append("no <<<CHAMELEON_REVIEW>>> ... <<<END_REVIEW>>> span in transcript")
        else:
            # Tie the BLOCK verdict to the actual Verdict line, not any BLOCK heading.
            verdict_line = next(
                (ln for ln in review_span.splitlines() if "verdict" in ln.lower()), ""
            )
            if not re.search(r"\bblock\b", verdict_line.lower()):
                problems.append(f"verdict line is not BLOCK (got: {verdict_line.strip()!r})")
            # Require the specific irreversible-migration evidence, not generic 'migration'
            # (the file path/section is echoed regardless of whether it was flagged).
            if not ("drop_table" in span_lower or "irreversible" in span_lower):
                problems.append(
                    "no irreversible-migration BLOCK finding (drop_table/irreversible) in span"
                )
            if not ("akia" in span_lower or "secret" in span_lower or "rotate" in span_lower):
                problems.append("no secret BLOCK finding in span")
            if not (
                "eval-call" in span_lower or "eval(" in span_lower or "eval sink" in span_lower
            ):
                problems.append("no eval-sink (2.6d) BLOCK finding in span")

        cross_check_passed[40] = not problems
        if problems:
            notes_extra[40] = "; ".join(problems)
    except Exception as e:  # noqa: BLE001
        notes_extra[40] = f"phase 40 cross-check error: {e}"
        cross_check_passed[40] = False

    # ---- Phase 41: the new gem is an ACK, never a BLOCK finding line ----
    try:
        problems_41: list[str] = []
        if not review_span:
            problems_41.append("no review span; cannot verify the dependency ACK")
        gem_in_block = False
        gem_acked = False
        in_ack_section = False
        for raw in review_span.splitlines():
            low = raw.lower()
            if "acknowledge before merge" in low:
                in_ack_section = True
            if _NEW_GEM in low:
                # Only a real BLOCK label/bullet counts — NOT the bare word 'block'
                # inside the ACK line's natural phrasing ('does not block').
                norm = (
                    low.replace("does not block", "")
                    .replace("doesn't block", "")
                    .replace("not block", "")
                )
                if "block:" in norm or "**block" in norm or re.search(r"^\s*[-*]\s*block\b", norm):
                    gem_in_block = True
                if "ack" in low or in_ack_section:
                    gem_acked = True
        if gem_in_block:
            problems_41.append(f"new gem {_NEW_GEM!r} rendered as a BLOCK finding (must be an ACK)")
        if review_span and not gem_acked:
            problems_41.append(
                f"new gem {_NEW_GEM!r} not surfaced in an ACK / Acknowledge-before-merge context"
            )
        cross_check_passed[41] = not problems_41
        if problems_41:
            notes_extra[41] = "; ".join(problems_41)
    except Exception as e:  # noqa: BLE001
        notes_extra[41] = f"phase 41 cross-check error: {e}"
        cross_check_passed[41] = False

    for phase, passed in cross_check_passed.items():
        if phase not in outcomes:
            continue
        if passed:
            if outcomes[phase].status == "SKIP":
                outcomes[phase].status = "PASS"
                outcomes[phase].notes = "promoted from SKIP by runner cross-check"
            elif outcomes[phase].status == "FAIL" and "phase incomplete" in outcomes[phase].notes:
                outcomes[phase].status = "PASS"
                outcomes[phase].notes = "promoted from incomplete-FAIL by runner cross-check"
        elif outcomes[phase].status == "PASS":
            # DEMOTE: a self-reported PASS that fails the evidence cross-check is a
            # real failure, not a CONCERN note. The runner evidence is authoritative.
            outcomes[phase].status = "FAIL"
            outcomes[phase].notes = "demoted from self-reported PASS by runner cross-check"

    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="12b_pr_review_deep",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
