---
name: using-chameleon
description: Use when starting any conversation in a TypeScript repo with a chameleon profile present, before any Edit, Write, or NotebookEdit operation
---

# Using Chameleon

> **Phase 1A placeholder.** The full skill body will be authored in Phase 3 (Skills with eval) following the RED-GREEN-REFACTOR cycle from `superpowers:writing-skills`.

## Status

This skill is a stub. The architecture specifies its design; the implementation requires:

1. **RED phase** — run pressure scenarios WITHOUT the skill against a TypeScript repo with a chameleon profile. Capture verbatim rationalizations agents make to skip the MCP call (e.g., "this is just a small fix", "I already know this codebase", "I already saw the canonical this session").
2. **GREEN phase** — write the skill body addressing those specific rationalizations. Includes:
   - `<chameleon-context>` block (NEUTRAL framing, no `<EXTREMELY_IMPORTANT>`)
   - `<SUBAGENT-STOP>` block (subagents skip)
   - The Rule: invoke `chameleon-mcp::detect_repo` + `get_pattern_context` BEFORE editing in profiled repos
   - Process flowchart (graphviz `dot`)
   - Red Flags table (rationalizations to defeat)
   - Available slash commands list
   - Profile state interpretation (trusted vs untrusted)
   - Coordination with superpowers (priority order)
3. **REFACTOR phase** — close any new rationalizations surfaced during testing.

## Design specification

See `ARCHITECTURE.md#skill-design` for the foundation skill specification.

## Skill test plan

See `ARCHITECTURE.md#skill-test-plan` for the RED-GREEN-REFACTOR plan, including:
- 6 baseline pressure scenarios
- Adversarial composition test (with `using-superpowers` active)
- Quarterly model re-baseline cadence

## Tests

Tests for this skill will live at `skills/using-chameleon/tests/`:
- `tests/baseline.md` — captured rationalizations (CI-enforced; PRs cannot merge with missing baseline)
- `tests/scenarios/` — pressure scenarios (one per .md file)
