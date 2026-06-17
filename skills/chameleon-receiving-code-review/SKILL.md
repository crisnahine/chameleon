---
name: chameleon-receiving-code-review
description: "Use when the user explicitly invokes /chameleon-receiving-code-review to handle a code review the team left on their PR — verify each comment against the code and the repo's chameleon conventions, decide apply-or-push-back, draft replies, and implement approved fixes one at a time."
---

# Receiving Code Review with Chameleon Context

A teammate reviewed your PR. This handles their feedback the way a senior engineer
does: verify before implementing, no performative agreement, push back with
technical reasoning -- enriched with chameleon's knowledge of the repo's actual
conventions, so a suggestion that contradicts the established canonical pattern is
a real reason to push back, not something to apply blindly.

## Core discipline (superpowers `receiving-code-review`)

- Verify before implementing; ask before assuming; technical correctness over
  social comfort.
- Forbidden responses: "You're absolutely right!", "Great point!", performative
  gratitude, and "let me implement that now" before verification.
- Response pattern: READ -> UNDERSTAND -> VERIFY -> EVALUATE -> GROUND -> RESPOND ->
  IMPLEMENT.

## Step 1: Gather the feedback

- **Pasted comments** (default): use what the user pasted.
- **PR URL**: fetch inline + general review comments -- `gh` for GitHub
  (`pulls/{n}/comments` carries `path`, `line`, `original_line`, `body`),
  `bbcurl` for Bitbucket (inline `path`/`from`/`to`). If `gh`/`bbcurl` is absent,
  unauthenticated, or returns empty, STOP and ask the user to paste -- never invent
  comments.
- **Jira key**: resolve the PR(s), then as above (see multi-PR below).

## Step 2: Normalize into a checklist

Each item: reviewer, `file:line` (nullable), the ask, type, and comment-class:
`inline-current` (line maps to the file), `inline-outdated` (carry `original_line`
+ `line`, mark "may have moved"), `file-level` (no line; whole-file), `general`
(no path; route to plain technical judgment, skip Step 4).

## Step 3: Verify each claim against the code

Open the cited `file:line` and read it. Prefer reading code/tests over executing.
If reproducing a "this breaks" claim needs execution: no installs, no network,
honor chameleon's refusal posture, and fail open to "I can't verify without
running X — should I?"

## Step 4: Adjudicate against chameleon conventions

FIRST call `get_pattern_context(file_path=<absolute path>)` to get `repo.id` and
`repo.trust_state`. Gate convention-based pushback on `trust_state == "trusted"`;
if untrusted/stale/absent, fall back to plain technical judgment labeled "profile
untrusted/absent" and suggest `/chameleon-trust`. Carry the `match_quality =
none/fallback` caveat. Reuse `repo.id` for the repo-scoped tools (`lint_file`,
`get_crossfile_context`, `get_callers`, `get_duplication_candidates`). Outcomes:
reviewer ALIGNS with the convention (strong apply), reviewer CONTRADICTS the
canonical (strong, evidence-backed pushback citing the witness), convention SILENT
(plain technical judgment, labeled).

## Step 5: Classify + order

Each item → AGREE / PUSH BACK / NEEDS CLARIFICATION / YAGNI (AGREE is an internal
triage label, not user-facing copy). YAGNI greps for actual usage first. Order:
blocking/bugs → simple → complex. If ANY item is unclear, STOP and ask before
implementing anything (this gate blocks Step 8 only).
