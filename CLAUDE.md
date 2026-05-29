# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What this repo is

`chameleon` — a Claude Code plugin that auto-derives codebase conventions and injects archetype-aware guidance per-edit. Supports TypeScript and Ruby on Rails repos.

See [docs/architecture.md](./docs/architecture.md) for the full design.

## Project structure

```
chameleon/
├── .claude-plugin/    plugin.json + marketplace.json (Claude Code plugin manifest)
├── hooks/             session-start, preflight-and-advise, posttool-recorder,
│                      posttool-verify, callout-detector (+ run-hook.cmd, hooks.json)
├── skills/            using-chameleon (auto) + 10 user-invocable slash commands
├── mcp/               chameleon-mcp Python server (FastMCP, stdio transport)
├── scripts/           ts_dump.mjs, prism_dump.rb, bump-version.sh, merge driver
├── bin/               chameleon-statusline.sh (status line, <100ms budget)
├── tests/             unit/ + journey/ harness + qa_*.py real-repo batteries
└── docs/              architecture.md (design) + install.md
```

The user-invocable commands: `init`, `refresh`, `status`, `teach`, `trust`, `disable`, `pause-15m`, `doctor`, `journey`, `pr-review` (all `/chameleon-*` with `/cham-*` aliases).

## Conventions

- **Language**: all code, comments, docs, error messages, and commit messages MUST be in English.
- **Versioning**: `bump-version.sh <new-version>` keeps six manifest files in sync (see `.version-bump.json`).
- **Locks**: `mcp/package-lock.json` and `mcp/uv.lock` are committed.
- **Atomic transactions**: profile writes use `.chameleon/.tmp/<txn-id>/COMMITTED` sentinel + flock-serialized rename.

## Working on this codebase

### Lint and format

Python is linted with ruff (line-length 100, config in `mcp/pyproject.toml`; `E402` and `E501` are intentionally ignored — see the comments there):

```bash
mcp/.venv/bin/ruff check .          # lint
mcp/.venv/bin/ruff format .         # format
```

### Run the journey harness

```bash
mcp/.venv/bin/python -m tests.journey.runner               # full run (~$33, ~65 min)
mcp/.venv/bin/python -m tests.journey.runner --list        # list acts
mcp/.venv/bin/python -m tests.journey.runner --dry-run     # preflight only, no Claude spawn
mcp/.venv/bin/python -m tests.journey.runner --max-budget-usd 20
```

The journey harness drives real `claude -p` subprocesses against committed seed fixtures. Run before each release. All state is isolated to a per-run dir under `tests/journey/results/`; the developer's own `~/.local/share/chameleon/` is never touched.

### Run unit tests for chameleon

```bash
PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ -v
```

These verify chameleon's hook functions (posttool_verify, etc.) with mocked dependencies.

### Run unit tests for the harness library

```bash
PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/ -v
```

These verify the harness library itself (context, checkpoints, expect, fixtures setup). They do NOT test chameleon; that's the journey runner's job.

### QA batteries against a real profiled repo

Faster and free vs the journey harness. These import the MCP tools and call them directly against an already-bootstrapped repo (read-only, never modifies it). Point the env vars at a repo that already has a `.chameleon/` profile:

```bash
# TypeScript repo battery
CHAMELEON_TEST_TS_REPO=/abs/path/to/ts-repo \
  PYTHONPATH=. mcp/.venv/bin/python tests/qa_typescript.py

# Ruby on Rails repo battery
CHAMELEON_TEST_RUBY_REPO=/abs/path/to/rails-repo \
  PYTHONPATH=. mcp/.venv/bin/python tests/qa_ruby.py

# Cross-cutting (security, caching, contract invariants) — needs BOTH repos
CHAMELEON_TEST_TS_REPO=... CHAMELEON_TEST_RUBY_REPO=... \
  PYTHONPATH=. mcp/.venv/bin/python tests/qa_crosscutting.py

# Drive 10 simulated tasks through the real PreToolUse + PostToolUse hooks
CHAMELEON_TEST_TS_REPO=... CHAMELEON_TEST_RUBY_REPO=... \
  PYTHONPATH=. mcp/.venv/bin/python tests/qa_hook_simulation.py
```

### Benchmark the hot path

```bash
PYTHONPATH=. mcp/.venv/bin/python tests/bench_hot_path.py
```

Reports cold/warm p50 and p99 for `get_pattern_context` and its sub-steps (repo detection, profile load, archetype resolve).

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
- `CHAMELEON_VERIFY=0` — disable PostToolUse archetype verification (default ON)
- `CHAMELEON_ENFORCEMENT_MODE=additionalContext` — revert PostToolUse violations to v0.6.x advisory style (default: `updatedToolOutput`)
- `CHAMELEON_PLUGIN_DATA` — override `~/.local/share/chameleon/` (tests only)
- `CHAMELEON_HMAC_KEY_PATH` — override the HMAC key location (tests only)
- `CHAMELEON_HOOK_ERROR_LOG` — override hook error log path
- `CLAUDE_PLUGIN_ROOT` — set by Claude Code; path to installed plugin
- `TMPDIR` — honored for HMAC exec log location

## Commit conventions

- Subject line: imperative mood ("Add X" not "Added X"), aim for concise.
- Body: explain *why*, not *what* (the diff shows what).
- Reference issues / ADRs where relevant.
