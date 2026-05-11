# Phase 6 — Calibration measurements (first real run)

Status: **initial baseline captured against 2 real production repos**
(reduced from the Phase 6 target of 4 repos). All measured numbers
**meet or exceed** the Phase 6 targets in [ARCHITECTURE.md](../../ARCHITECTURE.md#calibration-targets),
but the corpus is too small to claim statistical confidence — see
[Where the corpus is thin](#where-the-corpus-is-thin).

## Corpus

| Slot | Repo (anonymized) | Language | Files indexed |
|---|---|---|---|
| 1/4 | TS-A | TypeScript | 2,357 |
| 2/4 | Rails-A | Ruby on Rails | 4,800 |

The corpus paths live in `tests/calibration/corpus.json`, which is
**gitignored** — every maintainer points the harness at their own real
or synthetic repos. The repo names above are anonymized; the harness
JSON below records whatever `name` field each maintainer's `corpus.json`
declares.

Phase 6 in `ARCHITECTURE.md#phase-plan` calls for **3 TypeScript repos
plus 1 Rails repo**. This run hits **1 TS + 1 Rails**. We are
deliberately publishing the baseline at the reduced corpus rather than
waiting for full coverage — the numbers below are real and the gap
is documented in the "thin" section.

## Results (harness JSON, anonymized)

Captured 2026-05-11 from
`PYTHONPATH=mcp:tests mcp/.venv/bin/python tests/calibration/harness.py`.
Per-row `name` fields are anonymized below; the live JSON contains the
maintainer-declared names from their local `corpus.json`.

```json
{
  "status": "ok",
  "rows": [
    {
      "name": "TS-A",
      "status": "ok",
      "archetypes_detected": 7,
      "files_processed": 2357,
      "bootstrap_ms": 3365,
      "samples": 7,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    },
    {
      "name": "Rails-A",
      "status": "ok",
      "archetypes_detected": 149,
      "files_processed": 4800,
      "bootstrap_ms": 2598,
      "samples": 100,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    }
  ],
  "rollup": {
    "repos_ok": 2,
    "archetype_match_rate_mean": 1.0,
    "high_confidence_rate_mean": 1.0,
    "bootstrap_duration_p50_ms": 2598,
    "bootstrap_duration_p95_ms": 3365,
    "cost_per_bootstrap_usd": 0.0
  },
  "targets": {
    "archetype_match_rate_mean": 0.80,
    "bootstrap_duration_p95_ms": 10000
  }
}
```

## Target vs. actual

| Metric | Target (ARCHITECTURE.md) | Measured | Delta | Verdict |
|---|---|---|---|---|
| `archetype_match_rate_mean` | ≥ 0.80 | 1.00 | +0.20 | **PASS** |
| `bootstrap_duration_p95_ms` | ≤ 10,000 | 3,365 | -6,635 | **PASS** |
| `high_confidence_rate_mean` | (no formal target; informational) | 1.00 | n/a | informational |
| `cost_per_bootstrap_usd` | $0 (bootstrap is local) | $0 | 0 | **PASS** |

### How to read these numbers

- The harness samples **one witness file per archetype** from the
  freshly-written `.chameleon/canonicals.json`, capped at 100 files.
  A 100% match rate therefore says: "when you ask `get_pattern_context`
  about each archetype's own canonical witness, every witness gets
  classified back into its own archetype."

  This is the **necessary** condition for chameleon to work at all,
  not the **sufficient** condition. A match rate < 80% on these
  witnesses would mean the canonical selection and the runtime
  classifier disagree — i.e. something is broken. A match rate of
  100% is the floor we should always be at; the real generalization
  question is how the classifier behaves on non-witness files, which
  the harness does **not** measure today (see [Where the corpus is
  thin](#where-the-corpus-is-thin)).

- Bootstrap p95 of **3,365 ms across two repos** (one Rails repo at
  4,800 files, one TS repo at ~2,400 files post-gitignore) is
  **3× under the 10 s ceiling**. The `repo_size_guard` is 50,000 files
  — both repos sit at <10% of that ceiling, so this number does not
  yet test the scaling limit.

- Sample sizes are uneven: 7 (TS) vs 100 (Rails). The TS profile
  produces 7 archetypes total, so 7 witnesses is the maximum
  possible sample for that repo. The Rails profile produces 149
  archetypes (Rails apps have far more naming-conventional
  sub-classes — services, jobs, mailers, serializers, presenters
  per-domain — each becoming its own archetype), so the harness
  random-samples 100 of them with `random.seed(42)`.

## Calibration parameters — proposed tuning, none applied

The targets are met, so **no parameters were tuned in this run**.
Per `docs/chameleon/MAINTAINER.md`, parameters earn a tuning ADR
only when the measured correlation falls below the documented
threshold. Here is the read of each parameter from this run:

| Parameter | Default | Measured signal | Action proposed |
|---|---|---|---|
| `recency_weight` | 2× / 90 days | Can't measure correlation against reviewer-labeled stale canonicals with 2 repos. **Not measured** by this harness — needs human-labeling pass. | Keep default; reopen at 3+ TS repos. |
| `recency_window_days` | 90 | Not measured (would need rolling 7-day repo snapshots). | Keep default. |
| `confidence_function` weights | 0.4 / 0.3 / 0.3 | 100% of witnesses land in `high`/`medium` band. **No miss signal** to correlate against. | Keep default. |
| `cluster_size_log` base | natural log | Not directly observable from harness output. | Keep default. |
| `min_cluster_size` | 5 | Rails repo split into 149 archetypes — many will be at-or-near the floor. **Suggests `min_cluster_size=5` may be too permissive for Rails** (more on this below). | **Watch**: if subsequent Rails repos also produce 100+ archetypes, propose raising the floor to 7 or 10 via ADR. Not changed in this run. |
| `bimodal_threshold` | 60/40 | Not measured. | Keep default. |
| `repo_size_guard` | 50,000 files | Both repos < 5,000 files; ceiling untested. | Keep default. |
| `ast_node_ceiling` | 50,000 nodes | Not surfaced by harness. | Keep default. |
| `MCP timeout` | 2 seconds | p95 bootstrap is 3.4 s, but bootstrap is **not** the per-edit hot path. The per-edit hot path is `get_pattern_context`, which the harness times implicitly (100 calls finish inside the same bootstrap_ms window). | Keep default. |
| `path_pattern_bucket_depth` | 3 | Not surfaced by harness. | Keep default. |

### One concrete observation worth flagging

The Rails repo produces **149 archetypes** for 4,800 files
(~32 files per archetype on average). That's a high archetype count
relative to the 7 archetypes the TS repo derives from 2,357 files
(~337 files per archetype). Two hypotheses:

1. **Real**: Rails really does have more naming-conventional
   sub-categories (every `app/services/<domain>/` becomes an
   archetype). Working as intended.
2. **Tuning signal**: `min_cluster_size = 5` is too permissive for
   the deeper-nested Rails namespace structure; raising to 7 or 10
   would consolidate small archetypes without losing structure.

We cannot distinguish (1) from (2) with one Rails repo. **Action:
do not tune; revisit when corpus contains 2+ Rails apps.**

## How to reproduce

1. Create `tests/calibration/corpus.json` (gitignored — see
   `tests/calibration/README.md` for the schema) with absolute paths
   to your local repos:

   ```json
   {
     "repos": [
       {"name": "ts-repo-1", "path": "/abs/path/to/ts-repo", "language": "typescript"},
       {"name": "rails-repo-1", "path": "/abs/path/to/rails-repo", "language": "ruby"}
     ]
   }
   ```

2. Run the harness from the repo root:

   ```bash
   PYTHONPATH=mcp:tests mcp/.venv/bin/python tests/calibration/harness.py
   ```

3. The harness emits a single JSON object on stdout, plus an
   exit code of 0 even when `corpus.json` is absent (CI-safe
   "no_corpus_configured" branch). Pipe to `jq` for inspection
   or save with `tee`.

4. For automated runs, see `.github/workflows/calibration.yml`
   (manual-dispatch only; we do **not** run this against the public
   GitHub-hosted runners on every push because real corpus paths
   are per-developer secrets).

## Where the corpus is thin

This baseline is honest about its limits.

1. **Only 1 TypeScript repo.** Phase 6's target was 3 TS repos.
   With n=1, we cannot say anything about cross-repo variance, and
   the 100% match rate is a property of one team's conventions, not
   an architectural claim. **Need: 2-3 more TS repos** before we
   can publish "chameleon hits 80%+ archetype-match on TypeScript"
   as a general claim. Candidates: any OSS TS app with a
   conventional folder structure (Next.js apps, NestJS services,
   Astro sites).

2. **Only 1 Rails repo.** Phase 6 wanted 1; we have 1. **Statistical
   floor met for Rails. Statistical confidence: zero.** The 149
   archetypes finding (above) is exactly the kind of thing that
   needs a second Rails repo to tell us whether it's signal or just
   this codebase's structure.

3. **The harness does not measure generalization.** It tests the
   round-trip property "witness file → its own archetype." It does
   not pick non-witness files and check whether they get the
   archetype a human reviewer would have picked. Adding that
   measurement requires a human-labeled gold set per repo — that
   work is deferred to Phase 7 / quarterly maintenance.

4. **No drift / staleness measurement.** The harness runs cold
   against a freshly-bootstrapped repo. It does not test what
   happens after 100 edits land and `posttool-recorder` has
   updated `drift.db`. Drift behavior is tested elsewhere
   (`tests/drift_concurrent_writes_test.py`), but it has no
   calibration row here yet.

5. **No cost measurement on the per-edit hot path.** The harness
   reports `bootstrap_ms` (acceptable: bootstrap is rare). The
   per-edit cost is captured implicitly inside `bootstrap_ms`
   (100 `get_pattern_context` calls in series), but isn't broken
   out. ARCHITECTURE.md's "MCP timeout = 2 s" is the per-call
   ceiling; we should add a separate p99 timing row for
   `get_pattern_context` once the harness grows that capability.
   Not done in this run because the harness is read-only per the
   Phase 6 brief.

## What this baseline does and doesn't establish

**Establishes**:
- The plugin runs end-to-end on two real, large repos without crashing.
- Bootstrap fits in **single-digit seconds** on these repos
  (well under the 10 s p95 ceiling).
- The witness-roundtrip property holds at 100% for both repos.
- Cost is genuinely $0 today (no API calls during bootstrap).

**Does not establish**:
- That archetype-match rate stays ≥80% on novel files (only
  measures witnesses).
- That bootstrap stays under 10 s at 25,000+ files (both repos
  are small relative to the 50,000 ceiling).
- That parameter defaults are right (no correlation measurements
  against reviewer labels).

## Next steps before v1.0 release

1. Add 2 more TypeScript repos to the corpus (any OSS Next.js or
   NestJS project will do — corpus.json supports N repos).
2. Add a second Rails repo if available; reassess `min_cluster_size`.
3. Author a human-labeled gold set per repo (~50 non-witness files
   each, labeled "this file should be in archetype X") and extend
   the harness to measure precision/recall against the gold set.
   That's Phase 7 work.
4. Re-run this measurement quarterly per
   `docs/chameleon/MAINTAINER.md#2-calibration-review`. Update
   this doc in place; archive the previous run's numbers in
   `decisions/` if any parameter is tuned.
