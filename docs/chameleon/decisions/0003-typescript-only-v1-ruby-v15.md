# ADR-0003: TypeScript only in v1.0; Ruby (Prism) in v1.5

> **Status:** Accepted
> **Date:** 2026-05-10
> **Deciders:** Cris Nahine
> **Round/Context:** Round 1 dependency-discipline finding + EF dogfood verification

## Context

EF's primary stacks are Ruby on Rails (api) and TypeScript (client). The original v1 design proposed shipping multiple language extractors simultaneously: Ruby (Prism), TypeScript (TS Compiler API), Python (libcst). Round 1 Jesse-perspective reviewer flagged "6 bundled parsers ≠ superpowers-style discipline" — the dependency burden contradicts the architecture's stated zero-dependency philosophy.

EF dogfood verification confirmed both stacks are necessary for full Real Problem Evidence: api (Rails, 151 models, 1,300+ services) provides one rich evidence base; client (TS, 50+ pages, 100+ components) provides the other.

Three options:
1. Ship both Ruby + TypeScript in v1.0 (matches original scope)
2. Ship TypeScript only in v1.0; defer Ruby to v1.5 (sequential)
3. Ship Ruby only in v1.0 (since predecessor `claude-measure-twice` already had Prism working)

## Decision

**v1.0 ships TypeScript only. v1.5 adds Ruby (Prism). v2.0+ adds other languages community-driven.**

Sequencing: prove the engine + bootstrap + skills loop on one language first. Adding a language to a proven engine is integration work, not novel engineering. The predecessor `claude-measure-twice` already proved Prism for Ruby works; v1.5 ports that approach.

A validation gate sits between v1.0 and v1.5: 2-4 weeks of EF client dogfood. Ship v1.0 only after pattern conformance success metrics are met (see `ARCHITECTURE.md#success-metrics`). v1.5 work begins only after v1.0 is shipped and validated.

## Consequences

### Positive consequences

- Risk-controlled: the engine's fundamental abstractions (clustering, signature function, profile schema, MCP surface) are validated against one language before being asked to generalize
- Faster time-to-first-value: ~10 weeks for v1.0 (TS-only) vs ~13 weeks for v1.5 (TS+Ruby)
- Honest scope: addresses Round 1 dependency-discipline critique
- Predecessor proof: Prism approach is integration work in v1.5, not invention
- Clear validation gate: success metrics on TS before Ruby work begins

### Negative consequences / trade-offs

- EF api dogfood deferred until v1.5 (~3 months after v1.0 ships)
- Real Problem Evidence transcripts in v1.0 come from EF client only (half the EF stack signal)
- v1.0 cannot claim "supports EF" — only "supports TypeScript"
- Some EF teammates may not benefit until v1.5

### Risks accepted

- v1.5 may slip indefinitely if v1.0 priorities shift
  - Mitigation: explicit Phase 8 entry in phase plan with effort estimate; success metrics for v1.0 designed to motivate v1.5 continuation
- TS-only v1 may be perceived as too narrow by potential adopters
  - Mitigation: README explicit about v1 scope + v1.5 plan

## Alternatives considered

### Alternative A: Ship TS + Ruby together in v1.0

Rejected because:
- Doubles the engine validation surface (any abstraction failure surfaces in 2 languages, harder to isolate)
- Adds ~30-50h to Phase 1-3 (multi-language extractor design + 2 vendor toolchains + 2 sets of test corpora)
- Engine fundamentals (clustering, signature function) untested; multi-language amplifies any issues

### Alternative B: Ship Ruby only in v1.0

Rejected because:
- TypeScript ecosystem (Vite, vitest, Prettier, ESLint) provides cleaner ground-truth tool configs
- TS Compiler API is more widely-known to the broader Claude Code user base
- Predecessor `claude-measure-twice` already established Ruby support; pivoting away from TS feels like regression

## References

- Architecture section: `ARCHITECTURE.md#typescript-first-extractor-vendored-integrity-checked`
- Architecture section: `ARCHITECTURE.md#phase-plan` (Phase 8 — v1.5 Ruby)
- Round 1 Jesse-perspective verdict: "6 bundled parsers ≠ superpowers-style discipline"
- EF dogfood verification (post-Round 4): confirmed both stacks needed for full Real Problem Evidence
