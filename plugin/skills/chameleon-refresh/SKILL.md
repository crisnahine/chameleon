---
name: chameleon-refresh
description: Use when the user explicitly invokes /chameleon-refresh to re-analyze the current repo and update the chameleon profile after drift
---

# /chameleon-refresh

Re-analyze the current repo, detect drift, update `.chameleon/profile.json`. When <= 10% of files changed, uses partial refresh via cached `file_clusters` in `index.db`; only changed files re-parsed. Falls back to full re-bootstrap when change ratio exceeds 10%.

## When to use

- The user explicitly invokes `/chameleon-refresh`
- `using-chameleon` primer surfaces `days_since_refresh > 90` and the user asks for a refresh
- Reviewer feedback indicates the profile is stale (suggesting many recent edits diverge from the canonical)
- Material changes to the codebase: significant refactors, framework upgrades, archetype boundaries shifted

## When NOT to use

- The first time a repo has chameleon — that's `/chameleon-init`. Refresh requires an existing profile.
- For capturing missed patterns — that's `/chameleon-teach`. Refresh re-derives auto-detectable dimensions only.

## The flow

1. Confirm `.chameleon/profile.json` exists. If missing, suggest `/chameleon-init`.
2. Call `chameleon-mcp::chameleon_lifecycle(action="refresh_repo", params={"repo": <absolute path>})`.
3. The tool acquires an OS-level flock on `.chameleon/.refresh.lock` (per-PID + start timestamp; concurrent invocations fail with stale-lock detection at 1 hour).
4. **Production-pinned repos** (`production_ref` in `.chameleon/config.json`,
   set at init or migrated below): staleness is the locked ref's TIP SHA,
   not working-tree changes. Before resolving the tip, refresh runs ONE
   bounded, non-interactive `git fetch origin <branch>` by default, so the
   tip it sees is the genuinely-latest production — you do NOT need to
   checkout, pull, or even fetch the production branch yourself. Tip
   unchanged → `noop` (your feature-branch edits don't affect a
   production-pinned profile). Tip moved → full re-derive from a
   materialization of the new production tree.

   The fetch outcome rides out in the envelope's `production_ref_fetch`
   block (`{attempted, outcome, reason}`). Report it in one line:
   - `outcome: "ok"` → "Fetched origin/<branch>; refreshed from the latest
     production tip." (when tip moved) or "...tip unchanged, already current."
   - ANY non-ok outcome with a non-empty `reason` (`auth` / `timeout` /
     `no_network` / `no_remote_ref` / `concurrent` / `unknown`) → "Could not
     fetch origin/<branch> (<reason>); refreshed from the last-fetched ref,
     which may be behind production." Relay the reason verbatim (it names the
     manual `git fetch` to run where useful). Never stay silent on a non-ok
     fetch — a recurring failure means the profile is deriving from stale code.
   - block absent → the fetch was gated off (no lock, kill switch
     `CHAMELEON_FETCH_PRODUCTION_REF=0`, `auto_refresh.fetch_production_ref:
     false`, CI, local-only repo, or a recent-failure backoff); say nothing.
   **Old-profile migration**: a profile without the lock gets one
   automatically here when detection is clean and origin-backed
   (origin default branch, or an origin branch named production/prod);
   the refresh envelope's `production_ref` block reports it. An explicit
   `"production_ref": null` in config.json is the opt-out — migration
   never re-locks over it. If the block
   instead carries `conflict: true` or a non-origin candidate, surface the
   note and offer to set the lock: re-run with the user's answer via
   `chameleon-mcp::chameleon_lifecycle(action="bootstrap_repo", params={"path": "<repo root>", "production_ref": ..., "force": true})`.
5. **Unpinned repos**: re-discovers files (with same exclusions as init), re-parses changed files via cached `file_clusters` in `index.db`.
6. Re-clusters from current signatures. New archetypes may appear; old ones may disappear.
7. Atomic profile commit — old profile remains valid until `COMMITTED` sentinel is rolled in.
8. Reports diff: archetypes added/removed, canonicals updated, file count delta.

## Trust + material change

By default trust is one-time and persists across a refresh: even a material change to the hashed profile artifacts keeps the profile `trusted`, so the user is **not** re-prompted to re-run `/chameleon-trust`. Poisoned idioms/principles/conventions prose is screened out at render time instead.

Only under `CHAMELEON_TRUST_REVALIDATE=1` does a material refresh transition trust to `"stale"` and require a re-run of `/chameleon-trust` (the legacy behavior).

## Common failure modes

| Failure | Action |
|---|---|
| `status: "failed"` with an error naming "another /chameleon-refresh is in progress" | A concurrent `/chameleon-refresh` holds the lock (the error carries the PID + timestamp). There is no `lock_held` status — key on `data.status == "failed"` and match the error text. Wait or kill that PID. |
| `unsupported_schema_version` | The committed profile's `schema_version` is newer than this engine supports — it was written by a newer chameleon (a teammate upgraded first). Refresh refuses to re-derive, even under `force`, so it never downgrades and destroys the newer profile. Tell the user to upgrade chameleon, or delete `.chameleon/` to rebuild deliberately if they intend to stay on the older version. |
| `failed_too_many_files` | Repo grew past 200k file ceiling since init. Ask user for `paths_glob`. |
| `noop` | No files changed since the last refresh (unpinned), or the locked production tip is unchanged (pinned). Nothing to do. |
| `production_ref` unresolvable (note in the envelope's `production_ref` block) | The locked branch no longer resolves (deleted branch, renamed remote, shallow clone). Derivation fell back to the working tree. Suggest `git fetch origin <branch>`, fixing the name in `.chameleon/config.json`, or removing the key; `/chameleon-doctor` has a dedicated check. |
| `partial_refresh` | <= 10% of files changed; partial refresh was used (faster). |
| large archetype churn (>50%) | The refresh envelope carries the diff under `data.archetype_diff` = `{added, removed, renamed, unchanged_count}` (there is no `archetypes_changed` field). When `len(added) + len(removed)` is large relative to `unchanged_count`, surface as warning: "X archetypes added, Y removed; review profile.summary.md before /chameleon-trust." This is unusual — probably a major refactor or the previous profile was wrong. |

## Incremental refresh

When <= 10% of files changed since the last run, `refresh_repo` uses partial refresh: only changed files are re-parsed and re-clustered via `index.db`'s `file_clusters` table. Above 10% change ratio (or when `force=True`), a full re-bootstrap runs.

## After success: offer /chameleon-auto-idiom when there are no idioms

Refresh preserves `idioms.md` verbatim, but many profiles never got idioms in
the first place. After reporting the refresh diff, call
`chameleon-mcp::chameleon_telemetry(action="get_idiom_coverage", params={"repo": <abs-repo-path>})` and branch on
`data.status` FIRST, then `data.existing_idioms.active_count`:

- `status == "untrusted"` → do NOT make the offer. An untrusted profile
  withholds content and reports `active_count: 0` even when it has idioms, so
  the offer would be a false "no idioms yet" claim. Suggest `/chameleon-trust`
  instead if trust is stale.
- `status == "ok"` and `active_count == 0` → offer: "This profile has no team
  idioms yet. Run /chameleon-auto-idiom to derive them from repo evidence
  (append-only; it never overwrites idioms)?" If the user accepts, invoke the
  `chameleon-auto-idiom` skill.
- `status == "ok"` and `active_count > 0` → say nothing; don't nag a profile
  that already has idioms.

## Surface dropped archetypes

A full re-bootstrap (the >10%-churn path) returns the same diagnostic warning
lists as `/chameleon-init` — `sparse_cluster_warnings`,
`bimodal_cluster_warnings`, `workspace_skipped_warnings`,
`workspace_glob_warnings`, `nested_profile_warnings`. When any is non-empty, name
it in one short line (the pattern + reason, e.g. "2 `*.guard.ts` files fell below
the cluster floor — no archetype covers them; `/chameleon-teach` to capture the
role"). Skip empty categories; keep it terse. Advisory only.
