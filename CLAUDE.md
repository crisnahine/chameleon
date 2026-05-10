# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`chameleon` — a Claude Code plugin that auto-derives codebase conventions and injects archetype-aware guidance per-edit. Built primarily for Empire Flippers' api (Ruby on Rails) and client (TypeScript) repos as the dogfood test case.

The architecture is fully designed (5 review rounds, 27 reviewer perspectives, ~16,000 words). See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full specification.

Currently in Phase 1 (Foundation) of the v1.0 build.

## Project structure

```
chameleon/
├── .claude-plugin/    # plugin.json + marketplace.json (Claude Code plugin manifest)
├── hooks/             # SessionStart, PreToolUse, PostToolUse, UserPromptSubmit hooks
├── skills/            # using-chameleon (foundation) + 5 user skills + 2 admin
├── mcp/               # chameleon-mcp Python server (FastMCP, stdio transport)
├── scripts/           # ts_dump.mjs, bump-version.sh, etc.
├── tests/             # skill-triggering, unit, integration, acceptance
└── docs/chameleon/    # specs, plans, decisions (ADRs), MAINTAINER.md, runbooks
```

## Conventions

- **Language**: all code, comments, docs, error messages, and commit messages MUST be in English. Never mix Bisaya/Tagalog into code or user-facing output.
- **Style**: skill files (`skills/*/SKILL.md`) follow the `superpowers:writing-skills` format conventions
- **Versioning**: `plugin.json` `version` field is the source of truth; `package.json` mirrors it
- **Locks**: `package-lock.json` and `mcp/uv.lock` are committed
- **Atomic transactions**: profile writes use `.chameleon/.tmp/<txn-id>/COMMITTED` sentinel + atomic rename pattern

## Phase status

- [x] Architecture (v5, complete after 5 review rounds + EF dogfood verification)
- [ ] **Phase 1A: Core repo scaffold** ← current
- [ ] Phase 1B: Hooks + skills shells with proper bodies
- [ ] Phase 1C: MCP server scaffold (FastMCP entry, MCP tools stubs)
- [ ] Phase 2: TS extractor + bootstrap engine
- [ ] Phase 3: Skills with eval (RED-GREEN-REFACTOR per skill)
- [ ] Phase 4: Security mitigations (11 items)
- [ ] Phase 5: EF dogfood + Real Problem Evidence transcripts
- [ ] Phase 6: Conformance benchmarking + calibration target evaluation
- [ ] Phase 7: Documentation completion + v1.0 release

## Working on this codebase

### Run tests

```bash
# Unit tests (Python MCP server)
cd mcp && uv run pytest

# Skill-triggering behavioral tests (TBD Phase 3)
tests/skill-triggering/run-all.sh

# Integration tests (TBD Phase 1B+)
tests/integration/run-all.sh

# Acceptance tests (cooperative + adversarial; TBD Phase 3)
tests/acceptance/run.sh
```

### Test a hook locally (TBD Phase 1B)

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"/abs/path/to/file.ts"},"transcript_path":"/path/to/transcript.jsonl","session_id":"test"}' \
  | hooks/preflight-and-advise
```

### Run MCP server (TBD Phase 1C)

```bash
cd mcp
uv run python -m chameleon_mcp.server
```

### Inspect drift.db (TBD Phase 2)

```bash
sqlite3 ${CLAUDE_PLUGIN_DATA}/<repo_id>/drift.db
sqlite> SELECT * FROM files LIMIT 10;
```

## Configuration env vars (TBD Phase 1C+)

- `CHAMELEON_DISABLE=1` — disable plugin globally for this session
- `CHAMELEON_CI_MODE=1` — disable HMAC log + first-run-seen writes (for CI environments)
- `CHAMELEON_PLUGIN_DATA` — override `${CLAUDE_PLUGIN_DATA}` (for devcontainer persistence)
- `CLAUDE_PLUGIN_ROOT` — set by Claude Code; path to installed plugin
- `CLAUDE_PLUGIN_DATA` — set by Claude Code; per-plugin persistent data directory
- `TMPDIR` — honored for HMAC exec log location (per-user on macOS)

## Reviewer rounds

5 rounds complete (27 unique reviewer perspectives). Reports at `docs/chameleon/ROUND-{1..5}-*.md`.

**Review moratorium declared after v5.** Implementation findings replace reviewer findings from this point forward. Don't request another review round; ship and learn from real use.

## Commit conventions

- Subject line: 50 chars max, imperative mood ("Add X" not "Added X")
- Body: explain *why*, not *what* (the diff shows what)
- Reference issues/ADRs where relevant: `Closes #42` / `Per ADR-0003`

## Phase 1 prerequisites still pending

1. **EF stakeholder confirmation** — conversation with EF engineering manager about adoption commitment. Required before Phase 5 dogfood; can be deferred during Phases 1-4.
2. **Risk registry review** — see `ARCHITECTURE.md#risk-registry`. Pre-commit fall-back-to-v0.5 plan if Phase 1 takes 12+ weeks for 30% scope.

License decision: `UNLICENSED` — proprietary to Empire Flippers, LLC. (Decided 2026-05-10 in v5.)
