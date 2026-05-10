---
name: chameleon-status
description: Use when the user explicitly invokes /chameleon-status to view profile state, drift indicators, value attribution, and plugin health
---

# /chameleon-status

Surface the current state of chameleon for the active repo. The user-facing observability surface — like `git status` for chameleon profiles.

## What it reports

1. **Profile summary**
   - Archetype count + names
   - Last refresh date (from `profile.json#created_at`)
   - Trust state (per-user)
   - Schema version
2. **Drift indicators**
   - `days_since_refresh`
   - Observed confidence trend (Phase 4: from `value_attrib.db`)
   - Recommended action (refresh? teach?)
3. **Value attribution** (Phase 4)
   - Edits matching archetype (last 30 sessions)
   - Deviations flagged
   - Corrections via `/chameleon-teach`
4. **Plugin health** (Phase 4)
   - Recent MCP error rate
   - Hook latency p99
   - Fail-open rate

## The flow

1. Read `.chameleon/profile.json` and friends via `load_profile_dir()` (Phase 1C double-fstat pattern).
2. Read `drift.db` for `days_since_refresh` and confidence trend (Phase 4).
3. Read `value_attrib.db` for edit attribution (Phase 4).
4. Format output for user terminal display.

## Output format

```
chameleon profile: <repo-name>
  Schema version:    4 (engine min: 0.1.0)
  Last refresh:      47 days ago
  Trust state:       trusted (granted 2026-05-10 by <user>)
  Archetypes:        7
    - cluster-011440424e706e13 (9 files): components/base
    - cluster-06395b2e0fffcad7 (5 files): admin/users queries
    [...]

Recent activity:
  Last 30 sessions:  142 edits matched archetype, 11 deviations flagged
  Corrections:       3 idioms added via /chameleon-teach
  MCP errors:        0 (last 24h)
  p99 hook latency:  890ms (last 24h)
```

## Out of scope (Phase 4-end)

- `--health` flag with operator-grade SLO compliance dashboard (per Round 5 SRE recommendation)
- `--diff` flag with profile-poisoning scan + semantic diff for PR review
- `--json` flag for CI integration (machine-readable output)

## Slash command surface

- `/chameleon-status` — default summary
- `/cham-status` — short alias
