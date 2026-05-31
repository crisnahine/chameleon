"""Act 12: PR review (/chameleon-pr-review) end-to-end (Phases 38, 39).

Covers /chameleon-pr-review, which had ZERO end-to-end coverage before this act.

The act stages a branch diff on the already-bootstrapped + trusted ts_basic
fixture (trust granted in act_02 / phase 7) with two deliberate defects:

  - VIOLATION file (src/utils/format_date.ts): rewritten to break the util
    archetype convention. The codebase util shape is a named, camelCase
    `export function fooBar(...)` with no default export. The staged version
    switches to a default-exported snake_case arrow const, which diverges from
    the canonical witness and the detected naming/export conventions.

  - LOGIC-GAP file (src/utils/parse_count.ts): a new util with a clear runtime
    gap — it calls Number.parseInt on a possibly-undefined argument and divides
    by a value that can be zero, with no guard. This is the "logic findings"
    lens of the review (only the convention lens is fully data-backed; the logic
    lens is exercised but asserted softly).

Phase 38 drives the review and asserts, with STRONG evidence that prose alone
cannot fake:
  - the run made >=1 real get_pattern_context tool_use AND >=1 real lint_file
    tool_use (matched against the parsed assistant tool_use block names, not a
    whole-transcript substring — the SKILL.md body text mentions those tool
    names verbatim, so a substring match is satisfied just by invoking the
    command),
  - inside the sentinel-delimited review span (<<<CHAMELEON_REVIEW>>> ...
    <<<END_REVIEW>>>) only: a Verdict line, the BLOCK / FIX / NIT severity
    labels (matched with word boundaries so "fixture"/"init" cannot satisfy
    them), and a finding line that names format_date.ts together with explicit
    divergence reasoning (default export / snake_case / naming / export
    divergence / canonical).

Promotion (SKIP->PASS, incomplete-FAIL->PASS) only happens when that strong
evidence holds. Absent it, SKIP stays SKIP and incomplete-FAIL stays FAIL; the
old behavior of promoting on substring presence alone is gone.

Phase 39 is the anti-hallucination guard: every `path:line` reference the
review emits must point at a file that actually exists in the staged diff.
A reference to a file that is not in the changeset is an invented finding.

Phase 38 ALSO carries a best-effort PostToolUse / posttool-verify assertion
(Deliverable 2): the runner's parse_stream_json already captures PostToolUse
hook_response events (same channel as PreToolUse), so when the act edits the
off-archetype file in place, posttool-verify should emit a
`[\U0001f98e chameleon: ... violations]` deviation block. That check is SOFT:
if it does not fire it downgrades to a CONCERN note and never fails the phase,
because verify behavior depends on cooldown/enforcement state carried over from
earlier acts in the same per-run data dir.
"""

from __future__ import annotations

import re

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext

# Files the staged diff touches. The anti-hallucination guard (phase 39) only
# accepts findings that reference one of these relative paths.
_CHANGED_FILES = (
    "src/utils/format_date.ts",
    "src/utils/parse_count.ts",
)

# The convention-violating rewrite of an existing util. Default-exported,
# snake_case arrow const — the opposite of the util archetype's named-camelCase
# `export function` shape.
_VIOLATION_CONTENT = """\
const format_date = (date) => {
  return date.toISOString().slice(0, 10);
};

export default format_date;
"""

# New util with a clear, un-guarded runtime gap: parseInt on a maybe-undefined
# arg, then a divide that can be by zero. No null/empty guard.
_LOGIC_GAP_CONTENT = """\
export function parseCount(raw, total) {
  const n = Number.parseInt(raw, 10);
  return n / total;
}
"""

_PROMPT_BODY = (
    """\
You are exercising /chameleon-pr-review against working/ts_basic. That fixture
already has a bootstrapped AND trusted .chameleon/ profile from an earlier act —
do NOT re-bootstrap and do NOT re-trust. Use ABSOLUTE paths for every file
reference and every git command (run git with `git -C <abs path to ts_basic>`).

PHASE 38 - pr-review surfaces convention + logic findings:
  FIRST: emit checkpoint started phase 38 NOW.

  STEP 1 - stage a defective branch diff (Bash only, deterministic):
    Let TS=<absolute path to working/ts_basic>.
    a. Create a review branch off main:
         git -C "$TS" checkout -b pr-review-fixture
    b. Overwrite $TS/src/utils/format_date.ts so it VIOLATES the util
       convention. Use the Edit or Write tool (NOT a heredoc) so the
       PostToolUse verify hook observes the edit. The new content must be
       EXACTLY:
"""
    + _VIOLATION_CONTENT
    + """
    c. Create a NEW file $TS/src/utils/parse_count.ts with a logic gap. Again
       use the Write tool so the hooks observe it. The content must be EXACTLY:
"""
    + _LOGIC_GAP_CONTENT
    + """
    d. Commit both on the branch:
         git -C "$TS" add -A
         git -C "$TS" commit -q -m "staged review fixture: util violation + logic gap"
    e. Confirm the diff is what you expect:
         git -C "$TS" diff main...HEAD --name-only
       It should list exactly src/utils/format_date.ts and
       src/utils/parse_count.ts.

  STEP 2 - run the review:
    From inside working/ts_basic, run /chameleon-pr-review with NO arguments
    (convention-only review of the current branch vs main).
    The command MUST:
      - call chameleon-mcp::get_pattern_context on each changed file's ABSOLUTE
        path,
      - call chameleon-mcp::lint_file for each changed file's archetype,
      - read .chameleon/principles.md.
    Let the command produce its full review in your transcript.

  STEP 3 - verify the review output:
    Take the FULL review the command produced and re-emit it ONCE as a plain
    text block wrapped between these exact sentinel lines (each sentinel on its
    own line, OUTSIDE any code fence):

      <<<CHAMELEON_REVIEW>>>
      ...the entire review verbatim: Verdict line, every BLOCK/FIX/NIT finding
      with its file references...
      <<<END_REVIEW>>>

    The wrapped review MUST contain ALL of:
      - a "Verdict:" line (one of APPROVE / APPROVE WITH NITS / NEEDS CHANGES /
        BLOCK),
      - the severity structure with at least the BLOCK, FIX, and NIT labels
        present as headings or bullets (use the literal words BLOCK, FIX, NIT),
      - at least one finding LINE that names src/utils/format_date.ts AND, in
        that same line, calls out the divergence using one of: "default export",
        "snake_case", "naming", "export divergence", or "canonical". This is the
        staged violation; it must be caught with that specific reasoning.
    If the review did NOT flag format_date.ts as a convention divergence, emit
    the phase 38 checkpoint as FAILED with notes describing what it surfaced
    instead. Otherwise emit phase 38 as PASSED.

  ANTI-HALLUCINATION REMINDER: every BLOCK and FIX must reference real chameleon
  data (a lint violation, a canonical mismatch, a convention entry, a principle).
  Do NOT invent a file:line that is not in the staged diff. The only files in
  this diff are src/utils/format_date.ts and src/utils/parse_count.ts.

  emit checkpoint completed phase 38 (passed or failed per STEP 3).

PHASE 39 - anti-hallucination: no invented file references:
  emit checkpoint started phase 39
  Re-read every finding the review emitted. List every distinct file path the
  review referenced in a BLOCK or FIX finding. For each one, confirm it is one
  of the two staged files (src/utils/format_date.ts, src/utils/parse_count.ts).
  If the review referenced any OTHER file path in a BLOCK/FIX finding, that is a
  hallucinated finding — emit phase 39 FAILED listing the invented path(s).
  Otherwise emit phase 39 PASSED.
  emit checkpoint completed phase 39.

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing the fixture directory.
"""
)


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_12.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=45,
        allowed_tools=[
            "Bash",
            "Read",
            "Edit",
            "Write",
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__get_archetype",
            "mcp__plugin_chameleon_chameleon-mcp__get_canonical_excerpt",
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__lint_file",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
        ],
        plugin_root=ctx.plugin_root,
        permission_mode="bypassPermissions",
        timeout_s=1200,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[38, 39]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""

    # ---- Phase 38 cross-check: real tool evidence + sentinel-scoped findings ----
    #
    # Two layers of evidence, both of which prose alone cannot fake:
    #   1. The run made real get_pattern_context AND lint_file tool_use calls.
    #      A tool_use block name is emitted by the model invoking the tool, not
    #      by SKILL.md body text landing in the transcript, so it is immune to
    #      the substring false-green this act used to have.
    #   2. The verdict / severity / format_date.ts divergence checks run ONLY
    #      inside the <<<CHAMELEON_REVIEW>>> ... <<<END_REVIEW>>> span the model
    #      was told to wrap its review in. Severity labels use word boundaries
    #      so "fixture"/"init" cannot satisfy BLOCK/FIX/NIT.
    try:
        problems: list[str] = []

        # --- Layer 1: real MCP tool_use evidence (cannot be faked by prose) ---
        tool_uses = session.tool_uses
        gpc_calls = sum(1 for name in tool_uses if "get_pattern_context" in name)
        lint_calls = sum(1 for name in tool_uses if "lint_file" in name)
        if gpc_calls < 1:
            problems.append(
                "no real get_pattern_context tool_use observed "
                f"(tool_use names seen: {sorted(set(tool_uses))!r})"
            )
        if lint_calls < 1:
            problems.append(
                "no real lint_file tool_use observed "
                f"(tool_use names seen: {sorted(set(tool_uses))!r})"
            )

        # --- Layer 2: scope review-text checks to the sentinel span only ---
        span_match = re.search(
            r"<<<CHAMELEON_REVIEW>>>(.*?)<<<END_REVIEW>>>",
            transcript_text,
            re.DOTALL,
        )
        if not span_match:
            problems.append(
                "no <<<CHAMELEON_REVIEW>>> ... <<<END_REVIEW>>> span in transcript; "
                "review was not produced in the required, attributable form"
            )
            review_span = ""
        else:
            review_span = span_match.group(1)

        span_lower = review_span.lower()

        if "verdict" not in span_lower:
            problems.append("no 'Verdict:' line inside the review span")

        # Word-boundary severity labels so 'fixture'/'init'/'prefix' can't match.
        severity_labels = [
            lbl for lbl in ("block", "fix", "nit") if re.search(rf"\b{lbl}\b", span_lower)
        ]
        if len(severity_labels) < 2:
            problems.append(
                f"BLOCK/FIX/NIT severity structure absent in span; found {severity_labels!r}"
            )

        # Positive divergence evidence: a finding LINE naming format_date.ts AND,
        # in the same line, explicit divergence reasoning. Naming the file alone
        # (or echoing the staged content) is not enough.
        divergence_terms = (
            "default export",
            "snake_case",
            "naming",
            "export divergence",
            "canonical",
        )
        divergence_line = None
        for raw_line in review_span.splitlines():
            line_lower = raw_line.lower()
            if "format_date.ts" in line_lower and any(t in line_lower for t in divergence_terms):
                divergence_line = raw_line.strip()
                break
        if divergence_line is None:
            if "format_date.ts" not in span_lower:
                problems.append(
                    "review span never references the staged violation file format_date.ts"
                )
            else:
                problems.append(
                    "format_date.ts is named but no finding line ties it to a divergence "
                    "(default export / snake_case / naming / export divergence / canonical)"
                )

        if problems:
            notes_extra[38] = "; ".join(problems)
            cross_check_passed[38] = False
        else:
            cross_check_passed[38] = True
    except Exception as e:
        notes_extra[38] = f"phase 38 cross-check error: {e}"
        cross_check_passed[38] = False

    # ---- Phase 38 best-effort PostToolUse / posttool-verify deviation check ----
    # Deliverable 2: parse_stream_json already captures PostToolUse hook_response
    # events on the same channel as PreToolUse, so no runner plumbing change is
    # needed. The off-archetype in-place edit of format_date.ts should trip
    # posttool-verify. This is SOFT: a miss becomes a CONCERN note, never a FAIL.
    try:
        posttool_events = [
            e
            for e in session.hook_events
            if e.hook_name == "PostToolUse" and "<chameleon-context>" in e.stdout
        ]
        verify_deviation = [
            e
            for e in posttool_events
            if "violation" in e.stdout.lower() or "\U0001f98e" in e.stdout
        ]
        # Fall back to a transcript scan if hook_name attribution is unavailable
        # in this stream-json build (older CLIs surface PostToolUse text only in
        # the assistant context, not as a tagged hook_response event).
        transcript_has_verify = bool(
            re.search(r"\U0001f98e\s*chameleon:.*violation", transcript_text)
        )
        if not verify_deviation and not transcript_has_verify:
            concern = (
                "posttool-verify deviation block not observed for the off-archetype edit "
                "(soft check; may be suppressed by cooldown/enforcement state carried from "
                "earlier acts in this run)"
            )
            notes_extra[38] = (notes_extra.get(38, "") + "; " + concern).strip("; ")
    except Exception as e:
        notes_extra[38] = (
            notes_extra.get(38, "") + "; posttool-verify soft check error: " + str(e)
        ).strip("; ")

    # ---- Phase 39 cross-check: anti-hallucination on referenced file paths ----
    try:
        # Collect every `<path>:<line>` reference the review emitted.
        ref_paths = set(re.findall(r"([\w./\-]+\.(?:ts|tsx|js|jsx)):\d+", transcript_text))
        invented = [
            rp
            for rp in ref_paths
            if not any(rp.endswith(changed) or changed.endswith(rp) for changed in _CHANGED_FILES)
            # Ignore references to chameleon's own profile/data files and the
            # skill machinery; the guard only polices findings about source files
            # under the staged diff.
            and "/.chameleon/" not in rp
            and "node_modules" not in rp
        ]
        if invented:
            notes_extra[39] = (
                notes_extra.get(39, "")
                + "; "
                + f"review referenced file(s) not in the staged diff: {invented!r}"
            ).strip("; ")
            cross_check_passed[39] = False
        else:
            cross_check_passed[39] = True
    except Exception as e:
        notes_extra[39] = f"phase 39 cross-check error: {e}"
        cross_check_passed[39] = False

    # Promote SKIP->PASS / incomplete-FAIL->PASS ONLY when the strong cross-check
    # held. For phase 38 that means real get_pattern_context + lint_file tool_use
    # AND a sentinel-scoped format_date.ts divergence finding (see the phase 38
    # block above) — never on whole-transcript substring presence. If the strong
    # evidence is absent, cross_check_passed[phase] is False, so SKIP stays SKIP
    # and incomplete-FAIL stays FAIL.
    for phase, passed in cross_check_passed.items():
        if phase in outcomes and passed:
            if outcomes[phase].status == "SKIP":
                outcomes[phase].status = "PASS"
                outcomes[phase].notes = "promoted from SKIP by runner cross-check"
            elif outcomes[phase].status == "FAIL" and "phase incomplete" in outcomes[phase].notes:
                outcomes[phase].status = "PASS"
                outcomes[phase].notes = "promoted from incomplete-FAIL by runner cross-check"

    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="12_pr_review",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
