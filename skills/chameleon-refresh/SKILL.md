---
name: chameleon-refresh
description: Use when the user explicitly invokes /chameleon-refresh to re-analyze the current repo and update the chameleon profile after drift
---

# /chameleon-refresh

Re-analyze the current repo, detect drift, update `.chameleon/profile.json`. Uses incremental clustering: cached signatures (in `drift.db`) reused for unchanged files; only changed files re-parsed.

## When to use

- The user explicitly invokes `/chameleon-refresh` (or `/cham-refresh`)
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
4. Re-discovers files (with same exclusions as init), re-parses changed files via cached `(path, content_sha256)` lookup in `drift.db`.
5. Re-clusters from current signatures. New archetypes may appear; old ones may disappear.
6. Atomic profile commit — old profile remains valid until `COMMITTED` sentinel is rolled in.
7. Reports diff: archetypes added/removed, canonicals updated, file count delta.

## Trust + material change

If the new profile's `profile.json` SHA-256 differs from the trusted hash, trust is automatically invalidated. The user must re-run `/chameleon-trust`.

This is intentional — you can't safely keep using a stale trust grant after the profile materially changed.

## Common failure modes

| Failure | Action |
|---|---|
| `lock_held` | Another `/chameleon-refresh` is in progress (PID + timestamp shown). Wait or kill that PID. |
| `failed_too_many_files` | Repo grew past 50k file ceiling since init. Ask user for `paths_glob`. |
| `archetypes_changed` is large (>50%) | Surface as warning: "X archetypes added, Y removed; review profile.summary.md before /chameleon-trust." This is unusual — probably a major refactor or the previous profile was wrong. |

## Phase 2D scope

Phase 2D simplification: `refresh_repo` currently re-runs the full bootstrap (no real incremental algorithm yet). The `drift.db` cache is populated but not consulted on refresh. Phase 4-end implements true incremental refresh via `(path, content_sha256)` cache hits.
