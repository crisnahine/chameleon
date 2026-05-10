---
name: chameleon-status
description: Use when the user explicitly invokes /chameleon-status to view profile state, drift, value attribution, and plugin health
---

# /chameleon-status

> **Phase 1B placeholder.** Full skill body to be authored in Phase 3.

## Purpose

Surface to the user:
- Active profile summary (archetype count, last refresh, trust state)
- Drift indicators (days_since_refresh, observed confidence trend)
- Value attribution (edits matched archetype, deviations flagged, corrections via `/chameleon-teach`)
- Plugin health (recent MCP error rate, fail-open rate, hook latency p99)

## Implementation status

- [ ] Phase 2: read drift.db and value_attrib.db
- [ ] Phase 3: skill body via RED-GREEN-REFACTOR
- [ ] Phase 4: `--health` flag (Round 5 SRE recommendation #2)
- [ ] Phase 4: `--diff` flag (profile-poisoning scanner CI gate)

## Design specification

See `ARCHITECTURE.md`:
- "Performance characteristics" — observability metrics
- "Failure mode runbook" — health diagnostic commands
- "SQLite schemas" — `sessions` table in value_attrib.db

## Slash command surface

- `/chameleon-status` — default summary view
- `/chameleon-status --health` — operator diagnostic (Phase 4)
- `/chameleon-status --diff` — semantic diff for PR review (Phase 4)
- `/cham-status` — short alias
