# Dogfood SUMMARY — v0.5.4 cycle 4

Fourth dogfood pass under v0.5.4. Wiped every `.chameleon/` dir + plugin data dir before launch so each app bootstrapped from scratch. 9 apps run end-to-end through 10 phases each, exercising every MCP tool surface.

Reports under `REPORT-<app>.md`. Runner script: `run_dogfood.py`.

## Apps tested

| App | Shape | Files | Archetypes | Generic | Bootstrap | Result |
|---|---|---|---|---|---|---|
| bulletproof-react | monorepo-ts | 447 | 12 | 0 | 0.7s | 43 PASS, 0 FAIL, 0 FINDING |
| Rails-B (internal) | rails-only | 4,799 | 207 | 0 | 9.3s | 43 PASS, 0 FAIL, 0 FINDING |
| TS-B (internal) | ts-only | 2,357 | 17 | 0 | 17.2s | 43 PASS, 0 FAIL, 0 FINDING |
| excalidraw | monorepo-ts | 629 | 4 | 0 | 7.8s | 42 PASS, 0 FAIL, 1 FINDING |
| forem | rails-with-frontend | 3,514 | 127 | 0 | 4.4s | 44 PASS, 0 FAIL, 0 FINDING |
| gitlabhq | rails-only (legacy sprockets, see Bug E note) | 28,789 | 1,197 | 4 | 35.1s | 44 PASS, 0 FAIL, 0 FINDING |
| mastodon | rails-with-frontend | 3,178 | 146 | 1 | 4.5s | 43 PASS, 0 FAIL, 1 FINDING |
| maybe | rails-with-frontend | 793 | 59 | 1 | 1.3s | 44 PASS, 0 FAIL, 0 FINDING |
| plane | monorepo-ts | 3,581 | 70 | 5 | 16.8s | 42 PASS, 0 FAIL, 1 FINDING |

**Totals: 388 PASS, 0 FAIL, 3 FINDING.** 5 of 9 apps fully clean (0 FINDING).

## Cycle-by-cycle progression

| Cycle | Version | PASS | FAIL | FINDING | Clean apps (0 finding) |
|---|---|---|---|---|---|
| 2 | v0.5.1 | (n/a — bulletproof-react failed bootstrap) | 0 | 12 | 1 |
| 3 | v0.5.3 | 378 | 0 | 13 | 0 |
| 4 | v0.5.4 | 388 | 0 | 3 | **5** |

v0.5.4 closed 10 of 13 cycle-3 findings (77%). Of the 3 remaining, all are the same new bug (below).

## v0.5.4 fixes verified in real-world data

| Fix | Cycle 3 baseline | Cycle 4 status |
|---|---|---|
| Bug F — workspace-prefix strip in TS naming | n/a (not yet shipped) | Plane workspace paths correctly stripped; 0/12 generic on bulletproof-react |
| Bug F — 13 new TS prior entries | plane 12/70 generic | plane 5/70 generic (-58%); bulletproof-react 6/12 → 0/12 |
| `_Phase 2C:` placeholder removed | rendered in every summary | rules.json contents render with per-tool counts |
| `## deprecated\n_(none)_` placeholder | rendered in every summary | only shown when section has content |
| Runner `pause_session` PASS tagging | 9 spurious FINDING in cycle 3 | 0 in cycle 4 |
| Runner `language_hint.secondary_detected` | `secondary=None` in cycle 3 | correct field name now |
| Runner `archetypes` stale across phase_5 re-bootstrap | wrong sample-arch picked | reads fresh archetypes.json |

Plus the same v0.5.3 fixes (workspace bootstrap, instrumentation, legacy sprockets hybrid detect, Bug A typed envelopes) all continue working.

## New bug surfaced in cycle 4

### Bug H — `_resolve_repo_root_by_id` returns wrong workspace for monorepos (3-app confirmation, MEDIUM severity)

**Severity:** Medium. Silent misroute. No data loss, but downstream tools (`get_canonical_excerpt`, `get_drift_status`) operate on the wrong workspace and return "archetype not found" / wrong drift score.

**Confirmed on:** excalidraw, mastodon, plane.

**Symptom:** After `bootstrap_repo(<plane_root>)`, the `repos` table in `index.db` carries 18 rows — one for the plane root and one per workspace (`apps/admin`, `apps/live`, `apps/space`, `apps/web`, `packages/*` × 13). All 18 rows share the same `repo_id` because `_compute_repo_id(workspace_dir)` derives the id from the git remote, which is identical for all workspaces and the root.

`_resolve_repo_root_by_id(repo_id)` (the no-hint version called by `get_canonical_excerpt`, `get_drift_status`, and other consumers) picks the freshest row by `last_seen_at`. Workspace rows are inserted AFTER the root row, so the alphabetically-last workspace (`packages/utils` for plane) wins.

Then `get_canonical_excerpt(repo_id, "action")`:
1. resolves repo_root to `plane/packages/utils` (wrong)
2. loads profile from `plane/packages/utils/.chameleon/` (doesn't exist — workspace has no profile)
3. `load_profile_dir` returns an empty/stub profile, so `"action" not in known_archetypes` is True
4. Returns `{"status": "failed", "error": "archetype not found"}` — misleading

**Why v0.5.1's composite PK fix isn't enough:** v0.5.1 added `(repo_id, repo_root)` PK + a `repo_root_hint` parameter to `get_repo` / `resolve_repo_root`. That made the composite key work, but the wrapper `_resolve_repo_root_by_id(repo_id)` (without hint) is what consumers actually call, and it still picks "newest wins" against 18 candidates.

**Root cause:** `tools.py:1941` — in the workspace-bootstrap completion path, the code inserts a row for each detected workspace with `_compute_repo_id(ws_root)`. Since `_compute_repo_id` hashes the git remote URL, all rows get the same `repo_id`. The intent was per-workspace metadata, but the side effect is poisoning the no-hint lookup.

**Suggested fix (v0.5.5 Bug H):**

Option A — preferred — fix `_resolve_repo_root_by_id` to prefer the root when multiple workspaces share the same `repo_id`:
- Query all rows for the `repo_id`.
- If only one row, return it.
- If multiple rows, return the row whose `repo_root` is an **ancestor of (or equal to) every other row's `repo_root`** — i.e., the actual repo root, not a workspace.
- Falls back to "freshest" only when no clear ancestor exists.

Option B — alternative — don't insert workspace rows in `tools.py:1941` when they collapse to the same `repo_id`. The metadata at the workspace level isn't useful if the consumers can't disambiguate. This is more surgical but loses the per-workspace `archetype_count` / `files_indexed` data that v0.5.3 Bug D's instrumentation surfaces.

Recommend Option A — keeps Bug D's workspace-level instrumentation working, just makes the no-hint resolution sane.

## Coverage observations

- **gitlabhq jumped to 1,197 archetypes / 28,789 files** after the 200K cap bump in v0.5.3. v0.5.4 kept that 92x gain.
- **All 8 successful bootstraps under 35s wall-time.** Slowest: gitlabhq at 35.1s (down from 34.5s in cycle 3 despite the new TS prior table doing more work).
- **Naming quality is now ~97%+ on every repo.** 5 of 9 apps hit 0 generic names. The remaining 12 generic names across 4 apps are bespoke domain dirs (`emoji-icon-picker/`, `editor/`, deep `features/<feature>/api/` nests) that genuinely don't fit any generic prior table.

## v0.5.5 patch plan (proposed)

1. **Bug H** (Medium, 3-app confirmation): fix `_resolve_repo_root_by_id` to prefer the ancestor row when multiple workspaces share the same `repo_id`. ~30 LOC in `index_db.py`, regression test added. Verify-before reproducer: after `bootstrap_repo(plane_root)`, `_resolve_repo_root_by_id(repo_id)` returns `plane_root`, not `plane/packages/utils`.

## Deferred to v0.6

Same 11 findings carried since cycle 1. The 5 remaining plane / mastodon generics are bespoke domain dirs and don't warrant a generic prior table entry.

## Notes on testing methodology

- Pre-flight: every `.chameleon/` dir in `/Users/crisn/Documents/Projects/Testing Apps/*` was wiped before launch. `~/.local/share/chameleon/` (plugin data dir) was also wiped. Each app's run got an isolated `CHAMELEON_PLUGIN_DATA` tmpdir.
- Two internal repos (anonymized as `Rails-B` and `TS-B`) tested with real paths; their per-app reports stay local-only (gitignored).
- Cycle 4 wall-time per app: 0.7s (bulletproof-react) to 35.1s (gitlabhq); median ~7s.
