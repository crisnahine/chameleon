---
name: chameleon-trust
description: Use when the user explicitly invokes /chameleon-trust to approve a committed chameleon profile for use in their current Claude Code session
---

# /chameleon-trust

> **Phase 1B placeholder.** Full skill body to be authored in Phase 3.

## Purpose

Per-user, per-repo approval of a committed `.chameleon/profile.json`. Trust is required before chameleon's advisory injections fire for a given user (the trust prompt is non-blocking — Claude can still respond to the user without it).

Trust prompt requires the user to type the repo name (or `yes-trust-<repo_id_short>`) to defeat normalization-deviance ("yes to everything"). New canonicals or new active idioms added after trust grant trigger re-prompt; deprecation status changes and recency_weight recalculations are silent updates.

## Implementation status

- [ ] Phase 2: `.trust` file format with profile_sha256 hash for material-change detection
- [ ] Phase 3: skill body via RED-GREEN-REFACTOR
- [ ] Phase 4: cooldown + frequency limits (Round 5 AppSec recommendation #8)

## Design specification

See `ARCHITECTURE.md`:
- "Profile schema" — `.trust` file format (3 fields: granted_at, granted_by_user, profile_sha256)
- "SQLite schemas" — material-change predicate (re-prompt vs silent update)
- "Security mitigations" #11 — trust model with cooldown

## Slash command surface

- `/chameleon-trust` — interactive trust grant (requires typing repo name)
- `/cham-trust` — short alias
