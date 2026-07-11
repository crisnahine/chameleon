# Real-world effectiveness study (pre-registered)

Registered before any data analysis, per the project's evidence discipline: the
hypotheses, metrics, windows, and success bars below are fixed now; results
publish whatever they say. This is the study the session-scale A/B campaigns
could not be (see docs/gap-log.md 2026-07-11 addenda for the eight-experiment
proof of why: small fixtures are sibling-inferable, real repos are
non-uniform, one-shot cells cannot see turn-end value, and the deterministic
scorer reads structure rather than idioms).

## Question

Does chameleon measurably improve AI-assisted code output in real-world use on
production repositories — where conventions are NOT fully inferable from the
context a model reads, and where multi-turn sessions let turn-end review act?

## Setting

The Empire Flippers dogfood repos (`ef-api` Rails, `ef-client` TypeScript),
where chameleon has run in daily use since its adoption on **2026-06-01**
(plugin install date, verified from installed_plugins.json). Both repos carry
months-to-years of pre-adoption history: the "before" arm already exists in
git history.

## Design: two complementary quasi-experiments

Randomized assignment is impossible in real work, so the study combines two
observational designs whose confounds differ — concordant results across both
are the evidence standard:

### D1 — Interrupted time series (before/after adoption)

Unit: calendar month. For every mainline commit in a month, lint the ADDED
lines of its changed source files with chameleon's own `lint_file` (the same
deterministic engine the eval scorer uses) against the repo's profile, and
compute the month's **new-violation rate** (violations introduced per 100
changed source files). Plot 2026-01 → present with the 2026-06-01 adoption
line. Secondary series: PR review-comment rate per month (non-deleted
Bitbucket comments per merged PR — the outcome the product goal names).

- H1: the post-adoption mean new-violation rate is lower than the
  pre-adoption mean.
- H2: the post-adoption mean review-comment rate per PR is lower than the
  pre-adoption mean.

### D2 — Governed vs ungoverned changes (contemporaneous comparison)

Chameleon's session attestations record, per real session, which files were
chameleon-governed. The review ledger records verdicts; drift.db records edit
observations and would-block shadow events. Unit: merged change (PR or
mainline commit) in the post-adoption window, classified **governed** (its
files appear in a session attestation's governed set) vs **ungoverned**.

- H3: governed changes carry a lower new-violation rate than contemporaneous
  ungoverned changes on the same repo.
- H4: governed PRs draw fewer review comments than ungoverned PRs merged in
  the same window.

## Measurement instrument

Three deterministic, free, no-LLM-spawn scripts (built with this registration),
all reading the repo's committed profile:

- `tests/study_retrospective.py` — D1 new-violation rate, per-commit rows.
- `tests/study_review_comments.py` — H2 comments/PR from read-only Bitbucket
  (cached under `tests/effectiveness/.study_cache/`, gitignored).
- `tests/study_d2.py` — D2 governed vs ungoverned, from session attestations.
- `tests/study_analyze.py` — two-sample cluster bootstrap CI over the units
  (unit-tested in `tests/effectiveness/tests/test_study_analyze.py`).

## Declared limitations (fixed now, not after results)

- **Profile look-ahead**: lint uses the current profile against historical
  commits; conventions that changed mid-window bias both arms in the same
  direction but are noted per-repo. Mitigation recorded, not applied in v1.
- **Adoption is not exogenous**: team practices co-evolve; D1 alone cannot
  separate chameleon from other June changes. That is exactly why D2 exists —
  its comparison is within the same weeks.
- **Attestation coverage**: D2's governed set only exists post-adoption and
  only for sessions on machines with chameleon; ungoverned changes include
  human-only commits. Direction of that bias is stated with the results.
- **Local clone freshness**: the "after" window grows as the clones are
  refreshed; the study re-runs at each release (per the CLAUDE.md release
  rule) and the time series accumulates power over months.

## Success bars (fixed)

- Each hypothesis is reported as the paired direction + a bootstrap 95% CI on
  the rate difference; "supported" requires the CI to exclude zero in the
  predicted direction.
- The study claims "chameleon measurably improves output in real-world use"
  only if H1/H2 (time series) AND H3/H4 (contemporaneous) are concordantly
  supported; partial support is reported as exactly that.
- All results publish verbatim to `tests/effectiveness/results-published/`
  whatever they say, like every prior artifact.

## Cadence

Baseline (this registration): full retrospective through the current clone
tips. Then re-measured at every release; first powered read expected after
2-3 months of post-adoption accumulation.

## Companion experiment (not part of this pre-registration)

This retrospective measures repo-wide before/after, where only a fraction of
changes are chameleon-governed and org-level confounds dominate — all three
arms came back null or confounded (`results-published/dogfood-study-2026-07-11.md`).
A separate controlled A/B (`tests/study_migration_ab.py`,
`results-published/migration-ab-2026-07-11.md`) isolates chameleon on the
scenario it is built for — a migration state where the visible majority
misleads — and there the effect is large and significant (combined 22% -> 94%
correct-convention adherence, +72pp, 95% CI [50, 94], across two models). Read
the two together: chameleon's value shows up in the controlled scenario, not in
the confounded repo-wide aggregate.
