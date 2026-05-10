---
name: chameleon-teach
description: Use when the user explicitly invokes /chameleon-teach to capture a team idiom, banned import, mandatory wrapper, or other team-specific pattern that AST analysis cannot infer
---

# /chameleon-teach

> **Phase 1B placeholder.** Full skill body to be authored in Phase 3.

## Purpose

User-driven correction of profile content. Captures idioms, banned imports, mandatory wrappers, custom HTTP clients, and other team-specific patterns into `idioms.md`. Updates canonical references in `canonicals.json` when the user identifies a better witness file. Marks deprecated idioms with migration paths.

This is the load-bearing tool for Tier 2 dimensions (hand-curated). AST analysis fundamentally cannot infer prohibitions ("never use X"), mandatory wrappers ("all auth goes through Y"), or domain vocabulary ("we say Listing not Property").

## Implementation status

- [ ] Phase 2: idioms.md schema + deprecation tracking
- [ ] Phase 3: skill body via RED-GREEN-REFACTOR
- [ ] Phase 3: pressure scenario — user describes a missed pattern; agent must capture it correctly without misclassifying

## Design specification

See `ARCHITECTURE.md`:
- "Profile schema" — idioms.md schema with deprecation tracking
- "Tracked dimensions catalog" — Tier 2 dimensions (29 total) that this skill captures

## Slash command surface

- `/chameleon-teach` — interactive idiom capture
- `/cham-teach` — short alias

## Naming history

Renamed from `/chameleon-refine` in v4 to eliminate semantic collision with `/chameleon-refresh`. Per Round 5 Dev Tools Pioneer recommendation #3: refresh = automated, teach = manual user correction.
