---
name: chameleon-refresh
description: Use when the user explicitly invokes /chameleon-refresh to re-analyze the current repo and update the chameleon profile
---

# /chameleon-refresh

> **Phase 1B placeholder.** Full skill body to be authored in Phase 3.

## Purpose

Re-analyze the current repo, detect drift, update `.chameleon/profile.json`. Uses the recompute-all-from-cached-signatures incremental algorithm: cached signatures reused for unchanged files, recomputed only for changed files, full re-cluster from current sig set.

## Implementation status

- [ ] Phase 2: incremental algorithm + drift.db cache invalidation triggers
- [ ] Phase 3: skill body via RED-GREEN-REFACTOR

## Design specification

See `ARCHITECTURE.md` "Cluster signature function" → "Incremental algorithm" subsection.

## Slash command surface

- `/chameleon-refresh` — full re-analysis with consent prompt for drift summary
- `/cham-refresh` — short alias
