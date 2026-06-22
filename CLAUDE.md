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
├── skills/            using-chameleon (auto) + 13 user-invocable slash commands
├── mcp/               chameleon-mcp Python server (FastMCP, stdio transport)
├── scripts/           ts_dump.mjs, prism_dump.rb, bump-version.sh, merge driver
├── bin/               chameleon-statusline.sh (status line, <100ms budget)
├── tests/             unit/ + journey/ harness + qa_*.py real-repo batteries
└── docs/              architecture.md (design) + install.md
```

The user-invocable commands: `init`, `refresh`, `status`, `teach`, `auto-idiom`, `trust`, `disable`, `pause-15m`, `doctor`, `journey`, `pr-review`, `receiving-code-review`, `explain` (all invoked as `/chameleon-*`).

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

### Effectiveness eval (A/B: does chameleon improve agent output?)

Spawns real `claude -p` sessions — local only, never CI. Tier ci (~$3-5) runs
8 tasks on committed fixtures; tier full (~$25-45) needs the
`CHAMELEON_TEST_*_REPO` env vars and asks before spending.

```bash
# List tasks / preflight without spawning
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner --list
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner --dry-run

# Tier-ci A/B (off vs shadow), budget-capped
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner \
  --tier ci --arms off,shadow --max-budget-usd 8

# Feature-level toggle experiment (paired arm from shadow)
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner \
  --tier ci --arms off,shadow --toggle judge_crossfile_facts

# Unit tests for the eval itself (these DO run in CI)
PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/effectiveness/tests/ -v
```

Results land in `tests/effectiveness/results/effectiveness_<ts>/` (gitignored):
`run.json`, `run.md` (scoreboard + baseline deltas + 20% regression banner),
`transcripts/`, `diffs/`, `worktrees/`. `baselines.json` is committed and
updated manually at release time only. See `tests/effectiveness/README.md`.

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
- Do NOT claim "I tested everything" until Pass 2 ran.

## Environment variables

- `CHAMELEON_DISABLE=1` — disable plugin globally for this session
- `CHAMELEON_VERIFY=0` — disable PostToolUse archetype verification (default ON)
- `CHAMELEON_ENFORCE=0` — kill switch for all blocking enforcement (PreToolUse deny, PostToolUse block, Stop backstop). Forces advisory-only regardless of `enforcement.mode`. Blocking otherwise follows `.chameleon/config.json` `enforcement.mode`: `off` = advisory only, `shadow` = log would-have-blocked but never block (default), `enforce` = real deny/block on calibrated rules. A blocked edit is overridable inline with `// chameleon-ignore <rule>` (`# chameleon-ignore <rule>` in Ruby).
- `CHAMELEON_ALLOW_ESLINT_EVAL=1` — opt into loading JS ESLint configs via Node `require()`/`import()` during bootstrap (default OFF; off uses a static parser that never executes repo code). Enable only for repos you trust.
- `CHAMELEON_ALLOW_DEP_AUDIT=1` - opt into the `dep_audit` MCP tool, which shells the repo's own `npm audit` / `bundler-audit` (default OFF). Off refuses rather than spawning a network process behind your back. This is the only supply-chain check that touches the network and runs tool-time only, never on a hook hot path; it fails open to an "unavailable" result when the binary, manifest, or network is absent. The no-network manifest/lockfile diff checks in the pr-review skill run regardless of this flag.
- `CHAMELEON_ALLOW_TSC=1` — opt into the auto-pass router's `tsc --noEmit` grounding run (default OFF). Executes the repo's own tsc binary, resolved exclusively from `<repo>/node_modules/.bin` (never PATH, never a download), with a hard timeout. Tool-time only, never on a hook hot path. Off (or no root tsconfig.json, or no installed tsc) reads as a recorded "typecheck unavailable" fact — it never blocks auto-pass eligibility on its own.
- `CHAMELEON_ALLOW_TESTS=1` - opt into the auto-pass router's repo-local test run (default OFF). The sibling of `CHAMELEON_ALLOW_TSC`: resolves vitest/jest exclusively from `<repo>/node_modules/.bin` (never PATH, never npx, never a download), with a hard timeout (default 300s). Tool-time only, never on a hook hot path. Off reads as a recorded "tests unavailable" fact; only actual test failures route a change to a human, never the unavailable state.
- `CHAMELEON_JUDGE_ASYNC=1` — opt into the detached post-Stop correctness-judge spawn with next-turn findings delivery (default OFF; POSIX only). Off uses the synchronous per-turn spawn under the existing wall-clock budget.
- `CHAMELEON_JUDGE_MODEL` — model the turn-end correctness judge and duplication/multi-lens reviewers spawn (`claude -p --model <value>`); default `sonnet`. Lower to a cheaper model or raise for a stronger reviewer; affects only the advisory turn-end review spawns, never the hook hot path.
- `CHAMELEON_REVIEW_REFUTER=0` — disable the pr-review / receiving round-3 refuter (the independent `refute_finding` spawn); the skills fall back to inline verification. Default ON.
- `CHAMELEON_REVIEW_FANOUT=0` — disable pr-review large-diff fan-out; review runs single-pass inline. Default ON.
- `CHAMELEON_REFUTER_MODEL` — model for the round-3 refuter spawn (default `sonnet`); same role as `CHAMELEON_JUDGE_MODEL` for the turn-end judge.
- `CHAMELEON_FETCH_PRODUCTION_REF=0` — kill switch for the default-ON production-ref fetch. When a repo has a locked `production_ref`, refresh (manual `/chameleon-refresh` AND the auto-refresh) runs one bounded, non-interactive `git fetch origin <branch>` BEFORE resolving the tip, so derivation sees the genuinely-latest production instead of the user's last fetch. This is the one network path made default-ON (the per-repo flag is `auto_refresh.fetch_production_ref`, default true). It self-suppresses under `CI` (a fresh CI clone must not do an unasked network fetch), never runs on a hook hot path (PreToolUse/PostToolUse/SessionStart stay offline), and fails open to the last-fetched ref with a classified reason surfaced in the refresh envelope + `auto_refresh.log`. Tuning: `CHAMELEON_PRODUCTION_REF_FETCH_TIMEOUT_SECONDS` (default 10; SIGKILLs a stuck transfer) and `CHAMELEON_PRODUCTION_REF_FETCH_BACKOFF_HOURS` (default 6; after an auth/branch-gone failure, retry only this often).
- `CHAMELEON_INTENT_CAPTURE=0` — kill switch for UserPromptSubmit intent capture (default ON). Capture persists only hard-secret-scanned extracted assertion tokens and content digests, never raw prompt prose; the captured tokens force the correctness-judge security/intent lens when they contain checkable constants.
- `CHAMELEON_ATTESTATION=0` — kill switch for the turn-end session attestation record (default ON; local-only writes, no network, no repo-code execution). The attestation is self-signed and raise-only: nothing in it may ever lower scrutiny, it exists to raise gate depth and make post-incident replay honest.
- `CHAMELEON_MAX_CONVENTION_ITEMS` — cap on each repo-size-scaling section of the SessionStart convention block (preferred imports, DSL calls, key-export union); default 60, over-cap shows a "+N more" tail. Raise to surface more, lower to shrink the block. Read at import time.
- `CHAMELEON_MAX_KEY_EXPORTS` — cap on stored key exports per archetype (default 400). Read at import time.
- `CHAMELEON_MAX_SIBLINGS` — cap on the per-edit "Nearby files" sibling listing (default 60). Read at import time.
- `CHAMELEON_NEARBY_SIGNATURES=1` — EXPERIMENTAL, default OFF. Adds a "Nearby collaborator signatures" section to the per-edit Tier-2 block: the real callable signatures (`name(param: type): ret — path:line`) of source files in the edited file's directory, read from the precomputed `symbol_signatures.json` (no live parse; mtime-cached). Gives the model the cross-file CONTRACTS it must call, not just sibling filenames. Bounded (5 files / 8 signatures / 700 chars), fails open. Default-OFF and env-gated pending an effectiveness A/B — its own grounding research ("more retrieval can hurt") means the conformance lift must be measured before it ships on. The default path short-circuits on this env check, so it costs nothing when off.
- `CHAMELEON_COUNTEREXAMPLE=0` — kill switch for the per-edit off-pattern counterexample (default ON). When a team has taught a competing import (`/chameleon-teach-competing-import`, "prefer X over Y") and a real file still uses the discouraged form, the Tier-2 block pairs the canonical witness with that captured off-pattern line as a "do NOT write it this way" directive — the positive/negative contrast the in-context-learning literature favors over a positive example alone. The artifact (`counterexamples.json`, trust-hashed, drop-stale) is built at teach time (a bounded repo scan) and at bootstrap/refresh, never on a hook hot path; the edit-time read is mtime-cached and fails open. It fires only when a genuine taught off-pattern is still present, so a clean archetype injects nothing. Rendered OUTSIDE the imitate-spotlight (a counterexample must not be copied) but still sanitized. No repo-code execution and no network, so it follows the default-ON-with-kill-switch principle; set to `0` to disable. A/B it with `--toggle counterexample` (the paired arm turns it off).
- `CHAMELEON_EXCERPT_CACHE_CAP` — LRU capacity for the canonical-excerpt cache (default 64). Read at import time.
- `CHAMELEON_LINT_DIMENSIONS` — set to `core` to use the coarse lint dimension set instead of the full per-rule set.
- `CHAMELEON_DAEMON_IDLE_TIMEOUT` — seconds the advisor daemon stays alive while idle before self-exiting.
- `CHAMELEON_<THRESHOLD>` — operator override for any tuning threshold in `mcp/chameleon_mcp/_thresholds.py` (e.g. `CHAMELEON_WORKSPACE_FANOUT_CAP`, `CHAMELEON_EDIT_OBS_HARD_CAP`, `CHAMELEON_DRIFT_BANNER_THRESHOLD`); see that module's `DEFAULTS` for the full list and defaults.
- `CHAMELEON_PLUGIN_DATA` — override `~/.local/share/chameleon/` (tests only)
- `CHAMELEON_HMAC_KEY_PATH` — override the HMAC key location (tests only)
- `CHAMELEON_ALLOW_TMP_REPO=1` — opt out of the temp-dir / world-writable repo-root refusal so a repo built under `/tmp` or `$TMPDIR` resolves normally. By default chameleon refuses such roots because a foreign profile planted in a shared-writable dir could inject conventions into the session. This is the explicit per-invocation opt-out (the guard is never auto-disabled by sniffing the test runner). Set it for the test suite and in any CI job that bootstraps fixtures under a temp dir; leave it unset in normal use.
- `CHAMELEON_PLUGIN_ROOT` — override plugin root path resolution (tests only)
- `CHAMELEON_HOOK_ERROR_LOG` — override hook error log path
- `CLAUDE_PLUGIN_ROOT` — set by Claude Code; path to installed plugin
- `TMPDIR` — honored for HMAC exec log location

## Commit conventions

- Subject line: imperative mood ("Add X" not "Added X"), aim for concise.
- Body: explain *why*, not *what* (the diff shows what).
- Reference issues / ADRs where relevant.
