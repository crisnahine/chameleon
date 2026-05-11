# Dogfood Report — makeplane/plane (chameleon v0.5.0)

- **Target:** `/Users/crisn/Documents/Projects/Testing Apps/plane`
- **Engine:** chameleon v0.5.0
- **Languages on disk:** TypeScript / React (~3,581 `.ts(x)` files in 17 supported workspaces) + Django Python in `apps/api/` + a `tailwind-config` package and `typescript-config` package not recognized as TS
- **Build system:** pnpm workspaces + turborepo. Note: `apps/web` uses **React Router v7 (Remix-style) with `app/` directory and parenthesized route groups** — *not* Next.js, despite the conventional folder names. There is no `pages/` directory.
- **Pre-existing `.chameleon/`:** none

## TL;DR

**1. How does plane's apps/packages detection compare to excalidraw's npm-workspaces?**
Workspace discovery works correctly: 20 workspace entries (17 success + 3 failures for Python/tailwind/tsconfig). However, **`index.db` cannot represent a workspace tree at all** — every per-workspace bootstrap upserts into `repos(repo_id PRIMARY KEY)` and overwrites the previous row, so the final survivor is the *alphabetically last* sub-workspace (`packages/utils`). This breaks every MCP tool that resolves a `repo_id` back to a root: `get_canonical_excerpt`, partial-refresh fast path, drift, etc. (P1.) **Same root cause as mastodon BUG #2 — confirmed twice.**

**2. Did `"use client"` directive detection work?**
**Pure-function YES, end-to-end NO.** `signatures.content_signal_match_for` correctly returns `"use_client"` for the only `"use client"` file in plane. But `get_archetype` / `get_pattern_context` never call it — `content_signal_match` is hardcoded to `None` in every return branch.

**3. Did naming reflect Next.js / Remix conventions?**
**No.** Of the 23 detected archetypes, 15 are `cluster-<hash>` fallbacks, 5 have generic names (`react-component`, `react-hook`, `service`, `types`), and zero recognize **route**, **page**, **layout**, **server-component**, or **client-component** as concepts. None of the 58 `app/.../page.tsx` files clustered (each ended up in its own singleton).

## Bug summary

| # | Severity | Headline |
|---|---|---|
| 1 | **P1 (lookup)** | Monorepo `repo_id` collision: every workspace bootstrap overwrites the same `index.db` row → `_resolve_repo_root_by_id` returns the alphabetically-last sub-workspace. **Confirmed in mastodon too.** |
| 2 | **P1 (data loss)** | `apply_archetype_renames` survives only until the next `refresh_repo`. Any change to a file restarts a full bootstrap and silently re-clusters; the rename is destroyed without warning. Reproduced 3×. |
| 3 | **P2 (recall)** | Plane's `app/.../page.tsx` and `app/.../layout.tsx` (58 + 44 files) are never grouped into archetypes because `path_pattern_bucket_for` keys clusters by parent dir → 58 singletons → all fail threshold-5. The exact pattern chameleon should specialise on is the one it never sees. |
| 4 | **P2 (dead code)** | `content_signal_match` is plumbed at the signature layer but never returned to callers. `get_archetype`'s 4 return-points hardcode `None`. The implemented detector for `use_client` / `use_server` / shebang / `ts_pragma` is dead code. |
| 5 | P3 (idiom misroute) | `teach_profile(repo_id, ...)` rejects `repo_id` with "expected absolute repo path". Inconsistent API contract. **Third confirmation** (forem, maybe, plane). |
| 6 | P3 (path traversal silent canonicalise) | `detect_repo("/.../plane/../../../etc/passwd")` returns `repo_root=/Users/crisn` with a new `repo_id`. Read-only but surprising. |

## Section F. Refresh — Bug #2 lives here

Live trace of why partial refresh always bails:
```
cached.repo_root = /Users/crisn/Documents/Projects/Testing Apps/plane/packages/utils  ← Bug #1 fallout
prev_state files: 94          (from utils's per-workspace bootstrap)
candidates: 3581              (from root-level discovery)
unchanged=0  modified=0  added=3581  removed=94
denom=94  change_ratio = (3581 + 94) / 94 = 39.10
```
391× over the 0.10 ceiling → `_attempt_partial_refresh` always returns `None` → full bootstrap → rename clobbered.

**Bug #1 causes Bug #2's symptom.**

## Section J. Monorepo + Next.js / Remix

**J29.** `apps/*` vs `packages/*` discovery works — `profile.json.workspace` shows `is_workspace: true, manager: pnpm, workspace_count: 20`. The `"!apps/api"` / `"!apps/proxy"` exclusions in `pnpm-workspace.yaml` were correctly honored.

**J30.** Standalone `bootstrap_repo("/.../plane/apps/web")` → 7 archetypes, 2063 files, **817 ms / 1.72s wallclock**. The 7 archetypes are a *subset* of the root's 23 and noticeably crisper (path patterns like `core/services`, `core/hooks` instead of root's confusing `apps/core/services`). **Strong UX argument for recommending users run chameleon per-workspace on monorepos this size.**

**J31.** Next.js / Remix conventional files — every probe returned `archetype: null, confidence_band: low`:

| File | Archetype | Band |
|---|---|---|
| `apps/web/app/root.tsx` | None | low |
| `apps/web/app/layout.tsx` | None | low |
| `apps/web/app/(home)/page.tsx` | None | low |
| `apps/web/app/(home)/layout.tsx` | None | low |

Live cluster trace of the `apps/web` subprofile:
```
Total clusters: 1953   (apps/web alone)
Page.tsx file count: 58
Page cluster sizes: every page.tsx ended up in a size-1 cluster keyed by ClusterKey(
    path_pattern_bucket='app/issues/(list)', ...)
    path_pattern_bucket='app/analytics/[tabId]', ...)
    path_pattern_bucket='app/browse/[workItem]', ...) ... etc
```
58 page files split into 58 singletons; same for 44 layouts. None survive threshold-5.

**J32.** `"use client"` content_signal_match. Found exactly one `"use client"` file: `apps/web/core/components/navigation/app-rail-root.tsx`.
- `get_pattern_context(<file>)` → `"content_signal_match": null` (Bug #4).
- Direct `content_signal_match_for(content)` → `'use_client'` ✅. The detector exists; not wired to consumers.

## Suggested fixes

**For Bug #1 / #2 (workspace `repo_id` collision):** Make `index.db.repos` PK `(repo_id, repo_root)`, or have `_compute_repo_id` mix workspace-relative path into the hash for sub-workspaces.

**For Bug #3 (Remix/Next routes never cluster):** Specialise `path_pattern_bucket_for` to collapse parenthesized route groups and bracketed dynamic segments when under an `app/` directory.

**For Bug #4 (content_signal_match dead):** In `tools.py:get_archetype`, after `extract_dimensions`, call `content_signal_match_for(content)` and return it.

**For Bug #5 (teach_profile inconsistency):** Either accept `repo_id` everywhere or paths everywhere.

**For Bug #6 (detect_repo silent canonicalise):** When `Path(...).resolve()` lands outside any plausible repo, return `repo_root: None` rather than `$HOME`.

## Performance

| Run | self-reported `duration_ms` | wallclock |
|---|---|---|
| Initial bootstrap (3581 files, 20 workspaces) | 1596 | 11.98s |
| Refresh (full re-bootstrap) | 1381 | 11.28s |
| Refresh after 1 file edit | 1407 | 12.11s |
| Refresh after 78 file edits | 1305 | 10.44s |
| `force=True` refresh | 1314 | 10.62s |
| Standalone `apps/web` bootstrap (2063 files) | 817 | 1.72s |

`.chameleon/` size: 40 KB at root + ~240 KB across 17 sub-workspaces.

## Adversarial defense summary

| Vector | Outcome | Verdict |
|---|---|---|
| Prompt-injection idiom (tag literal) | Tag literal scrubbed; NL preamble persists | ⚠️ Partial |
| Zero-width-space tokenisation | ZWS stripped before write | ✅ |
| Path traversal in `teach_profile` / `trust_profile` | Rejected with explicit error | ✅ |
| Path traversal in `detect_repo` | Canonicalised to `$HOME` silently | ⚠️ Surprising |
| Fake secret in lint payload | Flagged with `severity: error` | ✅ |
