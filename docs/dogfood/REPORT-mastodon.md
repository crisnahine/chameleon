# Dogfood Report — mastodon/mastodon (v0.5.0 + v0.5.1-prep)

- **Target:** `/Users/crisn/Documents/Projects/Testing Apps/mastodon`
- **Engine:** chameleon v0.5.0 with v0.5.1 fix `bc6f901` applied
- **Languages on disk:** Rails (~3,179 `.rb`) + TS/JS (~650 `.ts(x)`) + Node sidecar in `streaming/` (9 files)
- **Pre-existing `.chameleon/`:** none

## TL;DR

**Did the v0.5.1 workspace fix hold?** **YES, unambiguously.** `bootstrap_repo("/.../mastodon")` completed in **639 ms** without raising. `_expand_workspace_globs` correctly skipped the `"."` entry in `package.json`'s `"workspaces": [".", "streaming"]` (the exact payload that produced `IndexError: tuple index out of range` before the fix), and went on to discover `streaming/` as a real sub-workspace.

**Any other mastodon-specific bugs surfaced?** **YES — five new issues. Two are P1.** None prevented the bootstrap from completing, but together they mean **mastodon's profile is essentially empty (0 archetypes)** despite a `"success"` status:

| # | Severity | Headline |
|---|---|---|
| 1 | **P1 (silent miss)** | Rails+JS hybrid → language detection picks TS, ignores 3,179 Ruby files. ActivityPub federation code completely uncovered. |
| 2 | **P1 (consistency)** | Workspace bootstrap reuses the parent's `repo_id` for sub-workspaces sharing a git remote. `streaming/` overwrites the root's `index.db` row, permanently disabling the noop/partial-refresh fast paths. |
| 3 | P2 (recall) | TypeScript clustering on mastodon yields **0 archetypes** + **831 sparse-cluster warnings**. Threshold-5 dense rule is wrong for mastodon's feature-per-folder layout (median cluster size = 1). |
| 4 | P2 (collision) | Idiom slug uses `int(time.time())`; two `teach_profile` calls in the same second collide on the same slug. Reproduced verbatim. |
| 5 | P3 (UX) | `get_drift_status(<path>)` silently accepts a path instead of repo_id and reports a misleading envelope. |

## Section A. Bootstrap

**A1.** No crash. `status: success`, `duration_ms: 639`, `files_processed: 856` (TS-only — 3,179 `.rb` not visited), `archetypes_detected: 0`, `rules_extracted: 1`, `sparse_cluster_warnings: 831`, `bimodal_cluster_warnings: 0`, `workspaces: 1` entry (`streaming/`).

**A2.** `.chameleon/` written cleanly: `archetypes.json/canonicals.json` both empty objects; `rules.json` contains only the typescript block (paths from `tsconfig.json`). No fallback `cluster-<hash>` names because there are 0 archetypes.

**A3.** `detect_repo("app/controllers/api/v1/accounts_controller.rb")` → `repo_id: d4fbbf2f...`, `profile_status: profile_present`, `trust_state: untrusted`. Detection works on a Ruby path even though Ruby was not scanned (detect_repo only walks parents for `.chameleon/`).

## Section F. Refresh — broken

**F16.** Immediate refresh did **NOT** return `noop` — it ran a full bootstrap (`duration_ms: 616`, `files_processed: 856`). Re-runs identical.
**F17.** Edit 1 TS file → still full bootstrap. No partial_refresh.
**F18.** Edit 100 TS files → full bootstrap.
**F19.** `force=True` → full bootstrap.

**Root cause** (traced live): `index.db.repos` row for `repo_id=d4fbbf2f...` got overwritten by streaming's bootstrap (it shows `repo_root=.../streaming`, `files_indexed=9`), so the cardinality match `len(candidates) == cached_files` is permanently false for the root (856 ≠ 9). Verbatim sqlite output:

```
d4fbbf2f...|/Users/crisn/Documents/Projects/Testing Apps/mastodon/streaming|0|9|142
```

## Section J. Mastodon-specific

**J29. ✅ Workspace fix verified.** `profile.json.workspaces` has exactly 1 entry (`streaming/`). The `"."` entry is correctly excluded.

**J30.** `bootstrap_repo("/.../mastodon/streaming")` directly succeeded: `files_processed: 9`, `archetypes_detected: 0`, `duration_ms: 150`, 9 singleton sparse clusters at `(root)`.

**J31.** `get_archetype` on 5 ActivityPub Ruby files (`serializer.rb`, `activity.rb`, `tag_manager.rb`, `activity/announce.rb`, `activity/block.rb`) → **all return `archetype: null`** with `confidence_band: low`. Mastodon's federation code is completely invisible to chameleon. This is the visible end of Issue #1.

## Bugs found

### Issue 1 — Rails+JS hybrid silently scans only TS (P1)

**Repro:** clone mastodon, bootstrap it, check `profile.json.language` → `typescript`; `get_archetype` on any `app/models/*.rb` → `null`.
**Root cause:** `mcp/chameleon_mcp/bootstrap/orchestrator.py:51-63` `_select_extractor` iterates `(TypeScriptExtractor, RubyExtractor)` and returns the first match. Docstring acknowledges the limitation but production behavior is a silent miss.
**Suggested fix:** detect `Gemfile + config/application.rb + app/javascript/` (Rails-with-frontend) and either (a) pick Ruby and treat `app/javascript/` as a sub-workspace, or (b) emit a `language_hint` warning in the bootstrap report.

### Issue 2 — Workspace repo_id collision in index.db (P1)

**Repro:** see verbatim sqlite output in Section F above.
**Root cause:** `_compute_repo_id` hashes the canonicalized git remote URL. Root + sub-workspaces share remote → same repo_id → `repos` table (PRIMARY KEY repo_id) lets them overwrite each other.
**Suggested fix:** repo_id for sub-workspaces should be `sha256(remote_url + '\0' + workspace_relpath)`.

### Issue 3 — 0 archetypes from 856 files (P2 tuning)

856 TS → 831 sparse clusters. Mastodon's feature-per-folder layout (typically 2-4 files per folder) defeats the dense-5 threshold. Recommend lowering the threshold to 3 when median cluster size ≤ 2.

### Issue 4 — Idiom slug collision in the same second (P2)

Verbatim repro from I26 + I27 written in the same second:

```
### idiom-2026-05-11-1778489943   (zero-width payload)
### idiom-2026-05-11-1778489943   (prompt-injection payload)
```

Both rows written under the same id. Renderer accepts both (markdown ignores duplicates), but any tool keying idioms by slug (slug-uniqueness check, renames API, daemon idiom-diff) will get the wrong row half the time.
**Suggested fix:** `idiom-YYYY-MM-DD-HHMMSS-<3 hex>` or seed from UUID4; detect collisions in `idioms.md` before write.

### Issue 5 — `get_drift_status(<path>)` silently misroutes (P3, UX)

The tool is typed `(repo: str)` and docstring says "repo by repo_id" but accepts any string. Passing a path produces a confusing-but-not-failing envelope.
**Suggested fix:** detect path-shaped input, auto-resolve via `detect_repo`, or return a clear error.

## Performance

| Run | self-reported `duration_ms` | wall |
|---|---|---|
| 1 | 639 | 1.62s |
| 2 | 617 | 2.01s |
| 3 | 646 | 1.62s |

Streaming standalone: 150 ms / 9 files. No memory or disk anomalies. `.chameleon/` total size: ~2 KB.

## Adversarial defense summary

| Vector | Outcome | Verdict |
|---|---|---|
| Prompt-injection idiom | Stored verbatim, persists in summary.md until trust revoked | Mitigated by trust boundary; recommend heuristic warning |
| Zero-width chars | Silently stripped | Good hygiene |
| `bootstrap_repo` traversal | Rejected at "not a directory" | OK |
| `detect_repo` traversal | Resolved to enclosing parent | Minor info-disclosure (P3) |

## v0.5.1 recommendations

1. **Ship `bc6f901` as v0.5.1.** Fix is correct, minimal, verified.
2. **Land Issue #2 before v0.5.1** — same code area; otherwise v0.5.1's monorepo bootstrap silently corrupts the parent's index row and disables refresh fast paths.
3. **Bundle Issue #4 into v0.5.1** — trivial fix, real data-integrity bug.
4. **File Issue #1 as the v0.5.2 headline.** Rails+JS hybrids are a major customer segment; without it Phase 6 calibration can't legitimately claim mastodon coverage (only 1/4 of mastodon's code is scanned).
5. **Defer Issue #3 (clustering tuning) to v0.6.**
6. **Defer Issue #5 (`get_drift_status` UX) to v0.6.**
