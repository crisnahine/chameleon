---
name: using-chameleon
description: "Active when .chameleon/ profile directory exists. Explains hook-injected pattern context and violation feedback for TypeScript, Ruby on Rails, and Python (Django/Flask/FastAPI)."
---

<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific task, skip this skill - your parent has already established the pattern context.
</SUBAGENT-STOP>

# How chameleon works

Chameleon enforces codebase conventions through hooks. You don't call MCP tools - the hooks inject context automatically as `<chameleon-context>` blocks. Read and follow those blocks.

## Hook lifecycle

**SessionStart**: injects this skill + an optional drift banner. If you see `[🦎 chameleon: drift]`, the profile may be outdated - suggest `/chameleon-refresh` when appropriate. A `[🦎 chameleon: production drift]` banner means the locked production branch moved past the commit the profile was derived from - suggest `/chameleon-refresh` (it re-derives from the production tree directly; no need to checkout or pull that branch).

**PreToolUse** (Edit/Write/NotebookEdit): tiered injection. Tier 1 (seen archetypes) injects a short pointer with pattern name and summary. Tier 2 (new or previously violated archetypes) injects the full canonical excerpt, confidence band, match quality, and team idioms. The header looks like:

    [🦎 chameleon: archetype=<name>, confidence=<band>, match_quality=<exact|ast|fallback|none>, sub_buckets=<N>]

- `match_quality`: how the canonical was matched. `exact` = same file pattern, `ast` = structural match, `fallback` = best guess, `none` = no canonical found. Weight the excerpt accordingly.
- `sub_buckets`: how many sub-clusters the archetype spans. 1 = tight cluster. 2+ = the archetype groups varied concerns - read the canonical more carefully.

**PostToolUse** (Edit/Write/NotebookEdit): lints the written file against its archetype. Violations are surfaced through the PostToolUse `additionalContext` channel. Escalation is per-file: repeated violations for the same file escalate through L0 (silent fix) -> L1 (flagged) -> L2 (stop and fix). Chameleon stops verifying a file after 10 rapid corrections to avoid loops. There is a 30-second per-file cooldown - if you see `[🦎 chameleon: already verified this file]`, refer to the previous feedback.

**PostToolUse** (Bash): HMAC exec logging. No context injected, no action needed.

**UserPromptSubmit**: frustration detector. If the user sounds frustrated, chameleon surfaces `/chameleon-disable` and `/chameleon-pause-15m` as options.

## Trust gate

- **trusted**: canonical excerpts, rules, and idioms inject normally.
- **stale**: content injects with a warning that already suggests `/chameleon-trust`. Don't repeat the suggestion.
- **untrusted**: no canonical injection. A trust prompt fires once per session suggesting `/chameleon-trust`. Edits proceed without guidance until trust is granted.

## Enforcement

Most chameleon feedback is advisory - it shapes the code you write but never blocks. A small set of high-confidence violations can block, gated so they only fire when they will not produce false positives.

**Three block points:**

- **PreToolUse deny** — a hard violation in the *proposed* content of an Edit/Write blocks the call before it runs: a banned/competing import (`import-preference-violation`), a hardcoded credential (`secret-detected-in-content`), or a dynamic `eval()`/`exec()` (`eval-call`). The credential and eval denies are archetype-independent — they fire on any file, including a brand-new one with no resolved archetype.
- **PostToolUse block** — a hard-class violation (`phantom-import`, `naming-convention-violation`, `inheritance-convention-violation`, and other calibrated rules) on a file already escalated to L2, when the archetype match is high-confidence AST, blocks the edit.
- **Stop backstop** — at turn end, an unresolved hard-class violation on a touched file refuses to end the turn (bounded by a per-session cap).
- **Idiom review** — `enforcement.idiom_review` (default on). When a turn edited files governed by team idioms/principles and no lint block fired, the Stop hook blocks ONCE per session to make you self-review those changes against the idioms (`idioms.md`) and principles (`principles.md`). Fix any clear violation, then end again to confirm. To skip the check, add `// chameleon-ignore idioms` (`# chameleon-ignore idioms` in Ruby) in a file you touched. `enforcement.idiom_judge` (opt-in, default off) strengthens the directive to demand a thorough review; it does not yet spawn an independent judge.
- **Correctness judge** — `enforcement.correctness_judge` (default on; advisory, never blocks). Once per session at turn end, a separate reviewer model reads the turn's diffs for correctness bugs (inverted conditions, missing guards, dropped awaits) and its findings arrive as a `[🦎 chameleon: independent review flagged ...]` context block. Treat each finding as a lead from an independent reviewer: verify it against the code before acting — findings may be wrong, but do not dismiss them unread. It fails open (no `claude` CLI, timeout → no findings) and never delays more than its hard wall-clock budget.
- **Turn-end duplication** (`enforcement.duplication_review`, default on; advisory, never blocks). At turn end, each function the turn introduced is matched by body hash against the committed function catalog and functions added earlier this session. A match the judge confirms surfaces as a `[🦎 chameleon: N possible duplicates]` advisory naming the new function, the existing one it re-implements, and its path: reuse the existing function. Skipped on SubagentStop, capped per session, per-(file, content) deduplicated so an unchanged file is not re-judged each turn. Set `enforcement.duplication_review: false` to opt out.

**Modes** (from `.chameleon/config.json` `enforcement.mode`):

- `off` — advisory only, nothing blocks.
- `shadow` — default; logs would-have-blocked events but never blocks. A repo runs in shadow first so it measures before it enforces.
- `enforce` — real deny/block on rules calibration kept active for this repo.

Only block rules with a near-zero false-positive rate against the repo's own committed files stay active; the rest are demoted to advisory. This includes `naming-convention-violation` (TypeScript interface prefix) and `inheritance-convention-violation` (Ruby dominant base class), which block only when calibration confirms the repo's own files all conform. `/chameleon-status` shows the active set and any demoted rule with its measured fp_rate.

**Escape hatch:** a blocked edit is overridable inline with `// chameleon-ignore <rule>` (`# chameleon-ignore <rule>` in Ruby), or a bare `// chameleon-ignore` to suppress all chameleon blocks on that line. Exception: hard-class deterministic security facts — hardcoded credentials of a deterministic kind and error-severity `eval()` calls — are never covered by the bare form and must be named explicitly (`// chameleon-ignore secret-detected-in-content`, `// chameleon-ignore eval-call`); advisory-grade variants (entropy-based secret hits, warning-severity dynamic-eval idioms like `class_eval`) remain bare-suppressible. Place the directive ON the offending line, or alone on the line directly above it; `// chameleon-ignore-file <rule>` suppresses the rule for the whole file. The directive must end its line (trailing prose deactivates it) and directives inside string literals do not count. `CHAMELEON_ENFORCE=0` disables all blocking for the session regardless of mode.

When you hit a block, fix the violation or add the ignore directive if it is intentional - don't silently work around it.

## Canonical as witness

The canonical excerpt is a witness, not a template. Use its normative shape and idioms (naming, structure, import style) but not its specific business logic or idiosyncrasies. It shows how the codebase does things - match the pattern, not the content.

## Fail-open behavior

All hooks fail open. If chameleon can't reach the advisor, you'll see:

    [🦎 chameleon: degraded - advisor_unavailable]

When you see this: make the edit using your best inference from what you know about the codebase, and tell your human partner the advisory was unavailable and suggest `/chameleon-doctor`.

## Coordination with other skills

Chameleon is an output-layer advisory: archetype + canonical + rules shape the code you write. Process-gating skills (brainstorming, planning, TDD) run first if both fire on the same edit. Finish the process gate, then follow chameleon's pattern context for the actual write.

## Flow

    Edit/Write/NotebookEdit called
        |
    PreToolUse: trusted? --no--> untrusted prompt (once) --> edit proceeds without canonical
        |yes/stale
    Injects <chameleon-context> with archetype + canonical
        |
    Edit executes
        |
    PostToolUse: lint against archetype
        |
    Violations? --no--> done
        |yes
    Injects violation feedback --> model fixes --> next edit

## Available slash commands

| Command | Purpose |
|---------|---------|
| `/chameleon-init` | Bootstrap a new profile |
| `/chameleon-refresh` | Re-analyze repo, update profile after drift |
| `/chameleon-status` | View profile state, drift, value attribution |
| `/chameleon-teach` | Capture a missed pattern as an idiom |
| `/chameleon-auto-idiom` | Derive novel team idioms from repo evidence (append-only) |
| `/chameleon-trust` | Approve a committed profile for this user |
| `/chameleon-disable` | Disable for the rest of this session |
| `/chameleon-pause-15m` | Pause for 15 minutes |
| `/chameleon-doctor` | Run health checks on the installation |
| `/chameleon-journey` | Run the end-to-end journey test harness |
| `/chameleon-pr-review` | Review a branch/PR against repo conventions and task intent |
| `/chameleon-receiving-code-review` | Handle a review the team left on your PR: verify + adjudicate + draft replies + implement on approval |
| `/chameleon-explain` | Drill down on one enforcement rule or replay a file's last edit |
