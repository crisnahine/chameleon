# Dogfood Report — excalidraw/excalidraw (chameleon v0.5.0)

- **Target:** `/Users/crisn/Documents/Projects/Testing Apps/excalidraw`
- **Engine:** chameleon v0.5.0
- **Language / framework:** TypeScript + React 19 monorepo (Yarn workspaces, 9 workspaces, 629 source files)
- **Pre-existing `.chameleon/`:** none

## TL;DR

1. **Did monorepo workspace detection work?** Partially. All 9 workspaces discovered; 8 of 9 bootstrapped successfully. **One critical regression:** all workspaces share the same git-remote-derived `repo_id` and `index.db.repos` is keyed by repo_id, so the last workspace bootstrapped (`packages/utils`) overwrites the root entry. **Same root cause as mastodon BUG #2 and plane BUG #1 — three independent confirmations.**

2. **Archetypes detected & naming quality?** Catastrophic yield: **2 archetypes across 629 files** (1 in root, 1 in `packages/excalidraw`). 596 of 629 files (94.8%) ended up as size-1 sparse warnings. 7 of 9 workspace profiles have **zero** archetypes. Names are fallback `cluster-<hex>`.

3. **TS-specific bugs:**
   - `.d.ts` files get no special archetype (`null`)
   - `path_pattern_bucket_for` ignores extension so `Foo.tsx` and `helper.ts` collapse together
   - Monorepo bucket-formula drops middle segments → `packages/{excalidraw,element,math}/components/TTDDialog/X.tsx` all bucket together
   - `excalidraw-app` (real TS+React workspace) fails `unsupported_language` because no workspace-local `tsconfig.json`
   - `refresh_repo` never noops or partial-refreshes; silently reverts manual renames

## A. Bootstrap

**A.1** `bootstrap_repo("/.../excalidraw")` — wall 4571.3 ms, server `duration_ms`=664, **archetypes=1**, files_processed=629, **sparse_cluster_warnings=607** (596 size-1, 8 size-2, 2 size-3, 1 size-4), **workspaces=9**.

Workspace results:

| workspace | status | archetypes | files | ms |
|---|---|---|---|---|
| `examples/with-nextjs` | success | 0 | 5 | 135 |
| `examples/with-script-in-browser` | success | 0 | 7 | 155 |
| `excalidraw-app` | **failed_unsupported_language** | 0 | 0 | 0 |
| `packages/common` | success | 0 | 26 | 158 |
| `packages/element` | success | 0 | 73 | 258 |
| `packages/excalidraw` | success | **1** | 414 | 432 |
| `packages/fractional-indexing` | success | 0 | 2 | 128 |
| `packages/math` | success | 0 | 23 | 148 |
| `packages/utils` | success | 0 | 11 | 154 |

`excalidraw-app` error verbatim: `No TypeScript signals (tsconfig.json / package.json TS deps) and no Ruby signals (Gemfile / *.gemspec) detected`. The workspace contains `App.tsx`, `index.tsx`, `react@19.0.0`, but no local `tsconfig.json`.

## J. TS/Monorepo-specific

**J.31** `get_archetype(repo_id, "/.../packages/excalidraw/global.d.ts")` → `null, confidence=low`. **No `types` archetype** anywhere across the 10 `.d.ts` files. They share buckets with regular `.ts` siblings, but their AST shape (declaration-only) doesn't match — they fail to cluster.

**J.32** `path_pattern_bucket_for("packages/excalidraw/components/Foo.tsx") == "packages/excalidraw/components" == path_pattern_bucket_for("packages/excalidraw/components/helper.ts")`. **Extension not part of bucket key.** JSX/non-JSX must be re-discriminated after AST extraction, but by then the cluster has already been forced together.

## Bug inventory (13 total)

| # | Severity | Surface | Description |
|---|---|---|---|
| 1 | **Critical** | `index.db` / repo_id model | All monorepo workspaces share git-remote-derived `repo_id`. Last-write-wins on the `repos` table overwrites root and earlier workspaces. All repo_id-keyed APIs route to wrong workspace. **3rd confirmation.** |
| 2 | Critical | `lint_file` | Cascade of #1. Always "no ast_query for archetype" for root and most workspaces. `jsx-presence-mismatch` rule unexercisable via public API. |
| 3 | High | Trust / `detect_repo` | Trust hash recorded only at root. Workspace-internal files permanently `stale` via `detect_repo`. |
| 4 | High | `propose_archetype_renames` | Cascade of #1 via repo_id. Suggestions have no TS/React semantic enrichment. |
| 5 | High | `refresh_repo` | Noop / partial-refresh never fires due to cardinality mismatch. Every refresh is a full bootstrap that silently reverts manual renames. |
| 6a | High | `teach_profile` | Stores prompt injection verbatim. No annotation. |
| 6b | Medium | `sanitize_for_chameleon_context` | Doesn't strip U+202D/U+202E bidi overrides. Trojan Source vector reaches `idioms.md`. **2nd confirmation (also maybe).** |
| 7 | Medium | Language detection | `excalidraw-app` has `*.tsx` + `react@19` but reports `failed_unsupported_language` because no local `tsconfig.json` / `typescript` dev-dep. Inherits parent tsconfig — undetected. |
| 8 | Medium | `path_pattern_bucket_for` | Formula `parts[0]/parts[-3]/parts[-2]` for paths ≥5 segments drops middle segments. Files from different monorepo workspaces collide. |
| 9 | Low | `teach_profile` slug | Same-second teach calls produce identical slug. **2nd confirmation (also mastodon).** |
| 10 | Low | API surface | Some tools accept `repo_id`, others reject with "expected absolute repo path". **4th confirmation** (forem, maybe, plane, excalidraw). |
| 11 | Low | Path bucketing — extension blind | `.tsx` and `.ts` in same dir bucket together. |
| 12 | Low | `.d.ts` blindspot | No `types` archetype produced; declaration files fail to cluster. |
| 13 | Subjective | Clustering threshold | Size-5 threshold + strict AST-equality is too rigid: 596/629 files end up as size-1 warnings. **3rd confirmation** (mastodon, plane, excalidraw all hit this). |

## Architecture observations

- Monorepo design assumed each workspace would have a unique `repo_id`. It doesn't. `index.db.repos` needs composite key `(repo_id, workspace_path)` or a separate `workspaces` table.
- Trust + `repo_root`: same repo_id, multiple `repo_root` paths → trust state needs to be per-(repo_id, repo_root) or per-profile-hash.
- `path_pattern_bucket_for` is monorepo-hostile: dropping middle segments collides workspaces. A monorepo-aware variant would key on `(workspace_relative_path)`.
- Size-5 threshold is too rigid for real TS+React repos with organic AST variance.
