# ADR-0003: Ship TypeScript first, Ruby second

> **Status:** Accepted (Ruby support shipped in v1.5)
> **Date:** 2026-05-10
> **Deciders:** Cris Nahine

## Context

The original v1 design proposed shipping multiple language extractors at once: TypeScript (TS Compiler API), Ruby (Prism), Python (libcst), and others. That dependency burden contradicted the architecture's stated zero-dependency philosophy and would have spread test pressure thin across many languages before any one was solid.

## Decision

Ship TypeScript only in v1.0. Add Ruby on Rails support in v1.5 once the engine, bootstrap pipeline, and skill loop are proven on a single language.

## Consequences

### Positive

- Smaller surface area for v1.0; faster path to a first shippable release.
- Lets the cluster signature, canonical selector, and hook stack mature on one language before extending.
- Adding a language to a proven engine is integration work, not novel engineering.

### Negative / trade-offs

- Ruby on Rails users have to wait for v1.5.
- Two-stage release adds a milestone; v1.5 is gated on v1.0 validation.

### Risks accepted

- A second-language port may surface design assumptions the engine made implicitly about TypeScript. Mitigated by keeping the cluster signature schema language-agnostic from day one.

## Alternatives considered

- **Ship multiple languages in v1.0.** Rejected: too many extractors at once would split test pressure and slow time-to-first-release.
- **Ship Ruby first.** Rejected: the TypeScript ecosystem has more pre-existing tooling (Compiler API, ts-morph, ESLint) to lean on while validating the engine.

## References

- `ARCHITECTURE.md#extractor-design`
- v1.5 added Ruby via `scripts/prism_dump.rb`, mirroring the TypeScript subprocess pattern.
