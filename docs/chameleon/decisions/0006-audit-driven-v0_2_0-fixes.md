# ADR-0006: Audit-driven v0.2.0 fix release

> **Status:** Accepted
> **Date:** 2026-05-11
> **Deciders:** Cris Nahine

## Context

An external audit of v0.1.1 ([chameleon-test-report.md][audit] in
the project repo) surfaced 10 distinct defects across the engine.
The defects spanned security (a high-severity gap in
`profile.summary.md`), correctness (a critical idiom-wipe on
refresh), schema design (the v4 bucketing collapse), input
validation (`list_profiles`, `trust_profile`), and documentation
(undocumented `force` parameter, stub flag absent on `lint_file`).

The choices were:

1. Patch a subset and defer the rest. Rejected — several were
   user-data-loss bugs.
2. Treat the audit as a single fix-release. Accepted.
3. Roll the fixes into a v0.3 feature release. Rejected — feature
   work would obscure the regression coverage, and the schema
   bump alone justified a labelled release.

CHANGELOG.md v0.2.0 is the canonical record of every fix. This ADR
documents the *release pattern* — how the audit findings were
verified, prioritized, and pinned with regression coverage — not
the individual fixes (the changelog already does that).

## Decision

Ship v0.2.0 as an audit-fix release with these properties:

1. **Every audit finding is verified independently before being
   fixed.** Two agents reproduce each reported bug against v0.1.1
   source before any fix lands. Findings that did not reproduce
   were re-classified as documentation issues or rejected with
   reasoning.
2. **The fix targets the upstream root cause, not the symptom.**
   See ADR-0005: the audit blamed `tools.py:127` for the
   app/spec collapse, but the real cause was the bucketing
   function; we fixed the bucket.
3. **Every fix is pinned with a regression test in
   `tests/v0_2_regression_test.py`** that fails on v0.1.1 source
   and passes on v0.2.0. 25 assertions across 10 audit-finding
   sections.
4. **Schema bumps are atomic with the fix that requires them.**
   The bucketing fix and the `PROFILE_SCHEMA_VERSION 4 → 5` bump
   ship together; downgrades are not supported.
5. **The CHANGELOG is the canonical fix register.** This ADR
   documents the *process*. The individual fix list, severity
   labels, and breaking-change call-outs are in CHANGELOG.md.

## Consequences

### Positive consequences

- A reproducible "audit → verify → fix → pin" loop. Future
  audits can run the same loop without rebuilding it each time.
- Regression coverage protects against rot. The
  `v0_2_regression_test.py` file is in
  `tests/run_all_orders.py` so it runs on every test cycle and
  in every randomized order.
- High-severity user-data-loss bugs (refresh wiping idioms) are
  closed before more user state accumulates.
- The trust gate is closer to its design intent: idiom bodies
  surface for review at trust time, not as a placeholder.

### Negative consequences / trade-offs

- Breaking schema change requires user re-bootstrap and re-trust.
  Mitigated by ENGINE_MIN_VERSION bump and explicit CHANGELOG
  documentation, but it is real friction.
- A regression-pinned audit-fix release sets a precedent: every
  future audit fix should follow the same pattern. That adds
  ceremony to single-line fixes; we accept that as the cost of
  not silently regressing the audit's findings.
- The audit was scoped to v0.1.1; findings in deeper layers
  (clustering quality at scale, calibration, multi-hour session
  stability) are out of v0.2.0 scope and remain Phase 6 / Phase 7
  work.

### Risks accepted

- The "two independent agents verify each finding" gate is
  process, not infrastructure. A future audit run by a single
  reviewer could skip the independent-verification step and
  ship a fix for a bug that turns out to be a misread.
  Mitigation: the regression-test gate would catch this — if the
  test passes on v0.1.1, the "bug" wasn't a bug. The test
  remains as a tripwire even if the verification step is
  shortcircuited.

## Alternatives considered

### A. Quick-patch release (only the critical idiom-wipe fix)

Rejected. The audit identified a high-severity security gap
(`profile.summary.md` placeholder hiding idiom bodies from the
trust review) that ought to ship in the same release as the
critical correctness fix. Stretching the critical fix into a
.1.2 and the security fix into a .1.3 would leave the security
gap open for one release cycle longer than necessary.

### B. Roll into v0.3 alongside Phase 4 lint engine

Rejected. Mixing audit fixes with feature work makes the
regression coverage harder to read and harder to bisect when
something breaks. A labelled audit-fix release is its own thing.

### C. Skip the regression-test pinning

Rejected. The audit findings span enough of the codebase that
without pinning, a future refactor could quietly un-fix any of
them. The 25 assertions in `tests/v0_2_regression_test.py`
are cheap to maintain and pay for themselves the first time
they catch a regression.

## References

- `CHANGELOG.md` — v0.2.0 entry (canonical fix register)
- `tests/v0_2_regression_test.py` — 25 assertions, one per audit finding
- ADR-0005 (`0005-schema-v5-path-pattern-bucketing.md`) — schema bump shipped in v0.2.0
- `mcp/chameleon_mcp/bootstrap/orchestrator.py` — `_extract_active_idioms`, `_build_summary_md` (trust-gate idiom surfacing)
- `mcp/chameleon_mcp/tools.py::teach_profile` — feedback validation cluster

[audit]: https://github.com/crisnahine/chameleon/blob/main/docs/chameleon-test-report.md
