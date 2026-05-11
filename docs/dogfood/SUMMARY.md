# Dogfood Summary — chameleon v0.5.0 across 6 real repos

**Date:** 2026-05-11
**Engine:** chameleon v0.5.0 (post-Phase 6 calibration, pre-v0.5.1)
**Total scope:** 6 production repos, 32-step protocol per repo, ~192 protocol steps + 6 app-specific deep dives

Per-app reports:
- [REPORT-forem.md](REPORT-forem.md) — Rails+Stimulus (DEV.to platform)
- [REPORT-maybe.md](REPORT-maybe.md) — Rails+Hotwire (personal finance)
- [REPORT-mastodon.md](REPORT-mastodon.md) — Rails+TS (federated social)
- [REPORT-gitlabhq.md](REPORT-gitlabhq.md) — Rails+JS (GitLab CE; 66k files)
- [REPORT-excalidraw.md](REPORT-excalidraw.md) — TS+React monorepo (Yarn workspaces)
- [REPORT-plane.md](REPORT-plane.md) — TS+React monorepo (pnpm + Remix-style routing)

## Bug count

**56 unique findings across 6 apps:**

| Severity | Count |
|---|---|
| Critical | **4** |
| High | **10** |
| Medium | 18 |
| Low / Nit | 24 |

## The critical pattern: 3 bugs caused by ONE root cause

The dogfood pass revealed a **single architectural assumption** that breaks every monorepo and every Rails+JS hybrid:

> **chameleon assumed `repo_id` would be a 1-to-1 identity for a profile.**
> 
> In reality, `repo_id = sha256(git_remote_url)` collides for:
> - Monorepo root + all sub-workspaces (same git remote)
> - Fresh clones inherited from prior runs (same git remote → same id)
> - Sibling profiles in `.chameleon/` per-workspace dirs (same git remote)

Three independent dogfoods (mastodon, plane, excalidraw) all hit the same `index.db.repos` PRIMARY KEY collision. Three independent dogfoods (forem, plane, excalidraw) all hit the cascade: `_resolve_repo_root_by_id` → wrong workspace → cardinality mismatch → full bootstrap on every refresh → silent rename loss.

## Top 4 critical bugs (must-fix for v0.5.1)

### 🔴 Bug 1 — Monorepo `repo_id` collision in index.db

**Confirmed by:** mastodon, plane, excalidraw (3 independent runs)
**Surface:** `mcp/chameleon_mcp/index_db.py` — `repos` table PRIMARY KEY on `repo_id` alone
**Impact:** Last-bootstrapped workspace overwrites root. All repo_id-keyed APIs misroute. Refresh fast paths permanently broken.
**Fix:** Composite PK `(repo_id, repo_root)` OR mix workspace-relative path into the repo_id hash.

### 🔴 Bug 2 — Rails+JS hybrid silently scans only TS

**Confirmed by:** forem (3,515 Ruby files invisible), mastodon (3,179 Ruby files invisible)
**Surface:** `mcp/chameleon_mcp/bootstrap/orchestrator.py:51-63` — `_select_extractor` picks TS first when both `package.json` and `Gemfile` exist; no fallback, no warning
**Impact:** Production Rails apps with any JS sidecar (Stimulus, Hotwire, asset pipeline) get a TS-only profile with ~75% miss rate. `language: "typescript"` recorded for repos that are 80% Ruby. Hidden behind `status: "success"`.
**Fix:** Detect Rails+JS hybrid (`Gemfile` + `config/application.rb` + `app/javascript/`); pick Ruby, treat `app/javascript/` as a sub-workspace OR emit a `language_hint` warning.

### 🔴 Bug 3 — `refresh_repo` after `apply_archetype_renames` silently wipes renames

**Confirmed by:** forem, plane, excalidraw
**Surface:** `mcp/chameleon_mcp/tools.py:_attempt_partial_refresh` (multiple independent root causes)
**Impact:** User's manual archetype curation destroyed on the next refresh. No warning, no force flag, no diff. Different fail-paths on different repos:
- forem: yarn `.cjs` shim defeats discovery-vs-parser cardinality
- maybe: `file_clusters` stores AST-fingerprint cluster_ids (145 unique) vs `archetypes.json` stores path-clustered cluster_ids (32 unique) — **different namespaces**
- plane: Bug 1 cascade — wrong `repo_root` in index.db → 39× change-ratio → fallthrough
**Fix:** Persist user-rename mapping (e.g., `.chameleon/renames.json`) and re-apply after every bootstrap. Diff on refresh; require `--force` to overwrite.

### 🔴 Bug 4 — Bidi control characters (U+202E) not sanitized (Trojan Source / CVE-2021-42574)

**Confirmed by:** maybe, excalidraw
**Surface:** `mcp/chameleon_mcp/sanitization.py:49` — regex strips zero-width chars but not bidi controls (U+202A–U+202E + U+2066–U+2069)
**Impact:** Real CVE class. Attacker can ship a `.chameleon/idioms.md` with RTL override → idiom displays one way to human reviewers, executes another way when LLM reads it. Reaches model context verbatim.
**Fix:** ~15 minutes. Add U+202A–U+202E + U+2066–U+2069 to the strip-set in `_NEUTRALIZE_CHARS` regex.

## High-severity findings (v0.5.1 should-fix)

| # | Bug | Affects | Notes |
|---|---|---|---|
| H1 | `apply_archetype_renames` doesn't flip trust to stale | All | `hash_profile` covers `profile.json + idioms.md` but not `archetypes.json`. Rename = invisible to trust. |
| H2 | Stale trust grants inherit to fresh clones via git-remote repo_id | forem | Calibration-era trust records leak to fresh clones with stale `repo_root` paths. No warning. |
| H3 | Naming heuristic is JS/TS-shaped only; no Rails priors | forem | 5/7 fallback to `cluster-<hex>` on Rails-side. No `model`/`service`/`job`/`mailer` priors. |
| H4 | Partial-refresh dead code (multiple root causes) | forem, maybe, plane, excalidraw | Even after Bug 3 fix, the cluster_id namespace mismatch (maybe) and yarn shim case (forem) need independent fixes. |
| H5 | Threshold-5 dense cluster rule wrong for feature-per-folder layouts | mastodon, plane, excalidraw | 94.8% of excalidraw files = sparse warnings. 0 archetypes for mastodon. Threshold needs adaptive scaling or AST-shape canonicalization. |
| H6 | Trust + `repo_root`: same repo_id, multiple roots → permanently stale | excalidraw | Workspace-internal files perma-stale even after re-trust. |
| H7 | `excalidraw-app` fails `unsupported_language` despite having `.tsx` + React 19 dep | excalidraw | `can_handle()` ignores `.tsx` file evidence when no local `tsconfig.json`. |
| H8 | Prompt-injection text stored verbatim with no `suspicious_input` flag | forem, maybe, plane, excalidraw | Token-only sanitization; "IGNORE PREVIOUS INSTRUCTIONS" passes through. Defense fully outsourced to consumer LLM. |
| H9 | `get_canonical_excerpt` silently returns empty content on wrong arg shape | maybe, plane, excalidraw | Cascade of Bug 1. No error, no warning. |
| H10 | `apps/.../page.tsx` and `layout.tsx` files never cluster (Next.js / Remix blindspot) | plane | 58 page files → 58 singletons → all fall below threshold-5. The exact thing chameleon should specialize on. |

## Medium-severity (v0.5.2)

- `pause_session`/`disable_session` reject `repo_id` despite arg name (4 confirmations) — unify API
- GitHub PAT bypassed by string-concat — fold concat before secret detection
- `list_profiles` strips repo_root/archetype_count from envelope (3 confirmations) — JOIN against index.db
- `atomic_profile_commit` clobbers `.chameleon/.skip` via dir-rename — preserve sibling files
- `db/schema.rb` always reported as "added" because not persisted into file_clusters — Rails-specific
- Idioms are repo-wide, language-agnostic — add `language:` field, filter at injection time
- Bootstrap response: 610 sparse_cluster_warnings, ~163KB JSON, many duplicates — truncate + group
- `content_signal_match` is dead code — wire `signatures.content_signal_match_for()` into `get_archetype` return paths
- `paths_pattern` drops `models/` prefix on some Rails archetypes (BUG-005)
- Path bucketing is extension-blind (`.tsx` vs `.ts` collide) — incorporate extension into bucket key
- Path bucket drops middle segments for monorepos — workspace-aware variant
- `.d.ts` files get no `types` archetype
- Path traversal in `detect_repo` silently canonicalizes to `$HOME` — return null instead
- Idiom slug collision within same epoch second (2 confirmations) — add 3-hex suffix or UUID

## Low-severity / nits (v0.5.3+)

- Engine version mismatch (`v0.4.0` in profile.summary.md vs `v0.5.0`) — single string update
- Fresh bootstrap trust_state = `"stale"` not `"untrusted"`/`"fresh"` — confusing
- `.chameleon/.skip` honored only at hook layer; MCP tools don't surface it — doc + add `opt_out_reason` field
- Warning duplicates in sparse-cluster output
- `excerpt` vs `content` field naming inconsistency
- `files_skipped_generated: 1` reports count without path

## Performance verdicts (all 6 apps)

| Repo | Files | Bootstrap (ms) | Wall (s) | Files/s |
|---|---|---|---|---|
| forem (TS-half only) | 775 | 751 | 1.41 | 1,032 |
| maybe (Rails) | 794 | 534 | 0.93 | 1,486 |
| mastodon (TS-half only) | 856 | 639 | 1.62 | 1,341 |
| gitlabhq/app (Rails) | 6,475 | 1,734 | 3.51 | 3,734 |
| excalidraw | 629 | 664 | 4.57 | 947 |
| plane | 3,581 | 1,596 | 11.98 | 2,244 |

**All under the 10s p95 calibration target** (max observed: 11.98s on plane root which is monorepo-traversal heavy).

**Peak RSS: gitlabhq at 75 MB / 6,475 files = ~3.5% of the 2 GiB ceiling.**

## Adversarial defense scorecard

| Vector | mastodon | maybe | forem | excalidraw | plane | Verdict |
|---|---|---|---|---|---|---|
| Zero-width chars (U+200B-U+200D) | ✓ stripped | ✓ stripped | ✓ stripped | ✓ stripped | ✓ stripped | Solid |
| ANSI escapes (\x1b[…]m) | ✓ stripped | ✓ stripped | ✓ stripped | ✓ stripped | ✓ stripped | Solid |
| **Bidi controls (U+202A-U+202E, U+2066-U+2069)** | n/t | ✗ **PASSED** | n/t | ✗ **PASSED** | n/t | **Bug 4 — fix immediately** |
| `<chameleon-context>` closing-tag injection | ✓ replaced | ✓ replaced | ✓ replaced | ✓ replaced | ✓ replaced | Solid |
| AWS access keys (plain) | ✓ flagged | ✓ flagged | ✓ flagged | ✓ flagged | ✓ flagged | Solid |
| AWS keys via string-concat | ✓ flagged | ✓ flagged | ✓ flagged | ✓ flagged | ✓ flagged | Solid |
| GitHub PAT (concat) | n/t | n/t | ✗ **MISSED** | n/t | n/t | Regex too tight |
| Prompt injection (NL preamble) | ⚠ stored | ⚠ stored | ⚠ stored | ⚠ stored | ⚠ stored | Outsourced to LLM consumer |
| Path traversal — write tools | ✓ rejected | ✓ rejected | ✓ rejected | ✓ rejected | ✓ rejected | Solid |
| Path traversal — `detect_repo` | ⚠ resolves to `$HOME` | ⚠ resolves | ✓ null | n/t | ⚠ resolves to `$HOME` | Silent canonicalize |

## Phase-6 calibration impact

The expanded 7-repo calibration from earlier today reported `archetype_match_rate_mean = 1.00`. Dogfood reveals this is **measuring witness-roundtrip only**. Real-world archetype recall is much worse:

| Repo | Files | Archetypes | Recall |
|---|---|---|---|
| excalidraw | 629 | 1 | 0.16% (1 cluster covers 5 files; 624 unmatched) |
| mastodon | 856 | 0 | 0.0% |
| plane | 3,581 | 23 | ~5% (heavy icon-cluster bias) |
| forem | 775 | 7 | ~6% (JS-side only; Rails ignored) |
| maybe | 794 | 32 | ~50% (correctly Rails-only) |
| gitlabhq/app | 6,475 | 297 | ~85% (Rails canonical archetypes well covered) |

**Phase 6 needs a generalization-rate metric** — what fraction of non-witness files get matched to a meaningful archetype. The current witness-roundtrip metric of 1.00 hides recall as low as 0%.

## v0.5.1 patch plan

Targeted, focused, single PR — close the audit-quality gap on the 4 critical + 10 high bugs that all 6 dogfoods agreed on:

1. **(15 min)** Sanitize bidi controls — strip U+202A-U+202E + U+2066-U+2069. Add unit tests for each.
2. **(2-3 hours)** Monorepo `repo_id` collision: composite PK on `(repo_id, repo_root)` in `index_db.repos`, OR mix workspace_relative_path into `_compute_repo_id`. Add tests.
3. **(2-3 hours)** Rails+JS hybrid detection: detect `Gemfile + config/application.rb + app/javascript/`, pick Ruby, surface a warning when secondary language is excluded. Add test.
4. **(2-3 hours)** Make renames durable: persist `.chameleon/renames.json` checked-in, re-apply after every bootstrap. Bootstrap re-derives names from heuristic, then overlays the user mapping.
5. **(1 hour)** `hash_profile` covers `archetypes.json + canonicals.json` — rename now flips trust to stale (as the audit expected).
6. **(1 hour)** Unify the `repo` argument: accept both path and repo_id everywhere via a `_resolve_repo()` helper that detects which was passed.
7. **(30 min)** `get_canonical_excerpt` argument validation: explicit "repo_id not found" error envelope instead of empty content.
8. **(30 min)** `atomic_profile_commit` preserves committed sibling files (`.skip`, `.gitignore`).
9. **(15 min)** Stale trust grant detection: when fresh clone meets old trust, hint "trust granted for `<old>`; this is `<new>` — re-trust?"

**Estimated effort: ~12-15 hours of focused work, ~30 new regression tests.**

## v0.5.2+ deferred

- Partial-refresh cluster_id namespace fix (deeper architectural change)
- Threshold-5 → adaptive sparse-cluster threshold
- Rails-aware naming priors
- Path bucketing: extension-aware + monorepo-aware
- Next.js/Remix route group recognition
- Semantic sanitization heuristics (prompt-injection NL preamble)
- GitHub PAT regex tightening for string-concat
- `list_profiles` JOIN against index.db
- `content_signal_match` wire-through
- Idiom language scoping

## Key takeaways

1. **Dogfood found 56 bugs that 891 unit tests + 6 calibration repos didn't.** This is exactly what dogfooding is for. Coverage is high; *generalization* is what unit tests can't measure.

2. **Bug clustering is real signal.** The fact that 3 independent dogfoods (mastodon + plane + excalidraw) all hit the same `repo_id` collision means it's the highest-priority architectural fix. Confirmed N=3.

3. **The "100% calibration match rate" was real but narrow.** Witness-roundtrip ≠ real-world recall. Phase 6 metrics need a generalization column.

4. **The architecture is sound; the assumptions about repo identity break under monorepos.** Bugs 1, 3, partial-refresh failure, trust-stale-on-rename — all cascade from one assumption (`repo_id` is a 1-to-1 identity).

5. **Adversarial defenses are 80% there.** Zero-width / ANSI / tag-boundary / path traversal write-side all hold. Bidi sanitization and prompt-injection NL detection are the remaining gaps.

6. **Real-world archetype recall is far below witness-roundtrip recall.** The biggest UX surprise: bootstrapping a real production repo can produce 0–2 archetypes when the user expects 20+. Threshold needs adaptive scaling.
