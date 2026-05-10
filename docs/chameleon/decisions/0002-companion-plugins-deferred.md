# ADR-0002: Companion plugin pattern deferred to v2.0+

> **Status:** Accepted
> **Date:** 2026-05-10
> **Deciders:** Cris Nahine

## Context

An early draft of the architecture proposed companion plugins as the distribution model for hand-curated profiles. The pattern brought significant complexity:

- Pack manifest schema (publisher, engine_min_version, engine_max_version, signature)
- Pack signing infrastructure (Sigstore / cosign or ed25519)
- `--allow-unsigned` flag (with the usual social-engineering risks)
- Pack ID namespace (`<publisher>/<pack>`)
- Pack version compatibility
- Max companion packs per session guard
- Pack abandonment policy
- Discovery mechanism
- Triple/quadruple-priming risk under multi-pack composition

The profile artifacts (`profile.json`, `archetypes.json`, etc.) are committable to each repo. Git is the distribution mechanism. New developers cloning the repo get the profile automatically. So a separate plugin distribution channel is not needed.

## Decision

No companion plugin pattern in v1. The engine is the only distributed artifact. Profile sharing happens via committed `.chameleon/profile.json` per repo — git is the package manager.

Companion plugins remain a v2.0+ possibility if user demand emerges. The engine architecture supports adding the pattern as a non-breaking change.

## Consequences

### Positive

- Eliminates significant attack surface (no signing infra, no `--allow-unsigned` flag, no version compat between engine and packs).
- Simpler mental model: one engine, one workflow ("run /chameleon-init").
- No separate plugin to maintain alongside the chameleon engine.
- Trust model is simpler (per-user trust per-repo, no pack-level trust).

### Negative / trade-offs

- No "drop-in" pre-built profiles.
- Each user must run `/chameleon-init` themselves (modest one-time cost).
- "Pre-built profile pack for Rails 7" or "Next.js 14 baseline" becomes user-driven, not chameleon-supplied.

### Risks accepted

- If demand emerges later, retrofitting the pattern requires engine changes.
  - Mitigation: the architecture pre-specifies the addition path; engine changes will be additive, not breaking.

## Alternatives considered

- **Ship companion plugin pattern in v1.0.** Rejected: complexity-to-value ratio is poor with no demonstrated demand.
- **Bundle profiles into the chameleon engine.** Rejected: violates the "engine is generic" principle.

## References

- `ARCHITECTURE.md#profile-distribution-engine-is-the-only-artifact`
- `ARCHITECTURE.md#future-possibility-companion-plugins-v20-out-of-scope-for-v1`
