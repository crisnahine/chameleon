---
name: chameleon-status
description: Use when the user explicitly invokes /chameleon-status to view profile state, drift indicators, and trust state for the current repo
---

# /chameleon-status

Surface the current state of chameleon for the active repo. The user-facing observability surface — like `git status` for chameleon profiles.

## What it reports

What's plumbed today (read straight from `.chameleon/` and `drift.db`):

1. **Profile summary** — language, archetype count + names, schema version, generation, last bootstrap timestamp.
2. **Trust state** — `trusted | untrusted | stale | n/a`, with the trusting user and grant timestamp when present.
3. **Drift** — `days_since_refresh`, `observed_drift_score`, and a `recommended_action` string from `get_drift_status`.
4. **Language hint** — when a Rails-with-frontend (or TS-with-Ruby-sidecar) was detected, name the secondary tree so the user can bootstrap it separately.
5. **Version coherence** (v0.5.7) — call `daemon_status` to get `running_version` (also returns `alive`, `pid`, `socket`, `uptime_s`, `last_request_at`). If the running version differs from the installed plugin version, surface "Running v<X>, installed v<Y> — restart Claude Code to pick up the new MCP."
6. **v0.6.0 config** — when `.chameleon/config.json` exists, surface the active settings so the user can see at a glance whether their v0.6.0 features are on:
   - `canonical_ref` (and whether materialize is currently working, via `branch_pinning_enabled`)
   - `auto_refresh.enabled` + `drift_threshold` + `max_age_hours`
   - `trust.auto_preserve_when`
   - `auto_rename`
   Read these via `chameleon-mcp::doctor` — its `config_json` check returns the parsed config. When the file is malformed, doctor surfaces a clear error and v0.6.0 features silently fall back to v0.5.x behavior; show the error prominently so the user can fix the typo.

## The flow

1. Call `chameleon-mcp::detect_repo(<file-path>)` to get the current repo_id and trust_state.
2. Read `.chameleon/profile.json` and `archetypes.json` to enumerate archetypes (or call `get_pattern_context` if more convenient).
3. Call `chameleon-mcp::get_drift_status(repo)` for `days_since_refresh` / `observed_drift_score` / `recommended_action`.
4. Format the result for the terminal.

## Output format

```
chameleon profile: <repo-name>
  Language:        typescript
  Schema:          7 (engine min: 0.5.7)
  Last bootstrap:  47 days ago
  Trust state:     trusted (granted 2026-05-10 by <user>)
  Drift score:     0.12 (recommended: refresh)
  Archetypes:      17
    - react-component (89 files): src/components/base
    - query (12 files): src/queries
    - utility (7 files): src/utils
    [...]
```

When `trust_state` is `untrusted` or `stale`, the line should be highlighted and accompanied by the corresponding remediation (`/chameleon-trust` for untrusted, `/chameleon-refresh` for stale).

## Slash command surface

- `/chameleon-status` — default summary
- `/cham-status` — short alias

## Out of scope

The earlier draft of this skill listed several telemetry surfaces ("value attribution", "MCP error rate", "p99 hook latency") that aren't implemented yet — there is no `value_attrib.db`, no MCP-error tracking surface, no hook-latency surface. Those have moved to **future work**:

- `--health` flag with operator-grade SLO compliance dashboard (Round 5 SRE recommendation).
- `--diff` flag with profile-poisoning scan + semantic diff for PR review.
- `--json` flag for CI integration (machine-readable output).
- Value attribution: edits matching archetype over last N sessions, deviations flagged, corrections via /chameleon-teach.

Until those land, do not invent values for them; print only what the MCP data layer actually returns.
