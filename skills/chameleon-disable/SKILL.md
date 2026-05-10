---
name: chameleon-disable
description: Use when the user explicitly invokes /chameleon-disable to suppress chameleon's advisory injections for the rest of the current session
---

# /chameleon-disable

> **Phase 1B placeholder.** Full skill body to be authored in Phase 3.

## Purpose

Disable chameleon's advisory injections for the current session only. Intended as a one-keystroke escape when chameleon's latency or pattern advice is unwelcome (e.g., experimental work, urgent fix).

For per-repo permanent disable: create `.chameleon/.skip` file. For global disable: set `CHAMELEON_DISABLE=1`.

## Implementation status

- [ ] Phase 2: session-scope state file in `${PLUGIN_DATA}/<repo_id>/.session_disabled`
- [ ] Phase 3: skill body via RED-GREEN-REFACTOR
- [ ] Phase 4: callout-detector integration — surface this command on detected frustration

## Design specification

See `ARCHITECTURE.md`:
- "Plugin coexistence" — opt-out hierarchy (.skip file, env var, session-scope, temporary)
- "Failure mode runbook" — escape hatches discoverable in moments of frustration

## Slash command surface

- `/chameleon-disable` — disable for rest of session
- `/cham-disable` — short alias
