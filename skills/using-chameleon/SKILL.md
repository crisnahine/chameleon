---
name: using-chameleon
description: "Active when .chameleon/ profile directory exists. Explains hook-injected pattern context and violation feedback for TypeScript and Ruby on Rails."
---

<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific task, skip this skill - your parent has already established the pattern context.
</SUBAGENT-STOP>

# How chameleon works

Chameleon enforces codebase conventions through hooks. You don't call MCP tools - the hooks inject context automatically as `<chameleon-context>` blocks. Read and follow those blocks.

## Hook lifecycle

**SessionStart**: injects this skill + an optional drift banner. If you see `[🦎 chameleon: drift]`, the profile may be outdated - suggest `/chameleon-refresh` when appropriate.

**PreToolUse** (Edit/Write/NotebookEdit): tiered injection. Tier 1 (seen archetypes) injects a short pointer with pattern name and summary. Tier 2 (new or previously violated archetypes) injects the full canonical excerpt, confidence band, match quality, and team idioms. The header looks like:

    [🦎 chameleon: archetype=<name>, confidence=<band>, match_quality=<exact|ast|fallback|none>, sub_buckets=<N>]

- `match_quality`: how the canonical was matched. `exact` = same file pattern, `ast` = structural match, `fallback` = best guess, `none` = no canonical found. Weight the excerpt accordingly.
- `sub_buckets`: how many sub-clusters the archetype spans. 1 = tight cluster. 2+ = the archetype groups varied concerns - read the canonical more carefully.

**PostToolUse** (Edit/Write/NotebookEdit): lints the written file against its archetype. Violations appear in `updatedToolOutput` by default (inline with the tool result, not as a system reminder). Escalation is per-file: repeated violations for the same file escalate through L0 (silent fix) -> L1 (flagged) -> L2 (stop and fix). Chameleon stops verifying a file after 10 rapid corrections to avoid loops. There is a 30-second per-file cooldown - if you see `[🦎 chameleon: already verified this file]`, refer to the previous feedback.

**PostToolUse** (Bash): HMAC exec logging. No context injected, no action needed.

**UserPromptSubmit**: frustration detector. If the user sounds frustrated, chameleon surfaces `/chameleon-disable` and `/chameleon-pause-15m` as options.

## Trust gate

- **trusted**: canonical excerpts, rules, and idioms inject normally.
- **stale**: content injects with a warning that already suggests `/chameleon-trust`. Don't repeat the suggestion.
- **untrusted**: no canonical injection. A trust prompt fires once per session suggesting `/chameleon-trust`. Edits proceed without guidance until trust is granted.

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
| `/chameleon-init` (`/cham-init`) | Bootstrap a new profile |
| `/chameleon-refresh` (`/cham-refresh`) | Re-analyze repo, update profile after drift |
| `/chameleon-status` (`/cham-status`) | View profile state, drift, value attribution |
| `/chameleon-teach` (`/cham-teach`) | Capture a missed pattern as an idiom |
| `/chameleon-trust` (`/cham-trust`) | Approve a committed profile for this user |
| `/chameleon-disable` (`/cham-disable`) | Disable for the rest of this session |
| `/chameleon-pause-15m` (`/cham-pause-15m`) | Pause for 15 minutes |
| `/chameleon-doctor` (`/cham-doctor`) | Run health checks on the installation |
| `/chameleon-journey` (`/cham-journey`) | Run the end-to-end journey test harness |
