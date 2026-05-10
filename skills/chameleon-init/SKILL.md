---
name: chameleon-init
description: Use when the user explicitly invokes /chameleon-init to bootstrap a chameleon profile for the current TypeScript repository
---

# /chameleon-init

> **Phase 1B placeholder.** Full skill body to be authored in Phase 3 (Skills with eval) following the RED-GREEN-REFACTOR cycle from `superpowers:writing-skills`.

## Purpose

Bootstrap a chameleon profile for the current repo via interactive AST scan + ≤3-prompt interview. Generates `.chameleon/profile.json` and friends, written atomically via the commit-marker pattern.

## Implementation status

- [ ] Phase 2: bootstrap engine (AST scan, clustering, canonical injection scanner, secret scanner)
- [ ] Phase 3: skill body authored via RED-GREEN-REFACTOR
- [ ] Phase 3: pressure scenarios (5,000-file repo, half-migrated codebase, Ctrl-C mid-flow, AST parse failure, workspace detection)

## Design specification

See `ARCHITECTURE.md`:
- "Bootstrap interview flow" — the ≤3-prompt user-facing flow
- "Atomicity & Crash Safety" — atomic transaction protocol
- "Cluster signature function" — the `f: file → ClusterKey` definition

## Slash command surface

- `/chameleon-init` — bootstrap with default settings (committed save destination)
- `/cham-init` — short alias
