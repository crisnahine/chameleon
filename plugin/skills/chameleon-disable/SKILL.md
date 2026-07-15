---
name: chameleon-disable
description: Use when the user explicitly invokes /chameleon-disable to suppress chameleon's advisory injections for the rest of the current session
---

# /chameleon-disable

Disable chameleon's per-edit layer for the current session. The hook stack still executes (and fails open), but once the session is disabled the PreToolUse hook early-returns before doing any work: no `<chameleon-context>` is injected AND the PreToolUse enforcement denies (`secret-detected-in-content`, `eval-call`, `import-preference-violation`) do NOT fire. Disable is a FULL per-edit opt-out, not an advisory-only mute. If you want to keep advisory guidance but stop only the blocking, use `CHAMELEON_ENFORCE=0` instead (advisory ON, blocking OFF) — that is the opposite trade-off from disable/pause (everything OFF).

## When to use

- User runs `/chameleon-disable` explicitly
- User expresses frustration with chameleon's latency or pattern advice (the `callout-detector` hook surfaces this command on detected frustration)
- User is doing experimental / one-off work where conformance pressure is unwelcome

## Opt-out hierarchy

```
Most-permanent →    .chameleon/.skip (per-repo, all users, committed → team-wide)
                ↓   CHAMELEON_DISABLE=1   (per-user globally; in shell rc)
                ↓   /chameleon-disable    (this session only)
                ↓   /chameleon-pause-15m  (next 15 minutes)
Most-temporary
```

Use the most-temporary option that solves the immediate need. Revert by:
- `/chameleon-disable` → starts new Claude Code session
- `/chameleon-pause-15m` → expires automatically
- `CHAMELEON_DISABLE=1` → unset the env var
- `.chameleon/.skip` → remove the file from the repo

## Silencing ONE surface durably (keep the rest of chameleon)

The full opt-outs above are usually the wrong tool when the complaint is a
single recurring surface. The turn-end review (correctness/duplication/idiom
findings) is advisory-only and delivered next turn, so a session touching no
idiom-governed files or triggering no real violation is already silent — but
a team that wants one lens off entirely (not just quiet on a clean turn) has
these `.chameleon/config.json` keys (per-repo, committed → team-wide, survive
new chats):

- `"enforcement": {"idiom_review": false}` — turn off the idiom lens of the
  async turn-end review job. Per-edit guidance, denies, and every other
  turn-end check (correctness, duplication, deterministic advisories) stay
  live. This is the answer to "stop idiom findings, but keep chameleon".
- `"enforcement": {"stop_backstop": false}` — turn off the ENTIRE Stop
  turn-end pipeline (relint block, the whole review job — correctness/
  duplication/idiom lenses — and every deterministic turn-end advisory).
  Per-edit hooks stay live.
- `"enforcement": {"stop_block_cap": 0}` — never let Stop BLOCK; the review
  job and its advisories still run and still deliver findings.

When the user asks to disable chameleon because of turn-end idiom findings
specifically, offer `idiom_review: false` first — it solves the recurring
annoyance without giving up the per-edit layer. Editing `.chameleon/config.json`
is a repo file change: make the edit, tell the user it applies from the next
turn, and let them commit it when they want it team-wide.

## Prerequisites

`disable_session` requires a trust grant. If the repo has no `.trust` record, the tool returns `status: failed` with a message to run `/chameleon-trust` first.

## The flow

1. Confirm chameleon is currently active in this session.
2. Call `chameleon-mcp::chameleon_lifecycle(action="disable_session", params={"repo": <repo_root>, "session_id": <current session_id>})`.
   - If the tool returns `session_unknown_to_chameleon: true`, it means this session has never invoked another chameleon tool. Retry with `"force": true` in `params` if the user explicitly asked for disable.
3. The PreToolUse hook checks for the resulting `.session_disabled.<sha256(session_id)[:16]>` marker before injecting; if present, skips.
4. Confirm to user: "chameleon disabled for this session. SessionStart primer will re-enable on next session unless you set CHAMELEON_DISABLE=1 globally or `.chameleon/.skip` in this repo."

## Don't suggest disable for the wrong problem

- Pattern advice is wrong → use `/chameleon-teach` instead
- Latency is too high → run `/chameleon-doctor` to check health
- One archetype's canonical is bad → edit `.chameleon/canonicals.json` directly OR use `/chameleon-refresh`
- Profile drift is causing churn → `/chameleon-refresh`
- Turn-end idiom findings feel noisy or wrong → `"enforcement": {"idiom_review": false}` in `.chameleon/config.json` (see above)

Disable is the escape hatch for situations where chameleon legitimately isn't useful in the moment, not a tool for fixing other problems.
