# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What this repo is

`chameleon` — a Claude Code plugin that auto-derives codebase conventions and injects archetype-aware guidance per-edit (conformance), and answers codebase-comprehension queries like search_codebase, describe_codebase, get_callees, and get_blast_radius (comprehension). Supports TypeScript/JavaScript, Ruby, and Python as first-class languages — framework-agnostic by default (it learns each repo's own conventions, so any framework works), with deeper framework-aware guidance where conventions are strong: Rails for Ruby, Django, DRF, Flask, and FastAPI for Python, and Next.js and NestJS for TypeScript/JavaScript.

See [docs/architecture.md](./docs/architecture.md) for the full design.

## Project structure

```
chameleon/
├── .claude-plugin/    plugin.json + marketplace.json (Claude Code plugin manifest)
├── hooks/             session-start, preflight-and-advise, posttool-recorder,
│                      posttool-verify, callout-detector, stop-backstop
│                      (+ _resolve-python.sh, run-hook.cmd, hooks.json)
├── skills/            using-chameleon (auto) + 14 user-invocable slash commands
├── mcp/               chameleon-mcp Python server (FastMCP, stdio transport)
├── scripts/           ts_dump.mjs, prism_dump.rb, bump-version.sh, merge driver
├── bin/               chameleon-statusline.sh (status line, <100ms budget)
├── tests/             unit/ + journey/ + effectiveness/ harnesses + qa_*.py real-repo batteries
└── docs/              architecture.md (design) + install.md + language-support-matrix.md + parity-progress.md + qa-team.md
```

The user-invocable commands: `init`, `refresh`, `status`, `teach`, `auto-idiom`, `trust`, `disable`, `pause-15m`, `doctor`, `journey`, `pr-review`, `receiving-code-review`, `explain`, `deep-work` (all invoked as `/chameleon-*`).

## Conventions

- **Language**: all code, comments, docs, error messages, and commit messages MUST be in English.
- **Versioning**: `bump-version.sh <new-version>` keeps six manifest files in sync (see `.version-bump.json`).
- **Locks**: `mcp/package-lock.json` and `mcp/uv.lock` are committed.
- **Atomic transactions**: profile writes use `.chameleon/.tmp/<txn-id>/COMMITTED` sentinel + flock-serialized rename.
- **Production-ref derivation**: when `.chameleon/config.json` has `production_ref` (auto-locked at init/refresh for origin-backed repos, or set explicitly), bootstrap/refresh analyze a materialized worktree of that ref instead of the checkout; refresh noop/staleness is tip-SHA-keyed. Refresh (manual + auto) first runs a default-ON, non-interactive `git fetch origin <branch>` so the tip it resolves is the latest production, not the user's last fetch (kill: `CHAMELEON_FETCH_PRODUCTION_REF=0` / config `auto_refresh.fetch_production_ref=false`; auto-suppressed under CI; fails open). Local-only repos (most test fixtures) never auto-lock — they keep working-tree derivation. An explicit `"production_ref": null` is a durable opt-out (migration never re-locks over it). See `mcp/chameleon_mcp/production_ref.py` and docs/architecture.md "Production-ref derivation".

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

# Ruby repo battery (any Ruby repo; Rails repos exercise the Rails-aware layer)
CHAMELEON_TEST_RUBY_REPO=/abs/path/to/ruby-repo \
  PYTHONPATH=. mcp/.venv/bin/python tests/qa_ruby.py

# Python repo battery (Django / Flask / FastAPI)
CHAMELEON_TEST_PYTHON_REPO=/abs/path/to/python-repo \
  PYTHONPATH=. mcp/.venv/bin/python tests/qa_python.py

# Cross-cutting (security, caching, contract invariants) — needs BOTH repos
CHAMELEON_TEST_TS_REPO=... CHAMELEON_TEST_RUBY_REPO=... \
  PYTHONPATH=. mcp/.venv/bin/python tests/qa_crosscutting.py

# Drive 10 simulated tasks through the real PreToolUse + PostToolUse hooks
CHAMELEON_TEST_TS_REPO=... CHAMELEON_TEST_RUBY_REPO=... \
  PYTHONPATH=. mcp/.venv/bin/python tests/qa_hook_simulation.py
```

### Effectiveness eval (A/B: does chameleon improve agent output?)

Spawns real `claude -p` sessions — local only, never CI. See
[.claude/rules/effectiveness-eval.md](./.claude/rules/effectiveness-eval.md)
(lazy-loads when touching `tests/effectiveness/`) for tiers, budgets, toggle
experiments, and results layout. Unit tests for the eval itself DO run in CI:
`PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/effectiveness/tests/ -v`

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

## Testing discipline (MANDATORY before claiming "tested" or "done")

When asked to test or validate, or before declaring any work complete, run the FULL matrix below — not a happy-path spot check. "I tested everything" is only true after the **depth** pass actually ran. Real bugs live in degraded/edge/interaction state, not the happy path (a tool returning once proves it works on good input, not bad/stale state).

Shortcut: `/qa` runs this whole matrix and reports. Use it instead of re-explaining.

### Pass 1 — breadth
Exercise each MCP tool + hook once on a healthy profile: the `qa_*.py` batteries + a from-zero bootstrap against the real repos.

### Pass 2 — depth (the pass that finds real bugs)
- **Stale/damaged artifacts**: for each generated artifact (`archetypes`/`canonicals`/`rules`/`conventions.json`, `principles.md`, `profile.summary.md`) test missing + corrupt + stale, then `/chameleon-refresh` — is it repaired (not noop-preserved)?
- **Damaged-profile read tools**: corrupt each artifact, call every read tool — crash or fail-open?
- **Boundary inputs**: empty / huge / binary / unicode / null-byte files; non-existent / traversal / null-byte paths.
- **Hook robustness**: every hook against malformed payloads (empty stdin, garbage, null fields, huge, missing keys) — must fail-open (exit 0, valid JSON, no crash).
- **Lifecycle chains**: bootstrap -> trust -> teach -> refresh -> rename -> merge; verify `idioms.md` survives + artifacts stay consistent.
- **Trust states**: every tool under untrusted / stale / trusted.

### Pass 3 — full surface (beyond tools + hooks)
- **Slash-command / skill flows**: drive each `/chameleon-*` end-to-end (init, refresh, status, teach, auto-idiom, trust, disable, pause-15m, doctor, pr-review, receiving-code-review, explain) — the skill logic + output, not just the underlying tool.
- **Statusline**: `bin/chameleon-statusline.sh` with a sample payload — correct format, within the <100ms budget, respects `CHAMELEON_DISABLE`.
- **MCP stdio server**: `python -m chameleon_mcp.server` — call a tool over the real stdio transport, not just in-process.
- **Daemon**: `daemon.py` / `daemon_client.py` — startup, socket, idle-timeout self-exit, `daemon_status`.
- **Merge driver**: `scripts/chameleon-merge-driver.sh` on a real `.chameleon` git merge conflict (3-way).
- **Hot path**: `tests/bench_hot_path.py` — `get_pattern_context` p50/p99 within budget.
- **Schema migrations**: load an old-schema-version profile — migrate or reject cleanly (don't crash).

### Out of scope for `/qa` (use the right method, don't fake it)
- **Journey harness** (real `claude -p` editing): `/chameleon-journey` or `tests/journey/runner.py` — ~$33, ~65 min. Run before a release, not on every `/qa`. Ask before spending.
- **Visual statusline rendering** in the live terminal, and **cross-platform** (Linux / other Python versions): CI matrix + manual, not `/qa`.
- Say plainly when one of these was NOT run.

### Rules
- Prefer the free real-repo test (9 bootstrapped repos in `~/Documents/Projects/Testing Apps/`) over the ~$33 journey harness.
- Fix CHAMELEON, not the test, when a test surfaces a gap. Tests enforce the spec.
- Verify load-bearing claims yourself before relaying them.
- After any fix: 2-3 rounds of review (read-only `Explore` agents, or back up first — review subagents can mutate the working tree), THEN run the matrix.
- Always bump the version (`scripts/bump-version.sh <ver>`); the plugin cache is version-keyed. Run `ruff check` AND `ruff format --check` from `mcp/` over `chameleon_mcp/ ../tests/unit/` before pushing.
- At each release: refresh the real-PR outcome aggregate (`tests/measure_pr_review_outcomes.py`) and the published eval artifacts + `baselines.json` per `tests/effectiveness/results-published/README.md`.
- Do NOT claim "I tested everything" until Pass 2 ran.

## Environment variables

The flags every session needs; the FULL operator reference (every kill switch,
model ladder, tuning threshold, and its design contract) lives in
[.claude/rules/environment-variables.md](./.claude/rules/environment-variables.md),
which lazy-loads when you touch `mcp/`, `hooks/`, `bin/`, or `tests/`.

- `CHAMELEON_DISABLE=1` — disable plugin globally for this session
- `CHAMELEON_VERIFY=0` — disable PostToolUse archetype verification (default ON)
- `CHAMELEON_ENFORCE=0` — kill all blocking enforcement (advisory-only per-edit hooks, silent Stop); a blocked edit is overridable inline with `// chameleon-ignore <rule>` (`#` form in Ruby/Python)
- `CHAMELEON_ALLOW_TMP_REPO=1` — allow repos under `/tmp`/`$TMPDIR` to resolve (set for test suites and CI fixture jobs; never in normal use)
- `CHAMELEON_PLUGIN_DATA` / `CHAMELEON_HMAC_KEY_PATH` / `CHAMELEON_PLUGIN_ROOT` — test-only overrides for data dir, HMAC key, plugin root
- `CHAMELEON_<THRESHOLD>` — operator override for any tuning threshold in `mcp/chameleon_mcp/_thresholds.py` (see its `DEFAULTS`)
- `CLAUDE_PLUGIN_ROOT` — set by Claude Code; path to installed plugin

Design contract for all flags: offline features ship default-ON with a kill
switch; anything that executes repo code or touches the network stays opt-in.

## Commit conventions

- Subject line: imperative mood ("Add X" not "Added X"), aim for concise.
- Body: explain *why*, not *what* (the diff shows what).
- Reference issues / ADRs where relevant.
