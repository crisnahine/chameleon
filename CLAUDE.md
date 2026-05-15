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

### Run the full test suite

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py
```

This runs all five core suites in four randomized orders to verify order-independence.

### Run individual test files

```bash
cd mcp
PYTHONPATH=.:../tests .venv/bin/python ../tests/smoke_test.py
PYTHONPATH=.:../tests .venv/bin/python ../tests/comprehensive_test.py
PYTHONPATH=.:../tests .venv/bin/python ../tests/mcp_protocol_test.py
PYTHONPATH=.:../tests .venv/bin/python ../tests/claude_code_acceptance_test.py    # ~$0.20 — real claude
```

### Test parameterization

Real-Claude-Code tests need a TypeScript repo and/or a Ruby on Rails repo to point at. Set in `.env` (gitignored):

```
CHAMELEON_TEST_TS_REPO=/abs/path/to/typescript/repo
CHAMELEON_TEST_RUBY_REPO=/abs/path/to/rails/repo
```

When unset, the tests skip gracefully.

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
- `CHAMELEON_TEST_TS_REPO` / `CHAMELEON_TEST_RUBY_REPO` — test repo paths (see `.env.example`)
- `CLAUDE_PLUGIN_ROOT` — set by Claude Code; path to installed plugin
- `TMPDIR` — honored for HMAC exec log location

## Commit conventions

- Subject line: 50 chars max, imperative mood ("Add X" not "Added X").
- Body: explain *why*, not *what* (the diff shows what).
- Reference issues / ADRs where relevant.
