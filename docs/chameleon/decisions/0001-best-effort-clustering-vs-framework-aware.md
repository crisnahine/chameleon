# ADR-0001: Best-effort clustering, not framework-aware detection

> **Status:** Accepted
> **Date:** 2026-05-10
> **Deciders:** Cris Nahine
> **Round/Context:** Round 2 adversarial pressure-test (pattern adversary) → v3 strategic scope shift

## Context

v1 of the architecture proposed dimension-based pattern detection that would "support" specific frameworks (Next.js App Router, NestJS decorators, Pydantic v1 vs v2, tRPC builders, etc.). Round 2's pattern adversary demonstrated the proposed `content_signal` schema (single `directive` + `absent_directives` fields) is a "two-cell toy" that cannot express:

- Imports (e.g., distinguishing `react-dom/server` from `react/jsx-runtime`)
- Decorators (NestJS `@Injectable()`, TypeORM)
- Type-level discriminants (branded types, conditional types)
- Class-body shapes (Pydantic v1 inner Config class)
- Multi-canonical archetypes
- Auto-generated API surfaces (tRPC builder chains)

Three options were considered:
1. Constrain v1 to a small framework allowlist
2. Expand `content_signal` into a full AST matcher DSL
3. Drop framework-aware framing entirely; do best-effort clustering on whatever AST can express, route the rest through human curation

## Decision

**chameleon does best-effort pattern clustering on whatever AST can parse. Where AST + statistical analysis produces clean archetypes, the engine handles them automatically. Where it cannot (DSL-heavy code, metaprogramming, type-level patterns), the engine falls back to interactive interview + iterative `/chameleon-teach` to capture team idioms in `idioms.md`. No framework is "supported" in a contractual sense; quality scales with codebase organization and team curation.**

## Consequences

### Positive consequences

- Architecture's promise is honest: "we cluster what we can, ask about what we can't"
- Avoids the slippery slope of growing `content_signal` into a JSONPath/CEL-style matcher language (Round 2 reviewer estimated this would triple extractor complexity)
- Two-tier dimension model becomes principled: 40 dimensions Tier 1 (auto-derivable), 29 Tier 2 (hand-curated)
- `/chameleon-teach` becomes load-bearing in a clear way (not an afterthought escape hatch)
- Engine is simpler and more focused; bigger surface area moves to user interview/iteration

### Negative consequences / trade-offs

- Less "magic" feel for users — the engine doesn't know about NestJS or tRPC by name
- More user effort required for teams using framework-heavy stacks (more `/chameleon-teach` invocations)
- Unable to claim feature parity with framework-specific tools

### Risks accepted

- New users may expect "framework support" and be disappointed
  - Mitigation: README explicit about best-effort framing
- Teams with very framework-heavy stacks may find chameleon less useful than CLAUDE.md
  - Mitigation: README "Why not just write a CLAUDE.md?" section explicitly addresses this

## Alternatives considered

### Alternative A: Framework allowlist (Next.js + classic React + plain Node only)

Rejected because it's simultaneously too narrow (excludes most modern TS) and not honest (still claims "framework support").

### Alternative B: Full AST matcher DSL

Rejected because it would triple extractor complexity, require shipping a language without a lexer, and gold-plate v1 with infrastructure that may not be needed.

## References

- Architecture section: `ARCHITECTURE.md#what-chameleon-is-and-is-not-computing`
- Round 2 report: `docs/chameleon/ROUND-2-REVIEWS.md` (Pattern Adversary: "content_signal is a two-cell toy")
- v3 changelog: "Strategic scope shift — dropped framework-aware framing"
