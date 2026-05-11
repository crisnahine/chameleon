# chameleon v0.5.0 dogfood report — `gitlabhq/gitlabhq`

**Target:** GitLab Community Edition source tree
**Path:** `/Users/crisn/Documents/Projects/Testing Apps/gitlabhq`
**Sub-path under test:** `app/` (Rails app code only)
**Language profile:** Ruby on Rails (+ Vue/JS frontend at root)
**Date:** 2026-05-11
**Tester:** dogfood QA pass (chameleon v0.5.0)
**Pre-existing `.chameleon/`:** none

## TL;DR

1. **Did the 50k cap trip cleanly on the root?**
   **No — not via the default `bootstrap_repo(path)` call.** The root contains 66,054 total files but the TypeScript extractor wins extractor-precedence (`package.json` present), and its `**/*.{ts,tsx,js,jsx,mjs,cjs}` glob filters the candidate set to **6,576 files** post-exclusion — well below the 50k ceiling. Bootstrap **succeeded** at the root, took 11.2s wall, produced a (Vue/JS-flavored) profile with 13 archetypes. The Ruby glob `**/*.rb` would also be under the cap (28,789 files). The guard only fires when the caller explicitly passes `paths_glob='**/*'`; with that, the guard tripped **cleanly and atomically**: status `failed_too_many_files`, error `"Repo has 65101 files (ceiling 50000); use explicit paths_glob"`, no `.chameleon/` was left at the root, duration 1,041 ms.

2. **Does bootstrap on a 25k-file Rails sub-tree complete in time?**
   **Yes — emphatically.** Bootstrap on `gitlabhq/app/` (6,475 Ruby files processed; 14,917 total files in tree) completed in **3.48s wall / 1,734 ms internal duration / 2,408 ms after a forced full refresh**. Well under the 10s p95 calibration target.

3. **Did peak RSS stay under 2 GiB?**
   **Yes — peak RSS was 74.7 MB (71.27 MiB) during bootstrap and 76.3 MB during forced refresh.** ~3.5% of the 2 GiB ceiling.

4. **Any other gitlabhq-specific bugs?**
   - **Issue #1 (UX, not a bug):** Bootstrap on `gitlabhq/app/` fails out of the box with `status=failed_unsupported_language` because Rails apps don't put `Gemfile` inside `app/` — it lives at the repo root. `RubyExtractor.can_handle()` checks for `Gemfile` / `*.gemspec` at the *bootstrap root only*. **Chameleon cannot bootstrap a Rails sub-tree without copying or stubbing a Gemfile.** Workaround used: dropped a minimal `Gemfile` into `app/` for the run, then deleted it.
   - **Issue #2 (silent partial coverage):** Calling `bootstrap_repo(root, paths_glob='app/**/*.rb')` from the root **does NOT** invoke the Ruby extractor (TS still wins precedence). The TS extractor parses `.rb` files as JS and reports `status=success` with `files_processed=1611` — far below the 6,475 the Ruby extractor finds. Caller has no signal of the mismatch.
   - **Issue #3 (zero rules extracted):** `rules_extracted=0`, `idioms_collected=0` on the bootstrap. Acceptable for a stock-format Rails app (no shipped rubocop/standard config at `app/`), but `tool_configs.sources` was empty.
   - **Issue #4 (sparse-cluster warning flood):** 1,288 sparse cluster warnings on this bootstrap (most are graphql resolver/type subdirectories with <5 files). The Phase 2C.3 sparse-cluster channel could swamp a future interview UI.

## Phase 1 — Repo-size guard verdict

### Tested
Three invocations against `/Users/crisn/Documents/Projects/Testing Apps/gitlabhq`:

| Invocation                                | Post-exclusion file count | Outcome                    | Wall    |
|-------------------------------------------|---------------------------|----------------------------|---------|
| `bootstrap_repo(root)` (TS auto)          | 6,576                     | `success` (13 archetypes)  | 11.2s   |
| `discover_files(glob='**/*.rb')`          | 28,789                    | under cap, no error        | n/a     |
| `discover_files(glob='**/*.{ts,...}')`    | 6,576                     | under cap, no error        | n/a     |
| `bootstrap_repo(root, paths_glob='**/*')` | 65,101                    | `failed_too_many_files`    | 1.04s   |

### Exact guard-trip envelope

```json
{
  "status": "failed_too_many_files",
  "archetypes_detected": 0, "rules_extracted": 0, "idioms_collected": 0,
  "files_processed": 0, "duration_ms": 1041, "profile_path": null,
  "error": "Repo has 65101 files (ceiling 50000); use explicit paths_glob"
}
```

After the guard tripped: `ls /Users/.../gitlabhq/.chameleon` → `No such file or directory`. **No partial state left behind. Atomic failure confirmed.**

### Key insight
The 50k cap is **post-language-glob-filtered**, not pre-glob (see `mcp/chameleon_mcp/bootstrap/discovery.py:174`). For a hybrid 66k-file repo with TS *and* Ruby, the cap is effectively unreachable through normal `bootstrap_repo()`. By design, but worth documenting prominently.

## Phase 2 — Standard protocol on `gitlabhq/app`

> **Setup note.** A one-line `Gemfile` was placed in `app/` to satisfy `RubyExtractor.can_handle()`, then removed at the end. This is Issue #1.

### A. Bootstrap from scratch

| Metric                       | Value                                                                |
|------------------------------|----------------------------------------------------------------------|
| Status                       | `success`                                                            |
| Archetypes detected          | **297**                                                              |
| Rules / idioms collected     | 0 / 0                                                                |
| Files processed / skipped    | 6,475 / 0                                                            |
| Sparse / bimodal warnings    | 1,288 / 0                                                            |
| Internal `duration_ms`       | 1,734                                                                |
| Wall (Python harness)        | 3.48s                                                                |
| Profile path                 | `/Users/.../gitlabhq/app/.chameleon`                                 |
| Repo ID                      | `bdf192571e0210e7201a64d899e937a9ea76399b819fb5ef77795c022c1c2e78`   |

Distinctive GitLab archetypes detected: `class-pajamas` (21 files, Pajamas design-system), `class-rapid-diffs` (8), `class-work-items` (8), `class-resolvers` (75 graphql resolvers), `service-gitlabhq`, `policy`.

### F. Refresh

| Mode                       | Wall    | duration_ms | Files  | Archetypes | RSS      |
|----------------------------|---------|-------------|--------|------------|----------|
| Initial bootstrap          | 3.48s   | 1,734       | 6,475  | 297        | 74.7 MB  |
| `refresh_repo(force=True)` | 4.14s   | 2,408       | 6,475  | 297        | 76.3 MB  |
| `refresh_repo(force=False)`| 0.25s   | 0 (noop)    | cached | 297        | (no run) |

Deterministic — same 297 archetypes on re-run. Non-force returns `status="noop"` instantly.

### J. GitLab-specific verification

**5 services (`app/services/*.rb`):** all → `service-gitlabhq` (high confidence, alt `service-5`). Consistent.

**5 policies (`app/policies/*.rb`):** all → `policy` (high confidence, alt `policy-gitlabhq`). Consistent.

**Archetype spread by top-level `app/` directory:**

| Top dir      | # archetypes | total cluster size |
|--------------|--------------|--------------------|
| services     | 77           | 769                |
| graphql      | 69           | 921                |
| models       | 39           | 834                |
| workers      | 32           | 406                |
| controllers  | 23           | 350                |
| serializers  | 13           | 267                |
| finders      | 11           | 123                |
| presenters   | 7            | 69                 |
| helpers      | 6            | 163                |
| components   | 5            | 54                 |
| mailers      | 5            | 43                 |
| policies     | 5            | 95                 |
| events       | 3            | 21                 |
| uploaders    | 1            | 13                 |
| validators   | 1            | 43                 |

Heaviest-clustered: `graphql` (921 files), `models` (834), `services` (769). Most fragmented: `services` (77 archetypes / 769 files = 0.10 archetypes/file) reflects GitLab's "skinny controller / fat service" architecture.

### K. Performance signals

**Bootstrap, `/usr/bin/time -l` output (verbatim):**

```
        3.51 real         2.39 user         1.11 sys
            74727424  maximum resident set size
               19086  page reclaims
                  17  page faults
                2094  voluntary context switches
               13543  involuntary context switches
         22029560885  instructions retired
          5716998976  cycles elapsed
            62063096  peak memory footprint
```

| Signal                              | Bootstrap                | Forced refresh        |
|-------------------------------------|--------------------------|-----------------------|
| Wall-clock (real)                   | **3.51s**                | **4.17s**             |
| User CPU / System CPU               | 2.39s / 1.11s            | 2.59s / 1.35s         |
| Peak RSS                            | **74,727,424 B (71.3 MiB)** | **76,316,672 B (72.8 MiB)** |
| Peak memory footprint               | 59.2 MiB                 | 60.7 MiB              |
| Files indexed                       | 6,475                    | 6,475                 |
| Files / second                      | 1,844                    | 1,553                 |
| Exceeded 10s p95 target?            | **No** (≈3× under)       | **No** (≈2.4× under)  |
| Exceeded 2 GiB RSS ceiling?         | **No** (≈3.5% of 2 GiB)  | **No**                |

## Findings summary

| # | Severity | Area | Finding |
|---|----------|------|---------|
| 1 | LOW (UX) | Ruby `can_handle` | Cannot bootstrap a Rails sub-directory because `Gemfile` / `*.gemspec` must sit *at the bootstrap root*. Suggest: walk upward looking for Gemfile/gemspec, OR document the limitation in `docs/ruby.md` with the "drop a stub Gemfile" workaround. |
| 2 | MEDIUM (correctness) | Extractor + paths_glob mismatch | `bootstrap_repo(ts_root, paths_glob='**/*.rb')` runs the TS extractor over `.rb` files and reports `success` with massive parse loss (1,611 / 6,475). Caller has no signal. Suggest: when `paths_glob` extension set doesn't overlap with `_glob_for_extractor(extractor)`, warn or refuse. |
| 3 | LOW | 50k guard observability | The cap is correctly enforced but only **post-language-glob**. Architectural docs could clarify so users don't expect it to trip on raw file count. |
| 4 | LOW | Sparse-cluster warning volume | 1,288 sparse-cluster warnings on this run is noisy. Consider grouping by top-level path prefix in the payload. |
| 5 | INFO | Performance headroom | 6.5k Ruby files bootstrapped in 1.7s internal / 3.5s wall at 75 MB peak RSS. Very comfortable margin against 10s / 2 GiB ceilings. |

## Top 3 surprises

1. **The 50k cap never trips on the natural `bootstrap_repo(gitlabhq_root)` call** — the brief assumed a 66k-file repo would obviously exceed it, but language-glob filtering brings the effective count to 6.5k (TS-wins) or 28.7k (Ruby) — both under cap. The cap fires only with `paths_glob='**/*'`.
2. **Bootstrap on the Rails `app/` sub-tree fails out of the box** with `failed_unsupported_language` because Rails apps don't put Gemfile in `app/`. Real product friction for "scope chameleon to a sub-folder of a monorepo" users.
3. **GitLab's service-object layer is the dominant archetype source**, not models. `services/` produces 77 of the 297 archetypes (26%) — confirming "skinny controller / fat service" GitLab convention and showing chameleon's clustering picks up that texture cleanly.
