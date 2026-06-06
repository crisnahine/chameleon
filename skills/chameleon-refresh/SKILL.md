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
2. Call `chameleon-mcp::refresh_repo(repo=<absolute path>)`.
3. The tool acquires an OS-level flock on `.chameleon/.refresh.lock` (per-PID + start timestamp; concurrent invocations fail with stale-lock detection at 1 hour).
4. Re-discovers files (with same exclusions as init), re-parses changed files via cached `file_clusters` in `index.db`.
5. Re-clusters from current signatures. New archetypes may appear; old ones may disappear.
6. Atomic profile commit — old profile remains valid until `COMMITTED` sentinel is rolled in.
7. Reports diff: archetypes added/removed, canonicals updated, file count delta.

## Trust + material change

If the refresh causes a material change to any of the 9 hashed profile artifacts, trust transitions to `"stale"` and the user must re-run `/chameleon-trust`.

Exception: structurally-identical refreshes (only the generation counter bumped, no archetype/canonical/rules changes) automatically preserve the existing trust grant.

## Common failure modes

| Failure | Action |
|---|---|
| `lock_held` | Another `/chameleon-refresh` is in progress (PID + timestamp shown). Wait or kill that PID. |
| `failed_too_many_files` | Repo grew past 200k file ceiling since init. Ask user for `paths_glob`. |
| `noop` | No files changed since the last refresh. Nothing to do. |
| `partial_refresh` | <= 10% of files changed; partial refresh was used (faster). |
| `archetypes_changed` is large (>50%) | Surface as warning: "X archetypes added, Y removed; review profile.summary.md before /chameleon-trust." This is unusual — probably a major refactor or the previous profile was wrong. |

## Incremental refresh

When <= 10% of files changed since the last run, `refresh_repo` uses partial refresh: only changed files are re-parsed and re-clustered via `index.db`'s `file_clusters` table. Above 10% change ratio (or when `force=True`), a full re-bootstrap runs.

## After success: offer /chameleon-auto-idiom when there are no idioms

Refresh preserves `idioms.md` verbatim, but many profiles never got idioms in
the first place. After reporting the refresh diff, call
`chameleon-mcp::get_idiom_coverage(repo=<abs-repo-path>)` and branch on
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
