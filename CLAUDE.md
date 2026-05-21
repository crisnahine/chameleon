# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What this repo is

`chameleon` — a Claude Code plugin that auto-derives codebase conventions and injects archetype-aware guidance per-edit. Supports TypeScript and Ruby on Rails repos.

See [docs/architecture.md](./docs/architecture.md) for the full design.

## Project structure

```
chameleon/
├── .claude-plugin/    plugin.json + marketplace.json (Claude Code plugin manifest)
├── .cursor-plugin/    Cursor harness manifest
├── .codex-plugin/     Codex CLI manifest
├── hooks/             SessionStart, PreToolUse, PostToolUse, UserPromptSubmit hooks
├── skills/            using-chameleon (auto) + 7 user-invocable slash commands
├── mcp/               chameleon-mcp Python server (FastMCP, stdio transport)
├── scripts/           ts_dump.mjs, prism_dump.rb, bump-version.sh, merge driver
├── tests/             unit, integration, acceptance, real-Claude-Code tests
└── docs/              architecture.md (design) + install.md
```

## Conventions

- **Language**: all code, comments, docs, error messages, and commit messages MUST be in English.
- **Versioning**: `bump-version.sh <new-version>` keeps the seven manifest files in sync.
- **Locks**: `package-lock.json` and `mcp/uv.lock` are committed.
- **Atomic transactions**: profile writes use `.chameleon/.tmp/<txn-id>/COMMITTED` sentinel + flock-serialized rename.

## Working on this codebase

### Run the journey harness

```bash
mcp/.venv/bin/python -m tests.journey.runner               # full run (~$25, ~65 min)
mcp/.venv/bin/python -m tests.journey.runner --list        # list acts
mcp/.venv/bin/python -m tests.journey.runner --dry-run     # preflight only, no Claude spawn
mcp/.venv/bin/python -m tests.journey.runner --max-budget-usd 20
```

The journey harness drives real `claude -p` subprocesses against committed seed fixtures. Run before each release. All state is isolated to a per-run dir under `tests/journey/results/`; the developer's own `~/.local/share/chameleon/` is never touched.

### Run unit tests for the harness library

```bash
PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/ -v
```

These verify the harness library itself (context, checkpoints, expect, fixtures setup). They do NOT test chameleon; that's the journey runner's job.

### Test a hook locally

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"/abs/path/to/file.ts"},"session_id":"test"}' \
  | CLAUDE_PLUGIN_ROOT="$(pwd)" hooks/preflight-and-advise
```

### Run the MCP server directly

```bash
cd mcp
.venv/bin/python -m chameleon_mcp.server
```

### Inspect drift.db

```bash
sqlite3 ~/.local/share/chameleon/<repo_id>/drift.db
sqlite> SELECT * FROM edit_observations ORDER BY observed_at DESC LIMIT 10;
```

## Environment variables

- `CHAMELEON_DISABLE=1` — disable plugin globally for this session
- `CHAMELEON_PLUGIN_DATA` — override `~/.local/share/chameleon/` (tests only)
- `CHAMELEON_HMAC_KEY_PATH` — override the HMAC key location (tests only)
- `CLAUDE_PLUGIN_ROOT` — set by Claude Code; path to installed plugin
- `TMPDIR` — honored for HMAC exec log location

## Commit conventions

- Subject line: 50 chars max, imperative mood ("Add X" not "Added X").
- Body: explain *why*, not *what* (the diff shows what).
- Reference issues / ADRs where relevant.
