# Phase 6 — Calibration measurements (expanded corpus)

Status: **expanded baseline captured against 7 real repos** (3 internal +
4 OSS), comfortably exceeding the Phase 6 target of 4 repos total.
All measured numbers **meet or exceed** the Phase 6 targets in
[ARCHITECTURE.md](../../ARCHITECTURE.md#calibration-targets). The
witness-roundtrip property still does not measure generalization — see
[Where the corpus is still thin](#where-the-corpus-is-still-thin).

## Corpus

| Slot | Repo | Language | Files indexed | Source |
|---|---|---|---|---|
| 1 | TS-A (anonymized) | TypeScript | 2,357 | internal |
| 2 | Rails-A (anonymized) | Ruby on Rails | 4,800 | internal |
| 3 | `type-fest` | TypeScript | 432 | OSS (sindresorhus/type-fest) |
| 4 | `zod` | TypeScript | 406 | OSS (colinhacks/zod) |
| 5 | `dub-web` | TypeScript | 3,134 | OSS (dubinc/dub, `apps/web/`) |
| 6 | `forem` | Ruby on Rails | 775 | OSS (forem/forem) |
| 7 | `maybe` | Ruby on Rails | 794 | OSS (maybe-finance/maybe) |

Phase 6 in `ARCHITECTURE.md#phase-plan` called for **3 TS repos + 1 Rails
repo = 4 total**. This run lands **3 TS + 2 Rails OSS, plus 1 TS + 1
Rails internal = 7 total**. The internal repos stay anonymized in this
public document; OSS repos keep their real names because the source
URLs are already public.

The corpus paths live in `tests/calibration/corpus.json`, which is
**gitignored** — every maintainer points the harness at their own
mix of internal and OSS repos.

### Repos attempted but excluded

| Repo | Reason | Action |
|---|---|---|
| `mastodon` (mastodon/mastodon) | Bootstrap crashes during workspace-glob expansion. Mastodon's `package.json` declares `"workspaces": [".", "streaming"]`; the leading `"."` entry hits an `IndexError` in `bootstrap/workspace.py:_expand_workspace_globs` on Python 3.11 `pathlib.Path.glob(".")`. Excluded from this run; not silently dropped. | Real chameleon bug (not user error). Tracked separately — `_expand_workspace_globs` should skip empty or pure-`.` glob entries before handing them to `Path.glob`. |
| `gitlabhq` (gitlabhq/gitlabhq) | Not attempted. Repo is ~100k files, which exceeds `repo_size_guard = 50_000`. Verifying the guard correctly trips on it is useful work but separate from corpus expansion. | Deferred. |

The mastodon traceback, verbatim from the harness:

```
File "/.../chameleon_mcp/bootstrap/workspace.py", line 250,
  in _expand_workspace_globs
    for p in repo_root.glob(glob):
File "/.../python3.11/pathlib.py", line 952, in glob
    selector = _make_selector(tuple(pattern_parts), self._flavour)
File "/.../python3.11/pathlib.py", line 282, in _make_selector
    pat = pattern_parts[0]
          ~~~~~~~~~~~~~^^^
IndexError: tuple index out of range
```

## Results (harness JSON)

Captured 2026-05-11 from
`cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/calibration/harness.py`.
Per-row `name` fields preserve whatever the maintainer-declared name was
in their local `corpus.json` — anonymized for the two internal repos
below, real for the OSS ones.

```json
{
  "status": "ok",
  "rows": [
    {
      "name": "TS-A",
      "status": "ok",
      "archetypes_detected": 7,
      "files_processed": 2357,
      "bootstrap_ms": 6244,
      "samples": 7,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    },
    {
      "name": "Rails-A",
      "status": "ok",
      "archetypes_detected": 149,
      "files_processed": 4800,
      "bootstrap_ms": 4327,
      "samples": 100,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    },
    {
      "name": "type-fest",
      "status": "ok",
      "archetypes_detected": 1,
      "files_processed": 432,
      "bootstrap_ms": 540,
      "samples": 1,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    },
    {
      "name": "zod",
      "status": "ok",
      "archetypes_detected": 2,
      "files_processed": 406,
      "bootstrap_ms": 3082,
      "samples": 2,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    },
    {
      "name": "dub-web",
      "status": "ok",
      "archetypes_detected": 9,
      "files_processed": 3134,
      "bootstrap_ms": 2404,
      "samples": 9,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    },
    {
      "name": "forem",
      "status": "ok",
      "archetypes_detected": 7,
      "files_processed": 775,
      "bootstrap_ms": 1188,
      "samples": 7,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    },
    {
      "name": "maybe",
      "status": "ok",
      "archetypes_detected": 32,
      "files_processed": 794,
      "bootstrap_ms": 516,
      "samples": 32,
      "archetype_match_rate": 1.0,
      "high_confidence_rate": 1.0
    }
  ],
  "rollup": {
    "repos_ok": 7,
    "archetype_match_rate_mean": 1.0,
    "high_confidence_rate_mean": 1.0,
    "bootstrap_duration_p50_ms": 2404,
    "bootstrap_duration_p95_ms": 6244,
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
| `archetype_match_rate_mean` | >= 0.80 | 1.00 | +0.20 | **PASS** |
| `bootstrap_duration_p95_ms` | <= 10,000 | 6,244 | -3,756 | **PASS** |
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
  still thin](#where-the-corpus-is-still-thin)).

- Bootstrap p95 of **6,244 ms across seven repos** (largest is the
  internal Rails repo at 4,800 files; smallest is `zod` at 406)
  comfortably clears the 10 s ceiling, though the p95 rose from the
  prior 2-repo run (3,365 ms). The increase is driven by larger
  repos — the internal TS repo at 2,357 files re-ran at 6,244 ms,
  ~2x the previous result. Likely cold-cache differences across
  the run; would need repeated runs to claim significance.

- Sample sizes vary widely (1 to 100) because the harness samples
  **one witness per archetype**. `type-fest` is a single-archetype
  type-level utility library (1 sample), while the internal Rails
  repo derives 149 archetypes (capped at 100 samples via
  `random.seed(42)`).

### Cross-repo archetype density

| Repo | Files | Archetypes | Files/archetype |
|---|---|---|---|
| `type-fest` | 432 | 1 | 432 |
| `zod` | 406 | 2 | 203 |
| `TS-A` (internal) | 2,357 | 7 | 337 |
| `dub-web` | 3,134 | 9 | 348 |
| `forem` | 775 | 7 | 111 |
| `maybe` | 794 | 32 | 25 |
| `Rails-A` (internal) | 4,800 | 149 | 32 |

Three observations from the expanded view:

1. **TS repos cluster at 200-400 files/archetype.** Four TS repos
   (type-fest, zod, TS-A, dub-web) all land in that band. This is
   the first cross-repo signal that the default
   `min_cluster_size = 5` produces consistent archetype granularity
   on TypeScript.

2. **Rails repos vary 4x in density** (111 to 32 files/archetype).
   `forem` is closer to TS density than to other Rails repos. One
   plausible explanation: forem has heavy gitignore exclusions
   (their `.gitignore` filters generated assets, vendored code,
   etc.) — the harness processes 775 files even though `find` sees
   3,551 `*.rb` files. After filtering, what remains are the
   conventional Rails directories that cluster well.

3. **`maybe` (25 files/archetype) is denser than `Rails-A` (32).**
   Both internal-Rails and maybe land in the same density band,
   strengthening the hypothesis from the prior run that Rails
   genuinely produces deeper archetype trees than TS — not a
   tuning artifact of one team's codebase. The earlier
   "watch min_cluster_size for Rails" flag is now closer to
   "this is just how Rails clusters, working as intended."

## Calibration parameters — proposed tuning, none applied

The targets are met, so **no parameters were tuned in this run**.
Per `docs/chameleon/MAINTAINER.md`, parameters earn a tuning ADR
only when the measured correlation falls below the documented
threshold. Here is the read of each parameter from this run:

| Parameter | Default | Measured signal | Action proposed |
|---|---|---|---|
| `recency_weight` | 2x / 90 days | Can't measure correlation against reviewer-labeled stale canonicals — needs human-labeling pass. | Keep default; revisit in Phase 7. |
| `recency_window_days` | 90 | Not measured (would need rolling 7-day repo snapshots). | Keep default. |
| `confidence_function` weights | 0.4 / 0.3 / 0.3 | 100% of witnesses land in `high`/`medium` band across all 7 repos. **No miss signal.** | Keep default. |
| `cluster_size_log` base | natural log | Not directly observable from harness output. | Keep default. |
| `min_cluster_size` | 5 | TS density tightly clustered (200-400 files/archetype) across 4 repos. Rails density 25-32 (internal + maybe) with one outlier (forem at 111). **Earlier "raise floor for Rails" hypothesis weakened**: two Rails repos now agree on ~30 files/archetype density. | Keep default. |
| `bimodal_threshold` | 60/40 | Not measured. | Keep default. |
| `repo_size_guard` | 50,000 files | Largest repo in corpus is 4,800 files. Ceiling untested. `gitlabhq` (~100k) would test it but wasn't attempted this run. | Keep default. |
| `ast_node_ceiling` | 50,000 nodes | Not surfaced by harness. | Keep default. |
| `MCP timeout` | 2 seconds | p95 bootstrap is 6.2 s, but bootstrap is **not** the per-edit hot path. Per-edit `get_pattern_context` is implicit inside bootstrap timings. | Keep default. |
| `path_pattern_bucket_depth` | 3 | Not surfaced by harness. | Keep default. |

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

2. Shallow-clone the OSS calibration repos (free, public):

   ```bash
   git clone --depth 1 https://github.com/sindresorhus/type-fest /tmp/calib_type_fest
   git clone --depth 1 https://github.com/colinhacks/zod         /tmp/calib_zod
   git clone --depth 1 https://github.com/dubinc/dub             /tmp/calib_dub
   git clone --depth 1 https://github.com/forem/forem            /tmp/calib_forem
   git clone --depth 1 https://github.com/maybe-finance/maybe    /tmp/calib_maybe
   ```

   For `dub`, point `corpus.json` at `/tmp/calib_dub/apps/web`
   (the package, not the monorepo root).

3. Run the harness from the repo root:

   ```bash
   cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/calibration/harness.py
   ```

4. The harness emits a single JSON object on stdout, plus an
   exit code of 0 even when `corpus.json` is absent (CI-safe
   "no_corpus_configured" branch).

5. For automated runs, see `.github/workflows/calibration.yml`
   (manual-dispatch only; corpus paths are per-developer).

## Where the corpus is still thin

This baseline is honest about its remaining limits.

1. **The harness does not measure generalization.** It tests the
   round-trip property "witness file -> its own archetype." It does
   not pick non-witness files and check whether they get the
   archetype a human reviewer would have picked. Adding that
   measurement requires a human-labeled gold set per repo — that
   work is deferred to Phase 7 / quarterly maintenance.

2. **No drift / staleness measurement.** The harness runs cold
   against a freshly-bootstrapped repo. It does not test what
   happens after 100 edits land and `posttool-recorder` has
   updated `drift.db`. Drift behavior is tested elsewhere
   (`tests/drift_concurrent_writes_test.py`), but it has no
   calibration row here yet.

3. **No cost measurement on the per-edit hot path.** The harness
   reports `bootstrap_ms` (acceptable: bootstrap is rare). The
   per-edit cost is captured implicitly inside `bootstrap_ms`
   (N `get_pattern_context` calls in series), but isn't broken
   out. ARCHITECTURE.md's "MCP timeout = 2 s" is the per-call
   ceiling; we should add a separate p99 timing row for
   `get_pattern_context` once the harness grows that capability.

4. **`repo_size_guard` still untested.** Largest repo in corpus is
   4,800 files (<10% of the 50,000 ceiling). `gitlabhq` (~100k)
   would test the guard but wasn't attempted this run.

5. **Mastodon excluded.** A real chameleon bug (workspace-glob
   IndexError on `"."` entries) blocked it. That's a tracked
   bug, not a corpus gap — but the corpus would be 8 repos
   (4 TS + 4 Rails) if the bug were fixed.

## What this baseline does and doesn't establish

**Establishes**:
- The plugin runs end-to-end on **7 real repos** spanning OSS and
  internal codebases, TS and Rails, monorepo and flat layouts.
- Bootstrap fits in **single-digit seconds** on all 7 repos
  (max 6.2 s, well under the 10 s p95 ceiling).
- The witness-roundtrip property holds at 100% across all 7 repos
  (was 100% on 2 — now demonstrated stable at 7).
- TS archetype density is **consistent across 4 unrelated TS
  codebases** (200-400 files/archetype), strengthening the case
  that `min_cluster_size = 5` is well-calibrated for TS.
- Rails archetype density is **consistently denser than TS**
  (25-111 files/archetype), reducing the prior "is this an
  internal-repo artifact" concern.
- Cost is genuinely $0 today (no API calls during bootstrap).
- The plugin surfaces a **real bug** on mastodon's workspace
  declaration (verbatim trace above); this is now an open
  bug-fix task rather than an unknown.

**Does not establish**:
- That archetype-match rate stays >=80% on novel files (only
  measures witnesses).
- That bootstrap stays under 10 s at 25,000+ files (largest
  corpus repo is 4,800 files).
- That parameter defaults are right (no correlation measurements
  against reviewer labels).

## Next steps before v1.0 release

1. **Fix the mastodon workspace-glob bug** (`workspace.py:250`)
   and re-include mastodon in the corpus.
2. Optionally attempt `gitlabhq` to verify `repo_size_guard`
   trips correctly.
3. Author a human-labeled gold set per repo (~50 non-witness files
   each, labeled "this file should be in archetype X") and extend
   the harness to measure precision/recall against the gold set.
   That's Phase 7 work.
4. Re-run this measurement quarterly per
   `docs/chameleon/MAINTAINER.md#2-calibration-review`. Update
   this doc in place; archive the previous run's numbers in
   `decisions/` if any parameter is tuned.
