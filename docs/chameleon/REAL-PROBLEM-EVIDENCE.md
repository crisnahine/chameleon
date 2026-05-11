# chameleon — Real Problem Evidence

What real problem does chameleon solve, and what evidence do we have
that the engine works? This document is the honest answer.

ARCHITECTURE.md's Phase 7 exit criteria list this file by name. It
answers the question a skeptical reviewer asks first: **does the
security ceremony — trust gate, sanitization, poisoning scanner,
HMAC-signed exec log, schema versioning, atomic commits — buy us
anything in practice?**

The answer is "yes for the layers we can measure today; not yet
measured for the conformance-rate claim." Sections below say where
each line of evidence comes from and where the gaps are.

---

## The problem

From the README:

> AI-generated code in established codebases routinely violates
> local conventions: wrong file location, off-pattern naming,
> missed team idioms, divergent error handling. Reviewer time
> gets spent on style and shape instead of logic and security.

This is the failure mode chameleon is built to address. It is not
a security tool, not a linter, not a code formatter. It is a
context-augmentation engine: it tells the model what *this team's*
controllers look like, where *this team's* tests live, what
imports *this team's* canonical service uses. The premise: an AI
that knows the local conventions writes code that needs less
shape-correction in review.

ADR-0001 (`docs/chameleon/decisions/0001-best-effort-clustering-vs-framework-aware.md`)
spells out the honest framing: "we cluster what we can, ask about
what we can't." chameleon does not claim framework-specific support.
The engine's value scales with two things: how well the codebase's
organization clusters under the 7-tuple signature function, and how
much team curation flows through `/chameleon-teach`.

---

## Evidence the engine works

Drawn from the v0.2.0 release artifacts and the audit response.

### Sanitization works at the byte level

`mcp/chameleon_mcp/sanitization.py` and its regression coverage in
`tests/comprehensive_test.py` ("Sanitization across all dangerous
tokens", "Sanitization defeats zero-width-injected closing tag")
demonstrate that the tag-boundary sanitizer correctly neutralizes
all 9 catalogued evasion tokens, including the zero-width-sandwich
case where an attacker inserts `​` characters inside
`</chameleon-context>` to bypass naive substring matching.

The sanitization order — strip zero-width unicode first, then ANSI
escapes, then NFC-normalize, *then* replace dangerous tokens — is
load-bearing. The regression test pins the order; reordering breaks
the zero-width defense.

### Trust-gate stale-flip works

`mcp/chameleon_mcp/profile/trust.py::is_material_change` and the
v0.2.0 acceptance pass demonstrate: a profile mutation flips the
trust state to `stale`, the SessionStart primer announces the
state, and downstream advisory injections are gated until re-grant.
This is the defense against silent profile poisoning between trust
grants.

The v0.2.0 audit verified the flip in both directions: a tampered
`profile.json` immediately surfaces as stale; a no-op
`/chameleon-refresh` against unchanged content does not.

### `get_pattern_context` routes correctly after the v5 schema fix

The v0.2.0 audit ([chameleon-test-report.md][audit] in the project
repo) surfaced one failure mode where the path-pattern bucket
collapsed `app/controllers/api/v1/foo.rb` and
`spec/controllers/api/v1/foo_spec.rb` into the same `"api/v1"` key,
so `get_archetype`'s cluster-size tiebreak routinely returned the
spec archetype for `app/` files. After the v5 bucketing fix
(`mcp/chameleon_mcp/signatures.py::path_pattern_bucket_for`),
`tests/v0_2_regression_test.py` confirms three distinct
app/spec pairs each resolve to distinct buckets. The same test
also confirms that shallow paths (`app/models/listing.rb` ≤ 3
segments) still produce a coherent bucket with the top-level
segment preserved.

This is the most concrete piece of "the engine is doing the right
thing on real Ruby on Rails layouts" evidence we have today.

### Refresh preserves idioms

The v0.2.0 audit caught a critical bug in v0.1.1: every
`/chameleon-refresh` wrote an empty `idioms.md` template inside
the atomic transaction, silently destroying every
`/chameleon-teach` capture. The fix
(`mcp/chameleon_mcp/bootstrap/orchestrator.py`) reads the
existing `idioms.md` before opening the transaction and re-emits
its content. `tests/v0_2_regression_test.py` section "Critical:
refresh_repo preserves user idioms" confirms a `/chameleon-teach`
capture survives a subsequent `/chameleon-refresh` cycle.

This matters because idioms are the Tier 2 dimensions — the parts
of "this team's conventions" the engine cannot infer from AST
alone. If they don't survive refresh, the team-curation channel
is broken and the engine reduces to "AST clustering only."

### Atomic profile commit survives interruption

`mcp/chameleon_mcp/bootstrap/transaction.py` plus the orphan-cleanup
path covered in `tests/comprehensive_test.py` section "Atomic
transaction orphan cleanup" demonstrate that a partial
`/chameleon-init` cannot leave a half-written `.chameleon/` directory
visible to loaders. The `COMMITTED` sentinel is written last; a
loader that doesn't see it refuses to read the dir and reports
"incomplete profile, run /chameleon-refresh." This is the defense
against an OOM or process-kill mid-bootstrap.

### drift.db survives concurrent writes

The recent stress-test
(`tests/drift_concurrent_writes_test.py`, see commit `d88e1ed`)
covers the previously-flagged gap: multiple PreToolUse hooks
writing to the same `drift.db` under WAL with
`busy_timeout=30000` and exponential-backoff retry. The test
verifies no rows are lost under sustained concurrent writes.

---

## Where the evidence is thin

Be honest about what we have not measured.

### `lint_file` is now real, but narrow

`mcp/chameleon_mcp/tools.py::lint_file` ships as a real engine in
v0.3.0 (Phase 4.1). It compares a file's shape — extracted via
regex heuristics in `mcp/chameleon_mcp/lint_engine.py` — against
the archetype's `ast_query` and returns structural violations
across five rule types. Envelope carries `"stub": false` when the
real engine runs.

What it does NOT yet do:
- AST-precise parsing (regex misses `export { default } from`
  re-exports and other rare forms — acknowledged in the engine's
  module docstring).
- Idiom enforcement. Idioms remain advisory text; the engine only
  checks the cluster-signature dimensions.
- False-positive calibration. Threshold tuning is Phase 6.

This means: chameleon's enforcement layer today is **shape-only**
— same five dimensions used at bootstrap clustering. Logic-level
review still belongs to humans (and other linters).

### Conformance rate is not yet measured

ARCHITECTURE.md Phase 6 lists "80%+ on archetype-matched tasks
across 3 test TS repos" as a calibration target. **This number
has not been measured.** The calibration harness exists in
concept (`MAINTAINER.md#calibration-review`) but has not been
run end-to-end against a labelled corpus. The 80% figure is a
ship gate, not a reported result.

What that means in practice: we have evidence the engine routes
correctly, sanitizes correctly, preserves idioms correctly, and
recovers from failures correctly. We **do not yet have evidence**
that the per-edit conformance rate of AI-generated code, given
chameleon's archetype-aware context, is meaningfully better than
without it. That measurement is Phase 6 work and remains
outstanding.

### Calibration parameters are unvalidated

The parameters listed in `MAINTAINER.md#calibration-review`
(recency_weight, recency_window_days, confidence_function weights,
cluster_size_log base, min_cluster_size, bimodal_threshold) are
all default values from the design phase. They have not been
swept against a labelled corpus to verify they produce sensible
clusters at scale.

### Multi-hour session stability is not exercised

CHANGELOG.md v0.1.0 calls this out under "Known limitations":
"Multi-hour session stability and 50k-file repo at the cap not
exercised at scale." Still true. The 50k-file ceiling is a
design parameter, not a tested bound.

### Real-Claude-Code transcripts are point samples

The `tests/claude_code_acceptance_test.py` suite ($0.20/run)
exercises the model-facing skill loop against a real repo, but
this is a smoke test, not a representative sample. We do not
yet have a corpus of "edits that benefited from chameleon
context" versus "edits that didn't."

---

## What success looks like in production

The metric chameleon was designed to move, restated in
operational terms:

- **Reviewer time spent on logic and security, not on style and
  shape.** A pre-chameleon PR review burns 10 minutes on
  "this should live in `app/services/`, not `lib/`," "we use
  Result types, not throws," "the test should be a request spec,
  not a unit spec." A post-chameleon PR has the file in the
  right place, with the right error pattern, in the right
  test directory — because the model saw the canonical example
  before generating.
- **AI edits that pass review on the first try more often.**
  Specifically: archetype-keyed code that matches the witness
  file's shape closely enough that the diff is a function of
  what the user asked for, not what the model assumed.

Neither of these has a number yet. Both are observable in
dogfooding, and both are what the calibration phase (Phase 6) is
designed to measure.

---

## The honest summary

The security ceremony — sanitization, scanners, atomic commits,
trust gate, HMAC exec log, schema versioning — is verified to
work at the layer it operates at. The audit-driven v0.2.0
release tightened the layers that were closest to user-visible
data (profile.summary.md surfacing, teach_profile validation,
path-pattern bucketing) and added regression coverage to keep
them from rotting.

The conformance-rate claim — that an AI given chameleon's
archetype-aware context produces meaningfully better-fitting code
than without — is plausible but **not yet measured (Phase 6)**.
Phase 7 ships the engine and the security model. Phase 6
measurement, currently outstanding, is what would turn the
plausibility into evidence.

---

## References

- README "Why" — problem statement (`README.md`)
- ADR-0001 — best-effort clustering framing (`docs/chameleon/decisions/0001-best-effort-clustering-vs-framework-aware.md`)
- v0.2.0 CHANGELOG entry — audit-driven fixes (`CHANGELOG.md`)
- `tests/v0_2_regression_test.py` — audit-fix regression coverage
- `tests/comprehensive_test.py` — sanitization + safe_open + atomic commit coverage
- `tests/drift_concurrent_writes_test.py` — concurrent-write stress test
- `MAINTAINER.md#calibration-review` — calibration target register (outstanding)

[audit]: https://github.com/crisnahine/chameleon/blob/main/docs/chameleon-test-report.md
