---
name: chameleon-pause-15m
description: Use when the user explicitly invokes /chameleon-pause-15m to temporarily suppress chameleon's advisory injections for 15 minutes
---

# /chameleon-pause-15m

> **Phase 1B placeholder.** Full skill body to be authored in Phase 3.

## Purpose

Pause chameleon's advisory injections for exactly 15 minutes. Auto-resumes after the timer expires. Intended for short focused work where the per-edit MCP latency is unwelcome (e.g., live coding session, code review walkthrough).

## Implementation status

- [ ] Phase 2: timestamp file at `${PLUGIN_DATA}/<repo_id>/.pause_until` (ISO 8601 UTC)
- [ ] Phase 3: skill body via RED-GREEN-REFACTOR
- [ ] Phase 4: callout-detector integration — surface this command on detected frustration

## Design specification

See `ARCHITECTURE.md` "Plugin coexistence" — opt-out hierarchy.

## Slash command surface

- `/chameleon-pause-15m` — pause for 15 minutes
- `/cham-pause-15m` — short alias

## Future variants (out of scope for v1)

If `15m` is too short or too long for some users, future v1.5+ may add `/chameleon-pause-1h`, `/chameleon-pause-until-restart`. Defer based on observed user behavior.
