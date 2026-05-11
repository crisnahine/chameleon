# Dogfood SUMMARY — v0.5.3 cycle 3

Third dogfood pass under v0.5.3. Wiped every `.chameleon/` dir + plugin data dir before launch so each app bootstrapped from scratch. 9 apps run end-to-end through 10 phases each, exercising every MCP tool surface.

Reports under `REPORT-<app>.md`. Runner script: `run_dogfood.py`.

## Apps tested

| App | Shape | Files | Archetypes | Generic | Bootstrap | Result |
|---|---|---|---|---|---|---|
| bulletproof-react | monorepo-ts | 447 | 12 | 6 | 0.8s | 41 PASS, 0 FAIL, 2 FINDING |
| Rails-B (internal) | rails-only | 4,799 | 207 | 0 | 9.5s | 42 PASS, 0 FAIL, 1 FINDING |
| TS-B (internal) | ts-only | 2,357 | 17 | 0 | 17.5s | 42 PASS, 0 FAIL, 1 FINDING |
| excalidraw | monorepo-ts | 629 | 4 | 0 | 7.7s | 41 PASS, 0 FAIL, 2 FINDING |
| forem | rails-with-frontend | 3,514 | 127 | 0 | 4.5s | 43 PASS, 0 FAIL, 1 FINDING |
| gitlabhq | rails-with-frontend (legacy sprockets) | 28,789 | 1,197 | 4 | 34.5s | 43 PASS, 0 FAIL, 1 FINDING |
| mastodon | rails-with-frontend | 3,178 | 146 | 1 | 4.4s | 42 PASS, 0 FAIL, 2 FINDING |
| maybe | rails-with-frontend | 793 | 59 | 1 | 1.3s | 43 PASS, 0 FAIL, 1 FINDING |
| plane | monorepo-ts | 3,581 | 70 | 12 | 18.1s | 41 PASS, 0 FAIL, 2 FINDING |

**Totals: 378 PASS, 0 FAIL, 13 FINDING across all 9 apps. Wall time: ~35s p90 per app.**

## v0.5.3 fixes verified in real-world data

Every v0.5.3 fix that landed in the previous cycle is confirmed working under cycle 3:

| Fix | Cycle 2 status | Cycle 3 status |
|---|---|---|
| Bug A — `get_canonical_excerpt` no_witness envelope | Empty content on 3 apps | Typed `no_witness` / `archetype not found` envelopes firing on 3 apps |
| Bug B — Workspace-level monorepo bootstrap | bulletproof-react FAILED bootstrap | bulletproof-react bootstraps in 0.8s with 12 archetypes |
| Bug C — TS naming priors | plane 35/70 generic (50%) | plane 12/70 generic (17%) — 66% reduction |
| Bug C — Rails naming priors | already shipped in v0.5.2 | Forem 0/127, Rails-B 0/207, mastodon 1/146, maybe 1/59, gitlabhq 4/1197 |
| Bug D — Bootstrap coverage instrumentation | gitlabhq 13 archetypes / 6,574 files | gitlabhq 1,197 archetypes / 28,789 files (post-cap-bump) |
| Bug E — Rails+JS legacy sprockets hybrid detect | gitlabhq hybrid undetected | `_is_rails_with_frontend(gitlabhq) == True` verified |
| REPO_SIZE_GUARD 200K | n/a | gitlabhq fits inside (28,789 files post-exclusion) |

## New bugs surfaced in cycle 3

### Bug F — TS prior table doesn't strip workspace prefix (2-app confirmation)

**Severity:** Medium.
**Confirmed on:** plane (12/70 generic archetypes, all under `packages/<workspace>/src/`), bulletproof-react (6/12 generic, all under `apps/<workspace>/src/`).

**Symptom:** v0.5.3's Bug C added a TS prior table that matches directory chains like `app/api/`, `app/`, `pages/`, `components/`, `hooks/`, etc. These match correctly on flat repos. But workspace bootstrap (v0.5.3 Bug B) feeds paths that start with `apps/<workspace>/` or `packages/<workspace>/`. The prior table's `_has_dir_chain` walker doesn't strip the workspace prefix before matching, so:
- `packages/propel/src/components/Foo.tsx` does NOT match the `components/` rule
- `apps/nextjs-app/src/app/page.tsx` does NOT match the `app-page-component` rule

Sample generic archetypes from plane:
- `packages/propel/src:tsx` × 4 (would have been `component`, `app-page-component`, etc.)
- `packages/propel/src:ts` × 4 (would have been `lib-module`, `util`, etc.)
- `packages/editor/src:ts`
- `packages/i18n/src:ts`
- `packages/constants/src:ts`
- `apps/web/app:tsx`

**Suggested fix:** When the bootstrap envelope carries `workspace_roots`, the naming pipeline should detect that a member path starts with one of those roots and SKIP that prefix when running `_has_dir_chain`. Algorithm:
1. Build `workspace_roots_set = {ws for ws in bootstrap.workspace_roots}` (e.g., `{"apps/web", "apps/nextjs-app", "packages/propel"}`).
2. For each member path in the cluster, compute `effective_parts = parts_after_workspace_root_strip(path, workspace_roots_set)`.
3. Run `_has_dir_chain(effective_parts, chain)` instead of `_has_dir_chain(path.parts, chain)`.

The workspace_roots envelope field is already populated by Bug B; piping it into naming.py is the missing wire.

### Bug G (runner-side, NOT chameleon) — phase_7 caches stale archetype name across phase_5 re-bootstrap

**Symptom:** 3 reports (excalidraw, mastodon, plane) show `get_canonical_excerpt(repo_id, "action")` returning `{"status": "failed", "error": "archetype not found"}`.

**Why:** Phase 1 reads `archetypes.json` and caches `archetypes[0].name = "action"`. Phase 5 re-bootstraps to verify atomic_profile_commit sibling preservation. Re-bootstrap may produce a different archetype set (different cluster ordering, witness selection, naming-prior order). Phase 7 then calls `get_canonical_excerpt` with the stale name from phase 1.

This is the v0.5.3 Bug A `archetype_not_found` envelope correctly firing — chameleon is doing the right thing. The runner just needs to re-read archetypes.json after phase 5.

**Suggested runner fix:** After phase 5's bootstrap, refresh the cached `archetypes` list from disk before passing into phase 7.

### Other runner-side cosmetic issues

- **"Bug 1 FINDING" across all 9 reports**: the runner tags the pass branch as FINDING (annotation typo from cycle 2). Cosmetic.
- **gitlabhq report shows "secondary=None"**: the runner uses `lang_hint.get("secondary")` but the actual field is `secondary_detected`. The hint IS emitted correctly (gitlabhq's bootstrap DID detect the legacy `app/assets/javascripts/` sidecar — Bug E fix verified).
- **bulletproof-react path bucket `apps/nextjs-app/src:ts`**: this is the v0.5.2 monorepo-aware bucket combined with the v0.5.2 extension-aware bucket suffix. Working as intended.

## Coverage observations

The instrumentation envelope (v0.5.3 Bug D) made gitlabhq's coverage story legible. Pre-v0.5.3, the report would have shown `files_processed=28,789` and stopped there. Now we have visibility into pre-exclusion vs post-exclusion vs clustered vs sparse-dropped. Future cycles should surface these counts in the runner output explicitly.

The 200K REPO_SIZE_GUARD bump was load-bearing for gitlabhq: at 100K cap, only 6,574 files surfaced. At 200K, 28,789 files (4.4x improvement) and the archetype count jumped from 13 to 1,197 (a 92x improvement) because the larger sample lets more clusters cross the sparse-cluster threshold.

## v0.5.4 patch plan (proposed)

1. **Bug F** (Medium, 2-app confirmation): pipe `workspace_roots` from `bootstrap_repo` envelope through to `naming.py` so the TS prior table strips workspace prefix before matching. ~80 LOC across `bootstrap/naming.py` + `bootstrap/orchestrator.py`. Verify-before reproducer: plane's `packages/propel/src/components/Foo.tsx` cluster gets named `component` instead of `cluster-<hex>`.
2. **Runner Bug G** (runner-side): re-read archetypes.json after phase 5. ~5 LOC in `run_dogfood.py`.
3. **Runner cosmetic** (runner-side): fix `lang_hint.get("secondary")` → `secondary_detected`, and the Bug 1 FINDING-vs-PASS tagging. ~10 LOC.

## Deferred to v0.6

Same 11 findings from v0.5.1 (semantic prompt-injection NL heuristic, Next.js route group recognition, Phase 6 calibration corpus refresh, etc.) plus the 4 minor concerns from the v0.5.3 code review (symlink walking defense, error-branch instrumentation defaults, class-default vocabulary, stress_50k wall-time at new cap).

## Notes on testing methodology

- Pre-flight: every `.chameleon/` dir in `/Users/crisn/Documents/Projects/Testing Apps/*` was wiped before launch. `~/.local/share/chameleon/` (plugin data dir) was also wiped. Each app's run got an isolated `CHAMELEON_PLUGIN_DATA` tmpdir so trust grants didn't bleed across apps.
- Two internal repos (anonymized as `Rails-B` and `TS-B`) were tested with real paths; their per-app reports are intentionally excluded from the public commit (gitignored).
- Cycle 3 wall-time per app: 1.3s (maybe) to 34.5s (gitlabhq); median ~9s.
