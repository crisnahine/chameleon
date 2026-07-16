---
name: chameleon-pause-15m
description: Use when the user explicitly invokes /chameleon-pause-15m to temporarily suppress chameleon's advisory injections for 15 minutes
---

# /chameleon-pause-15m

Pause chameleon's per-edit layer for 15 minutes by default. Auto-resumes after the timer expires. Use when latency is unwelcome for a short focused window (e.g. live coding, code review walkthrough, demo).

Like `/chameleon-disable`, a pause is a FULL per-edit opt-out for its window: while paused the PreToolUse hook early-returns, so no `<chameleon-context>` is injected AND the PreToolUse enforcement denies (`secret-detected-in-content`, `eval-call`, `import-preference-violation`) do NOT fire. If the goal is to keep advisory guidance but stop only the blocking, use `CHAMELEON_ENFORCE=0` instead — pause turns everything off, `CHAMELEON_ENFORCE=0` keeps advisory ON and blocking OFF.

The underlying `pause_session` action accepts any integer in `[1, 240]` minutes; the `-15m` slash command alias is the default convenience. If a different duration is needed, call `chameleon-mcp::chameleon_lifecycle(action="pause_session", params={"repo": <repo>, "minutes": N})` directly.

## When to use

- User is presenting / demoing and wants minimal hook latency
- User is doing rapid scratch experimentation in a TS repo
- User wants to compare AI output with vs without chameleon active over a short window
- Frustration detected → callout-detector hook surfaces this as a less-permanent option than `/chameleon-disable`

## When NOT to use

- The frustration is about pattern advice quality → `/chameleon-teach` to capture the missed pattern
- The frustration is about session-long latency → `/chameleon-disable` (session-scope) or run `/chameleon-doctor`

## Prerequisites

`pause_session` requires a trust grant. If the repo has no `.trust` record, the tool returns `status: failed` with a message to run `/chameleon-trust` first.

## The flow

1. Call `chameleon-mcp::chameleon_lifecycle(action="pause_session", params={"repo": <repo_root>, "minutes": 15})`.
2. The tool writes `${PLUGIN_DATA}/<repo_id>/.pause_until` with the ISO 8601 expiry on line 1 and an HMAC `sig=` line under it.
3. PreToolUse hook checks `.pause_until`:
   - If file missing or expired → inject normally (auto-cleans expired markers)
   - If timestamp in future AND the signature verifies → skip injection (a hand-planted or forged marker is ignored, same defense as the session-disable marker)
4. Confirm to user: "chameleon paused for 15 minutes (until <expires_at returned by the tool>). Will auto-resume."

## Opt-out hierarchy

See `chameleon-disable` skill for the full hierarchy. `pause-15m` is the most-temporary option.

## Implementation status

Hook integration is wired: preflight-and-advise calls `is_chameleon_suppressed` before injecting, which honors `.pause_until` (auto-expires), `.session_disabled.<sha256(session_id)[:16]>`, `CHAMELEON_DISABLE=1`, and `.chameleon/.skip`.

## Future variants

If `15m` is the wrong duration for some users, future versions may add `/chameleon-pause-1h` and `/chameleon-pause-until-restart`. The `pause_session` action already accepts a `minutes` arg up to 240 (4 hours).
