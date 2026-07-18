# Chameleon — Full-Matrix Real-Usage Test Campaign

**Status:** IN PROGRESS — Phase 1 (inventory + environment)
**Branch:** `plugin-testing-fixes`
**Baseline commit:** `27fd8d3` (Release v4.4.15) — clean tree, no uncommitted changes
**Plugin version under test:** 4.4.15
**Started:** 2026-07-18

This file is the source of truth for the campaign. On any interruption, compaction, or
restart: re-read this file and `git log --oneline`, then resume from the first cell that is
not marked PASS with evidence. Never restart the campaign. Never mark a cell PASS without
fresh evidence captured in this run.

---

## 1. Environment

Verified on 2026-07-18, host `darwin 25.5.0` (arm64, Apple Silicon).

| Component | Version | Status | Notes |
|---|---|---|---|
| macOS / kernel | Darwin 25.5.0 | OK | arm64 |
| git | 2.50.1 (Apple Git-155) | OK | |
| node | v22.22.3 | OK | TypeScript/JS extractor host (`ts_dump.mjs`) |
| npm | 10.9.8 | OK | |
| pnpm | 11.12.0 | OK | |
| npx | present | OK | scaffolding |
| ruby | 3.4.9 (+PRISM) | OK | Ruby extractor host (`prism_dump.rb`) |
| prism gem | 1.9.0 / 1.5.2 (default) / 1.4.0 | OK | Ruby AST parser |
| bundler | 4.0.15 | OK | |
| rails | 8.1.3 | OK | Rails cell scaffolding |
| python3 (system) | 3.9.6 | BELOW FLOOR | `/usr/bin/python3`; below chameleon's >=3.11 floor — exercises the `_resolve-python.sh` ladder rather than blocking |
| plugin venv python | 3.13.13 | OK | `plugin/mcp/.venv/bin/python` — rung 1 of the interpreter ladder |
| uv / uvx | 0.11.7 | OK | MCP server launcher (`.mcp.json` uses `uvx`) |
| uv-managed pythons | 3.11.15, 3.12.13, 3.13.13 | OK | rung 2/3 of the ladder |
| sqlite3 | 3.51.0 | OK | `drift.db`, `index.db` |
| claude CLI | 2.1.214 | OK | real slash-command / hook invocation |
| network | reachable (npm registry 200) | OK | dependency scaffolding only; chameleon itself is offline |
| free disk | 95 GiB | OK | |

**BLOCKED items:** none. Every runtime and toolchain the plugin requires is installed and
working.

**Environment note (not a blocker, but under test):** the system `python3` is 3.9.6, below
the plugin's documented `>=3.11` floor. `plugin/hooks/_resolve-python.sh` exists precisely
for this and resolves via a validated ladder (bundled venv -> version-named binaries -> `uv
run` -> probed `python3`). This host therefore exercises the ladder's rung-1 path for real,
which is a feature of this environment, not a gap. Ladder behaviour is itself a matrix item.

### Test workspace

Fresh repos are built under `~/Documents/Projects/chameleon-fullmatrix-qa/`, one per
language/framework cell, each `git init`-ed with a real initial commit and **no** `.chameleon/`
directory at start. The developer's own `~/.local/share/chameleon/` is never used as the
campaign's data dir; each run points `CHAMELEON_PLUGIN_DATA` at a campaign-scoped directory
so the host profile store stays untouched.

---

## 2. Inventory

_(populated in Phase 1 — see section 3 for the matrix)_

---

## 3. Coverage Matrix

_(populated in Phase 1)_

---

## 4. Gaps & Effectiveness Log

_(running log; every issue found, its impact, and its resolution)_

---

## 5. Fix Log

_(one entry per fix cycle: issue, cell, root cause, red evidence, green evidence, commit)_
