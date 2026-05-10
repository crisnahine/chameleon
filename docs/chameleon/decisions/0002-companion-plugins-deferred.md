# ADR-0002: Companion plugin pattern deferred to v2.0+

> **Status:** Accepted
> **Date:** 2026-05-10
> **Deciders:** Cris Nahine
> **Round/Context:** v3-final scope cut (user direction)

## Context

v3-draft of the architecture proposed companion plugins as the distribution model for hand-curated profiles. Specifically, EF profiles would ship as a separate plugin `chameleon-ef-pack`. The pattern brought significant complexity:

- Pack manifest schema (publisher, engine_min_version, engine_max_version, signature)
- Pack signing infrastructure (Sigstore/cosign or ed25519)
- `--allow-unsigned` flag (with all the social-engineering risks)
- Pack ID namespace (`<publisher>/<pack>`)
- Pack version compatibility (Round 2 BLOCKING #3)
- Max companion packs per session guard (10)
- Pack abandonment policy
- Discovery mechanism for 50+ packs
- Triple/quadruple-priming risk (Round 2 plugin compat reviewer)

User raised the question: "Why do we need a separate plugin for EF? Can't we just run `/chameleon-init` on the EF repos and commit the profile?"

The answer is yes. The profile artifacts (`profile.json`, `archetypes.json`, etc.) are committable to each repo. Git is the distribution mechanism. New devs cloning the repo get the profile automatically.

## Decision

**No companion plugin pattern in v1. Engine is the only distributed artifact. Profile sharing happens via committed `.chameleon/profile.json` per repo (git is the package manager). EF api and EF client are reframed as dogfood test cases, not special plugins.**

Companion plugins remain a v2.0+ possibility if community demand emerges post-v1 release. The engine architecture supports adding the pattern as a non-breaking change.

## Consequences

### Positive consequences

- Eliminates significant attack surface (no signing infra, no `--allow-unsigned` flag, no version compat between engine and packs)
- Simpler mental model: one engine, one workflow ("run /chameleon-init")
- No separate plugin to maintain alongside chameleon engine
- Phase 5 (EF dogfood) drops from 50h to 30h
- Trust model gets simpler (per-user trust per-repo, no pack-level trust)
- 5 fewer security mitigations needed (Round 4 Security mitigations went from 16 → 11 items)

### Negative consequences / trade-offs

- No "drop-in" pre-built profiles for community
- Each team must run `/chameleon-init` themselves (modest one-time cost: $0.50-$2)
- "Pre-built profile pack for Rails 7" or "Next.js 14 baseline" becomes user-driven, not chameleon-supplied

### Risks accepted

- If community demand emerges later, retrofitting the pattern requires engine changes
  - Mitigation: architecture section "Future possibility: companion plugins (v2.0+)" pre-specifies the addition path; engine changes will be additive, not breaking

## Alternatives considered

### Alternative A: Ship companion plugin pattern in v1.0

Rejected because the complexity-to-value ratio is poor. v1 has no demonstrated demand for shared community profiles. Speculative infrastructure for hypothetical adopters.

### Alternative B: Bundle EF profiles into chameleon engine

Rejected because it violates "engine is generic" principle. Round 1 Jesse-perspective reviewer flagged this as project-specific bloat in core.

## References

- Architecture section: `ARCHITECTURE.md#profile-distribution-engine-is-the-only-artifact`
- Round 4 changelog: dropped companion plugin pattern from v1 entirely
- Architecture section: `ARCHITECTURE.md#future-possibility-companion-plugins-v20-out-of-scope-for-v1`
