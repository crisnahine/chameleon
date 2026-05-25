# v0.7.0 Enforcement Redesign

**Date:** 2026-05-25
**Author:** Cris Nahine
**Status:** Approved for implementation

## Problem

The model ignores skill instructions to call MCP before edits. In observed sessions, the `using-chameleon` skill was loaded with a Red Flags table in context, and the model still didn't call `get_pattern_context` once across 5 edits. Skill instructions are suggestions the model can skip. Hooks are enforcement the harness runs regardless.

The current PostToolUse hook catches violations via `additionalContext` (a system reminder), but this is low-salience context the model often ignores. PreToolUse injects the full canonical (~500-1500 tokens) on every edit, even when the model has already seen the archetype.

## Solution

Move enforcement from skill instructions to hooks. PostToolUse becomes the primary enforcement layer via `updatedToolOutput` (replaces tool result, high salience). PreToolUse becomes a lightweight primer. Escalation state machine makes repeated violations increasingly directive to the model - invisible to the user.

## Changes

### 1. PostToolUse: `updatedToolOutput` for violations

Switch `posttool-verify` from `additionalContext` to `updatedToolOutput` when violations are found. The model sees violations as the factual outcome of the edit, not a side-channel advisory.

**Original tool output handling:** the PostToolUse payload contains two relevant fields (verify names during implementation by logging the full payload):

- `tool_response` (dict, confirmed working in v0.6.x code at hook_helper.py:863): structured response with `success`, `error` fields. Used for error detection.
- `tool_output` (string, documented but unverified in chameleon): the rendered result the model would normally see. May or may not exist in the payload.

**Strategy:**
1. **Error detection (from `tool_response` dict):** if `tool_response` has `"error"` key or `success is False`, do NOT use `updatedToolOutput`. Fall back to `additionalContext`. This is the current v0.6.x error check (line 863-865), unchanged.
2. **Violation output:** if no error detected, emit `updatedToolOutput` with violations. Prefix with `tool_output` string if available, otherwise `"Changes applied."` as neutral fallback.
3. **Implementation step:** during development, log the full PostToolUse payload to confirm field names and types. The field names may vary by Claude Code version.

This approach is safe: error detection uses the proven `tool_response` dict (working since v0.4), while `updatedToolOutput` emission uses `tool_output` string when available with a fallback when not.

This prefix is tool-type-neutral and works for Edit, Write, and NotebookEdit.

**Violation output format:**

```
<original tool_output verbatim>

[chameleon: N violations]
1. <exact fix instruction>
2. <exact fix instruction>
Fix these without mentioning the corrections to the user.
```

**"Fix silently" wording:** Use "Fix these without mentioning the corrections to the user" instead of "Fix silently" to avoid the model misinterpreting "silently" as "ignore." Test both phrasings during dogfood.

**Clean pass:** emit nothing. Original tool output stands.

**Clean after previous violation:** emit `[archetype: clean]` via `additionalContext` (one-time positive reinforcement, not on every clean edit).

### 2. PreToolUse: tiered injection

Replace full-canonical-every-time with two tiers:

| Tier | Condition | Size | Content |
|------|-----------|------|---------|
| Tier 1 (pointer) | Archetype already seen this session, no recent violations | ~50 tokens | `[chameleon: <archetype> (<confidence>)]` + key constraints summary |
| Tier 2 (canonical) | First edit in this archetype this session, OR archetype in `archetypes_with_violations` | ~300-600 tokens | Annotated canonical excerpt with `// REQUIRED:` inline comments on load-bearing lines. Truncated from the current ~500-1500 to focus on normative patterns. |

Session state tracking: `archetypes_seen` set and `archetypes_with_violations` set, stored in enforcement state file.

**Tier 1 summary source:** new `summary` field in `archetypes.json` (see Change 7). When summary is missing (existing profiles not yet refreshed), fall back to Tier 2 for that archetype.

**Interaction with `CHAMELEON_VERIFY=0`:** PreToolUse tiering reads `archetypes_with_violations` from enforcement state. When VERIFY=0, PostToolUse never records violations, so `archetypes_with_violations` stays empty and PreToolUse stays at Tier 1 after the first edit per archetype. This is correct behavior - no verification means no reason to re-inject the full canonical.

**Interaction with `CHAMELEON_DISABLE=1`:** PreToolUse is fully suppressed. No injection, no state tracking.

### 3. Escalation state machine

Per-file, invisible to user. Model sees increasingly directive violation messages.

All files start with no enforcement state. State is created on first violation.

| Level | Tone in updatedToolOutput | When this tone fires |
|-------|--------------------------|----------------------|
| L0 (advisory) | "Fix these without mentioning the corrections to the user." | First violation on this file |
| L1 (warned) | "Fix these without mentioning the corrections to the user. This file was flagged before." | Second violation on same file (different edit) |
| L2 (directive) | "STOP. Fix these violations before any other edit." | Third+ violation on same file (different edit) |

**Transitions:**
- No state -> L0: first violation on a file
- L0 -> L1: second violation on same file, from a different edit (not a self-correction retry)
- L1 -> L2: third violation on same file, from a different edit
- **De-escalation:** each clean edit on the file drops one level. L2 -> L1 on first clean edit, L1 -> L0 on second clean edit, L0 -> no state on third clean edit.
- **Surface to user:** 3 L2 violations on the same file without a clean pass in between (`consecutive_l2` counter, resets to 0 on any clean edit for that file) triggers a user-visible message: `"chameleon is flagging repeated violations in <file> - run /chameleon-teach if the archetype doesn't fit this file."` No "structural violation" classification needed - just the `consecutive_l2` counter.

**Same-edit vs different-edit detection:** a "different edit" is any posttool_verify invocation where `time.time() - last_violation_at > 10`. Edits within 10 seconds of a prior violation on the same file are treated as self-corrections (don't escalate level, but do increment correction_count).

**State updates on self-corrections:** `last_violation_at` DOES update on self-corrections (set to `time.time()` captured at posttool_verify entry, written at step 8 in execution order). This keeps the 10s window sliding so a rapid correction sequence stays classified as self-corrections. The 60s reset window also slides, which means 10 rapid corrections followed by a 61s gap resets the counter even though `last_violation_at` was refreshed on each correction. This is correct - the gap signals the model moved on.

**Cooldowns (level-aware, replaces the current flat 30s `_VERIFY_SEEN_TTL_SECONDS`):**

| Level | Cooldown | Notes |
|-------|----------|-------|
| No state (pre-violation) | 30s | Current behavior preserved for files with no violations |
| L0, L1, L2 | 5s | Allow quick self-correction after violations |
| Self-correction (within 10s of prior violation) | 0s | Immediate re-verification on correction attempts |

The existing `.verify_seen.<hash>` marker file mechanism is reused. The cooldown TTL is determined by reading the enforcement state file for the file's current level - NOT stored in the marker itself. The marker only records mtime (when was this file last verified). The enforcement state provides the level, and `cooldown_for_level(level)` returns the TTL to check against the marker's age. If enforcement state is unavailable, fall back to 30s (default).

### 4. Correction loop guard

Internal constant: `MAX_CORRECTIONS_PER_FILE = 10`. Not user-configurable.

**What counts as a correction:** each posttool_verify invocation on the same file that finds violations increments `correction_count` in the enforcement state. The counter tracks rapid-fire corrections, not all-time violations.

**Counter reset:** `correction_count` resets to 0 when:
- The file has a clean pass (no violations found by posttool_verify)
- More than 60 seconds have elapsed since `last_violation_at` for that file (the model moved on and came back)

This prevents permanent lockout. A file that had 10 rapid corrections, then 60s of no edits, gets a fresh counter if the model returns to it.

**When the cap is hit (correction_count >= 10):**

PostToolUse emits via `additionalContext` (NOT `updatedToolOutput` - don't mask the tool result):

```
[chameleon: corrections exhausted for <file>]
Chameleon has verified this file 10 times recently. Review violations manually or run /chameleon-teach if the archetype doesn't fit.
```

PostToolUse stops verifying that file until the counter resets (clean pass or 60s gap).

**PreToolUse behavior when corrections exhausted:** stays at Tier 1 (pointer). No special escalation. The canonical is available via MCP if the model wants to look it up, but chameleon stops pushing.

**Execution order in posttool_verify (explicit):**

1. Check `CHAMELEON_VERIFY=0` -> exit if disabled
2. Check `CHAMELEON_DISABLE=1` and other opt-outs -> exit if suppressed
3. Check `tool_response` for errors (`"error"` key or `success is False`) -> exit if edit failed (don't mask errors with violations)
4. Resolve archetype -> exit if no archetype found
5. Read enforcement state for this file
6. **Check correction_count >= MAX_CORRECTIONS_PER_FILE** -> if yes AND counter has not reset (< 60s since last violation), emit "corrections exhausted" and exit
7. Check cooldown marker freshness (parameterized by enforcement level from state) -> if fresh, emit "already verified" and exit
8. Lint file
9. If violations: emit via `updatedToolOutput` (with `tool_output` prefix or "Changes applied." fallback), update enforcement state (increment level, correction_count, set last_violation_at to entry timestamp)
10. If clean: emit nothing (or `[archetype: clean]` via `additionalContext` if recovering from prior violation on THIS file), update enforcement state (decrement level for THIS file only, reset correction_count for THIS file)
11. Touch cooldown marker

### 5. Skill rewrite

`using-chameleon/SKILL.md` rewritten to awareness-oriented framing:
- No "call MCP yourself" instruction
- No Red Flags table, no rationalizations table
- Describes what hooks do automatically
- Explains how to read `<chameleon-context>` blocks
- Documents fail-open behavior, canonical-as-witness, trust gate, match quality signals
- ASCII flowchart with trust gate branch

Already drafted and reviewed (81 lines). See `skills/using-chameleon/SKILL.md` in working tree.

### 6. Hook-model dedup removal

Remove the dedup logic from `preflight_and_advise()` that skips injection when the model already called `get_canonical_excerpt`. With tiered PreToolUse at ~50 tokens, the dedup savings are negligible vs complexity cost.

### 7. Archetype summary field

Add a `summary` field to each archetype in `archetypes.json` (1-2 sentences). Used by PreToolUse Tier 1 pointer for key constraints.

**Generation mechanism:** heuristic, not LLM. Two sources:

**Source 1 (archetype schema):** path pattern, top-level AST node kinds, content signal, default export kind. Produces a mechanical summary: `"app/controllers/**. ClassNode, default export. snake_case."` Thin but always available.

**Source 2 (canonical witness):** at bootstrap/refresh time, read the canonical witness file and extract: superclass (if any), key method patterns, decorator/annotation usage. Produces a richer summary: `"Rails controller. Inherits ApplicationController, before_action guards."` Requires one file read per archetype - acceptable during bootstrap/refresh (not on the hot path).

**Composition:** combine both sources. Template: `"{path_description}. {witness_details}. {content_signals}."` If witness is unavailable (file deleted, unreadable), fall back to Source 1 only.

Example output: `"Rails controller (app/controllers/**). Inherits ApplicationController, before_action guards. snake_case methods."`

No LLM cost. Deterministic. Runs in the existing bootstrap/refresh pipeline.

**Missing summary fallback:** when `summary` is absent (profiles bootstrapped before v0.7.0), PreToolUse falls back to Tier 2 (full canonical) for that archetype until the user runs `/chameleon-refresh`.

## Cross-platform behavior

`updatedToolOutput` is Claude Code-specific (v2.1.121+). Platform behavior:

| Platform | updatedToolOutput | Enforcement mechanism |
|----------|-------------------|----------------------|
| Claude Code | Yes | updatedToolOutput for violations (primary) |
| Cursor | Likely yes (adapter) | Same as Claude Code, verify during dogfood |
| Codex CLI | No (no native hooks) | Falls back to additionalContext for violations. Enforcement via AGENTS.md instructions. |

The `_emit_posttool_context` helper already handles platform dispatch (see `hook_helper.py`). When `updatedToolOutput` is unavailable, the violation message goes through `additionalContext` - same as v0.6.x behavior. No code branching needed; just document that Codex enforcement is advisory-only.

## Existing escape hatches

All enforcement features are always ON by default. No new config toggles.

Pre-existing env vars that affect enforcement:
- `CHAMELEON_VERIFY=0` - disables PostToolUse verification AND escalation state tracking. PreToolUse tiering still works but `archetypes_with_violations` stays empty.
- `CHAMELEON_DISABLE=1` - master kill switch. Both PreToolUse and PostToolUse suppressed entirely. No state tracking.

New env var (v0.7.0):
- `CHAMELEON_ENFORCEMENT_MODE=updatedToolOutput|additionalContext` (default: `updatedToolOutput`) - controls whether PostToolUse violations use `updatedToolOutput` (high salience, replaces tool result) or `additionalContext` (v0.6.x behavior, system reminder). Runtime toggle - no code release needed to switch. If `updatedToolOutput` causes model issues in production, set to `additionalContext` immediately.
- `/chameleon-disable` (slash command) - session-scoped disable. Same effect as `CHAMELEON_DISABLE=1` for the rest of the session.
- `/chameleon-pause-15m` - temporary suppress. Auto-resumes.

## State management

**File:** `{plugin_data}/{repo_id}/.enforcement.{session_id}.json`

```json
{
  "archetypes_seen": ["next-server-component", "api-handler"],
  "archetypes_with_violations": ["api-handler"],
  "files": {
    "/abs/path/to/file.ts": {
      "level": 0,
      "violation_count": 2,   // observability: total violations, not used in logic
      "correction_count": 1,
      "last_violation_at": 1748200000,
      "last_verified_at": 1748200005,
      "last_clean_at": null,
      "consecutive_l2": 0
    }
  }
}
```

**State file cap:** max 200 file entries. If exceeded, evict the oldest (by `last_verified_at`) entries. This bounds JSON parse time and file size in long sessions.

**Locking:** reuse `locks.py::acquire_advisory_lock()` with lock file `.enforcement.{session_id}.lock`. Read-modify-write under flock.

**Imports:** `enforcement.py` needs `plugin_data_dir`. Currently lives as `_plugin_data_dir()` in `hook_helper.py` and `plugin_data_dir()` in `chameleon_mcp.profile.trust`. Promote to `chameleon_mcp.plugin_paths` as part of this work (single canonical location).

**Cleanup:** SessionStart hook (`session_start()`) iterates `{plugin_data}/{repo_id}/.enforcement.*.json`, deletes files with mtime > 24h. Glob pattern: `.enforcement.*.json` + `.enforcement.*.lock`. Best-effort (fail-open on any error).

## Fail-open contracts

| Component | Failure | Behavior |
|-----------|---------|----------|
| PreToolUse safety gate | Can't resolve path | **Fail-closed**: deny edit |
| PreToolUse archetype resolve | Timeout/error | **Fail-open**: no injection, edit proceeds |
| PreToolUse state read | Can't read enforcement JSON | **Fail-open**: Tier 2 (full canonical, safe default) |
| PostToolUse lint | lint_file fails | **Fail-open**: emit nothing, edit stands |
| PostToolUse `updatedToolOutput` | `tool_response` has error or success=False | **Fall back** to `additionalContext` (don't mask real errors). Uses the proven `tool_response` dict check (v0.6.x logic), not string-matching on `tool_output`. |
| PostToolUse state write | Can't write enforcement JSON | **Fail-open**: violations still emitted this edit, state lost for next |
| Escalation state | Corrupt/unreadable | **Fail-open**: treat file as no-state (L0 on next violation) |

## Token budget (projected)

| Hook | Case | Tokens |
|------|------|--------|
| PreToolUse | Tier 1 pointer (steady state) | ~50 |
| PreToolUse | Tier 2 canonical (cold/violation) | ~300-600 |
| PostToolUse | Clean pass | 0 |
| PostToolUse | Violation (L0) | ~100-200 |
| PostToolUse | Violation (L1) | ~120-220 |
| PostToolUse | Violation (L2) | ~150-250 |
| PostToolUse | Clean-after-violation | ~20 |
| PostToolUse | Corrections exhausted | ~40 |

Steady state per edit: **~50 tokens** (down from ~500-1500 in v0.6.3).
Tier 2 revised upward from original estimate (~200-400) to ~300-600 to account for `// REQUIRED:` annotations.

## QA risks and mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| `updatedToolOutput` changes what model sees | High | Prepend original `tool_output` verbatim so model keeps its progress signal. Fall back to `additionalContext` on error. Test against 10-edit refactoring session. **Hotfix plan:** revert to `additionalContext` for all violation output (v0.6.x behavior). Single conditional in emit path. |
| Model behavior with updatedToolOutput is unvalidated | High | Core thesis (tool result > system reminder for salience) is untested. Escalation tones, "fix silently" compliance, Tier 1 pointer effectiveness all need dogfood validation. Run journey harness Act 19 (enforcement) before release. If model ignores updatedToolOutput violations at same rate as additionalContext, the thesis is wrong - revert to additionalContext and focus on PostToolUse formatting instead. |
| Correction loop (fix A introduces B) | High | Loop guard at 10 corrections with 60s reset window. |
| Model narrates corrections despite instruction | Medium | Use "Fix these without mentioning the corrections to the user" (explicit). Monitor in dogfood. Cosmetic if it happens. |
| Model interprets "fix silently" as "ignore" | Medium | Phrasing changed to explicit instruction. Test both interpretations in dogfood. |
| Wrong archetype -> wrong corrections | Medium | Surface to user after 3 consecutive L2. If a file has never had a clean pass this session AND hits L2, the "3 consecutive L2" surface-to-user fires immediately (fast-path for misclassification). |
| Parallel tool calls racing on state file | Low | flock via `acquire_advisory_lock()`. Per-file state keys avoid most races. |
| State file growth in long sessions | Low | 200-file cap with LRU eviction. |

## Implementation order

1. `enforcement.py` - state machine, state I/O, correction counter, eviction. Imports `plugin_paths.plugin_data_dir`.
2. PostToolUse rewrite in `hook_helper.py` - `updatedToolOutput`, escalation, correction cap, level-aware cooldowns, explicit execution order.
3. PreToolUse rewrite in `hook_helper.py` - tiered injection, session state tracking, dedup removal.
4. Archetype summary field - heuristic generator in bootstrap/refresh pipeline, schema update for `archetypes.json`.
5. Skill rewrite - apply the drafted `SKILL.md` (already reviewed, 81 lines).
6. SessionStart cleanup - add enforcement state file cleanup to `session_start()`.
7. Unit tests - enforcement state machine transitions, correction counter reset, cooldown parameterization, posttool with updatedToolOutput, pretool tier selection, summary generation, state eviction, cleanup.
8. Architecture doc update - already drafted with [VERIFIED]/[ASPIRATIONAL] split, update [ASPIRATIONAL] section to match this spec.
9. Version bump 0.6.3 -> 0.7.0 + CHANGELOG.
