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
├── skills/            using-chameleon (auto) + 13 user-invocable slash commands
├── mcp/               chameleon-mcp Python server (FastMCP, stdio transport)
├── scripts/           ts_dump.mjs, prism_dump.rb, bump-version.sh, merge driver
├── bin/               chameleon-statusline.sh (status line, <100ms budget)
├── tests/             unit/ + journey/ + effectiveness/ harnesses + qa_*.py real-repo batteries
└── docs/              architecture.md (design) + install.md + language-support-matrix.md + parity-progress.md + qa-team.md
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
- `CHAMELEON_ENFORCE=0` — kill switch for all blocking enforcement (PreToolUse deny, PostToolUse block, Stop backstop). Disables blocking regardless of `enforcement.mode`. NOT the same as full silence at every hook: PreToolUse still injects its per-edit pattern advisory (it just never denies), but the Stop backstop returns nothing — its turn-end advisories (idiom self-review, cross-file existence, correctness-judge findings) are suppressed along with its block, so ENFORCE=0 is "advisory-only" at the per-edit hooks and fully silent at Stop. For advisory-only turn-end review WITHOUT the block, set `enforcement.mode=shadow` (or `off`) instead, which keeps the Stop advisory pipeline running. Blocking otherwise follows `.chameleon/config.json` `enforcement.mode`: `off` = advisory only, `shadow` = log would-have-blocked but never block, `enforce` = real deny/block on calibrated rules (default). A blocked edit is overridable inline with `// chameleon-ignore <rule>` (`# chameleon-ignore <rule>` in Ruby/Python).
- `CHAMELEON_ALLOW_ESLINT_EVAL=1` — opt into loading JS ESLint configs via Node `require()`/`import()` during bootstrap (default OFF; off uses a static parser that never executes repo code). Enable only for repos you trust.
- `CHAMELEON_ALLOW_DEP_AUDIT=1` - opt into the `dep_audit` MCP tool, which shells the repo's own `npm audit` / `bundler-audit` (default OFF). Off refuses rather than spawning a network process behind your back. This is the only supply-chain check that touches the network and runs tool-time only, never on a hook hot path; it fails open to an "unavailable" result when the binary, manifest, or network is absent. The no-network manifest/lockfile diff checks in the pr-review skill run regardless of this flag.
- `CHAMELEON_ALLOW_TSC=1` — opt into the auto-pass router's `tsc --noEmit` grounding run (default OFF). Executes the repo's own tsc binary, resolved exclusively from `<repo>/node_modules/.bin` (never PATH, never a download), with a hard timeout. Tool-time only, never on a hook hot path. Off (or no root tsconfig.json, or no installed tsc) reads as a recorded "typecheck unavailable" fact — it never blocks auto-pass eligibility on its own.
- `CHAMELEON_ALLOW_TESTS=1` - opt into the auto-pass router's repo-local test run (default OFF). The sibling of `CHAMELEON_ALLOW_TSC`: resolves vitest/jest exclusively from `<repo>/node_modules/.bin` (never PATH, never npx, never a download), with a hard timeout (default 300s). Tool-time only, never on a hook hot path. Off reads as a recorded "tests unavailable" fact; only actual test failures route a change to a human, never the unavailable state.
- `CHAMELEON_JUDGE_ASYNC=1` — opt into the detached post-Stop correctness-judge spawn with next-turn findings delivery (default OFF; POSIX only). Off uses the synchronous per-turn spawn under the existing wall-clock budget.
- `CHAMELEON_JUDGE_MODEL` — model the turn-end correctness judge and duplication/multi-lens reviewers spawn (`claude -p --model <value>`); default `sonnet`. Lower to a cheaper model or raise for a stronger reviewer; affects only the advisory turn-end review spawns, never the hook hot path.
- `CHAMELEON_JUDGE_MODEL_HIGH` — model the correctness judge escalates to on a HIGH-risk route (`risk_high` / intent-forced / security-surface; security and blast-unknown both fold into `risk_high`); default `opus`. Low-risk routes (`risk_elevated` / `first_low_risk`) keep `CHAMELEON_JUDGE_MODEL`. The reviewer model ladder (roadmap #6): the main loop may run a stronger model than the flat reviewer, so the highest-risk turns get a stronger judge. IMPORTANT: the escalation runs ONLY on the DETACHED async path (`CHAMELEON_JUDGE_ASYNC=1`, or the auto-detach on a known bare-auth failure), whose generous fallback budget the slower model needs — the synchronous Stop path is capped by the 55s hook wrapper (45s judge budget, shared with the duplication lens), where a slower model would time out and fail-open to ZERO findings on exactly the high-risk turns the escalation is meant to strengthen. So a sync turn keeps the base model (unchanged, no regression); enable `CHAMELEON_JUDGE_ASYNC=1` to get the escalation. Raise-only and never garbage: an unrecognized model (not an exact `opus`/`sonnet`/`haiku`/`fable` tier token or a `claude-…` id) falls back to the valid base rather than being spawned, because a bad `--model` makes `claude -p` exit nonzero and would fail-open the judge to zero findings — the ladder can only strengthen the reviewer or leave it unchanged, never silently disable it. Kill the whole ladder with `CHAMELEON_JUDGE_TIERING=0` (flattens every route to the base model — today's behavior). A/B the escalation via the effectiveness harness's model-tier arms before locking the default. See `judge_model_for_route` in `mcp/chameleon_mcp/judge.py`.
- `CHAMELEON_JUDGE_TIERING=0` — kill switch for the reviewer model ladder (default ON). Flattens the correctness judge AND the refuter to their base models (`CHAMELEON_JUDGE_MODEL` / `CHAMELEON_REFUTER_MODEL`) on every route/severity, restoring the pre-#6 flat-model behavior.
- `CHAMELEON_REFUTER_MODEL_HIGH` — model the round-3 refuter escalates to for a BLOCK / high / critical-severity finding; default `opus`. Nit/FIX-severity findings keep `CHAMELEON_REFUTER_MODEL`. Same raise-only / never-garbage guard and `CHAMELEON_JUDGE_TIERING=0` kill switch as the judge ladder. See `_refuter_model_for` in `mcp/chameleon_mcp/refuter.py`.
- `CHAMELEON_DUP_MODEL` — model the turn-end duplication confirm spawn uses (`claude -p --model <value>`); default `sonnet` (unchanged from when it rode the judge's sonnet default), now independently tunable. An unrecognized value falls back to `sonnet` rather than fail-opening the spawn.
- `CHAMELEON_REVIEW_REFUTER=0` — disable the pr-review / receiving round-3 refuter (the independent `refute_finding` spawn); the skills fall back to inline verification. Default ON.
- `CHAMELEON_REVIEW_FANOUT=0` — disable pr-review large-diff fan-out; review runs single-pass inline. Default ON.
- `CHAMELEON_REFUTER_MODEL` — model for the round-3 refuter spawn (default `sonnet`); same role as `CHAMELEON_JUDGE_MODEL` for the turn-end judge.
- `CHAMELEON_FETCH_PRODUCTION_REF=0` — kill switch for the default-ON production-ref fetch. When a repo has a locked `production_ref`, refresh (manual `/chameleon-refresh` AND the auto-refresh) runs one bounded, non-interactive `git fetch origin <branch>` BEFORE resolving the tip, so derivation sees the genuinely-latest production instead of the user's last fetch. This is the one network path made default-ON (the per-repo flag is `auto_refresh.fetch_production_ref`, default true). It self-suppresses under `CI` (a fresh CI clone must not do an unasked network fetch), never runs on a hook hot path (PreToolUse/PostToolUse/SessionStart stay offline), and fails open to the last-fetched ref with a classified reason surfaced in the refresh envelope + `auto_refresh.log`. Tuning: `CHAMELEON_PRODUCTION_REF_FETCH_TIMEOUT_SECONDS` (default 10; SIGKILLs a stuck transfer) and `CHAMELEON_PRODUCTION_REF_FETCH_BACKOFF_HOURS` (default 6; after an auth/branch-gone failure, retry only this often).
- `CHAMELEON_INTENT_CAPTURE=0` — kill switch for UserPromptSubmit intent capture (default ON). Capture persists only hard-secret-scanned extracted assertion tokens and content digests, never raw prompt prose; the captured tokens force the correctness-judge security/intent lens when they contain checkable constants.
- `CHAMELEON_ATTESTATION=0` — kill switch for the turn-end session attestation record (default ON; local-only writes, no network, no repo-code execution). The attestation is self-signed and raise-only: nothing in it may ever lower scrutiny, it exists to raise gate depth and make post-incident replay honest.
- `CHAMELEON_FINDING_LEDGER=0` — kill switch for the finding->fix loop (default ON; roadmap #9). Each finding the multi-lens review (`surfaced`) or the synchronous correctness judge (`findings`) emits at Stop is persisted to a new `judge_findings` drift.db table (additive/idempotent DDL, no schema bump) with the reviewed file's 16-hex content digest as an anchor. The NEXT Stop re-checks each open finding BEFORE that turn's gates persist (so this turn's own findings are never immediately re-surfaced): the cited file changed or is gone since review => addressed (dropped from the open set); unchanged => still open. An unaddressed HIGH-severity finding (correctness `confidence` >= 0.7, or a multi-lens finding two lenses independently agreed on — normalized in `_finding_severity`) is re-surfaced exactly ONCE via a `<chameleon-context>` block, then marked `resurfaced` and never nagged again. Off the per-edit hot path (Stop only), fail-open, bounded (`open_judge_findings` limit), rel_path + lens sanitized on the way to the model surface, durable-table age+recency trim like the other drift tables. Async-detached correctness findings keep their existing one-shot `_pending_findings_block` delivery and are out of this pass's ledger scope. See `_ledger_persist` / `_ledger_recheck_and_resurface` in `mcp/chameleon_mcp/hook_helper.py` and `record_judge_finding` in `mcp/chameleon_mcp/drift/observations.py`.
- `CHAMELEON_AUTOPASS_ATTESTATION=0` — kill switch for attestation-gated auto-pass (default ON; roadmap #7). `get_autopass_verdict` reads the recent session attestations, attributes them to the branch diff by FILE OVERLAP (the `governed_files`/`ungoverned_files` of an attestation sharing a path with the diff; repo_id is already the ledger scope), and folds three governance signals in RAISE-ONLY: verification suppressed while the diff was written (`env.verify_off` / a `verify_env_off` skip, never the routine cooldown re-verify), the correctness judge spawn degraded (`degraded_spawn`, never the deliberate low-risk skip), or a chameleon-ignore override on one of the diff's own files. Each adds a soft (→ elevated) needs-human reason via `classify_change`, so an under-governed diff routes to a human on terms a fully-governed one does not. Strictly raise-only per the attestation contract: no match leaves the verdict identical to today (a forged clean record buys nothing), and the router never blocks. Tool-time only (never a hook hot path), fail-open (any read error leaves the coverage all-clear). Scan window: `CHAMELEON_ATTESTATION_MATCH_LIMIT` (default 25 recent attestations). See `session_coverage_from_attestations` in `mcp/chameleon_mcp/autopass.py`.
- `CHAMELEON_TRUST_REVALIDATE=1` — opt back IN to trust re-validation (default OFF). By default trust is ONE-TIME: once a repo is trusted it stays trusted across every later profile change (refresh, re-bootstrap, teach) and never goes "stale", so the user never re-grants. `=1` restores the old behavior where any change to the trust-hashed profile surface flips the grant to `stale` and re-prompts for a fresh `/chameleon-trust`. The staleness predicate is funnelled through `profile_diverged_from_grant` in `profile/trust.py` (read at call time), so all gates (detect_repo, the PreToolUse/PostToolUse/Stop enforcement gates, the statusline) honor it uniformly. Because persistent trust means a post-grant profile edit is never re-reviewed, the prose artifacts (`idioms.md`, `principles.md`) are injection/secret/dangerous-pattern scanned at their READ path (`loader._prose_injection_unsafe`, mtime-cached; the principles.md SessionStart read) and dropped if poisoned — render-site sanitization does NOT neutralize injection prose, so this read-path scan is what closes the staleness-decoupled injection gap. See docs/architecture.md "Trust model".
- `CHAMELEON_MAX_CONVENTION_ITEMS` — cap on each repo-size-scaling section of the SessionStart convention block (preferred imports, DSL calls, key-export union); default 60, over-cap shows a "+N more" tail. Raise to surface more, lower to shrink the block. Read at import time.
- `CHAMELEON_MAX_KEY_EXPORTS` — cap on stored key exports per archetype (default 400). Read at import time.
- `CHAMELEON_MAX_SIBLINGS` — cap on the per-edit "Nearby files" sibling listing (default 60). Read at import time.
- `CHAMELEON_NEARBY_SIGNATURES=0` — kill switch for the per-edit "Nearby collaborator signatures" section (default ON). The section adds the real callable signatures (`name(param: type): ret — path:line`) of source files in the edited file's directory to the Tier-2 block, read from the precomputed `symbol_signatures.json` (no live parse; mtime-cached), so the model sees the cross-file CONTRACTS it must call, not just sibling filenames. Candidates are ranked by call proximity: a sibling the edited file is recorded calling (from the reverse `calls_index.json`) leads, with deterministic name order as the tiebreak and the full order when no call facts exist. Bounded (5 files / 8 signatures / 700 chars; scored set capped by `CHAMELEON_NEARBY_SIG_SCAN_CAP`, default 200), fails open. Pure advisory, no repo-code execution and no network (only cached artifact reads), so it follows the default-on-with-kill-switch principle. A/B it with `--toggle nearby_signatures` (the paired arm sets `=0` to turn it off).
- `CHAMELEON_INBOUND_CALLERS=0` — kill switch for the per-edit "Inbound callers of this file's exports" section (default ON). The counterpart to nearby-signatures: that shows OUTBOUND sibling contracts you might call; this shows INBOUND dependents that break if you change THIS file's exported signatures. On a Tier-2 Edit/Write, it reads the edited file's own exports (`symbol_signatures.json`) and, for each, the recorded call sites from the reverse `calls_index.json` (both mtime-cached, no live parse), rendering e.g. `getUser() <- src/order.ts:3, src/checkout/summary.ts:44 (+3 more)` with a "change a signature -> update these call sites in the same turn" directive — converting chameleon's most-detected defect class (cross-file staleness, caught at turn end by the correctness judge) into pre-edit prevention. Rendered OUTSIDE the imitate-spotlight (a chameleon directive over repo-derived facts; paths/names sanitized at the boundary), next to the counterexample. Bounded (`CHAMELEON_INBOUND_CALLERS_MAX_EXPORTS`=3 exports / `CHAMELEON_INBOUND_CALLERS_MAX_SITES`=5 sites / `CHAMELEON_INBOUND_CALLERS_MAX_CHARS`=600 chars), fires only when real caller edges exist, and carries an honesty note (barrels and dynamic dispatch are invisible to the snapshot, so an empty list is NOT proof it's safe to break). No repo-code execution and no network (cached artifact reads only), so it follows the default-on-with-kill-switch principle; fails open to "". A/B it with `--toggle inbound_callers` (the paired arm sets `=0`). See `_inbound_contracts_section` in `mcp/chameleon_mcp/hook_helper.py`.
- `CHAMELEON_CROSSWS_INDEX=0` — kill switch for the monorepo cross-workspace existence index (WP-C5, default ON). Closes the per-workspace reverse-index blind spot: `build_reverse_index` runs per workspace, so a file in package B importing from package A (a `@scope/a` cross-package edge) is absent from A's own index, and a removed export A's sibling still imports goes unseen. With the flag on, at bootstrap/refresh each workspace captures the cross-package import specifiers its own reverse index DROPS (in-memory on the BootstrapReport, never persisted per-workspace), the coordinator JOIN (in `_amend`'s successor after the workspace loop) resolves each `@scope/pkg` to the sibling workspace's file via a package.json-`name` map + fail-closed name-in-exports confirmation, and the resolved edges are written to a single `cross_reverse_index.json` in the PLUGIN DATA DIR (`~/.local/share/chameleon/<coordinator repo_id>/`) — deliberately OFF the trust-hashed profile surface (a pre-code security review found that materializing a coordinator profile to host it would create a new trust anchor and arm the security-deny floor on previously-ungoverned root files). The consumer is a turn-end Stop ADVISORY (never a deny): for each edited TS file it resolves the coordinator index via the workspace profile's `workspace.parent.repo_id`, and flags a removed export a sibling workspace still imports, confirmed by a live presence re-check on the importer. TypeScript/JS cross-package resolution only in v1 (Python is a documented gap); off the per-edit hot path (bootstrap-time build + Stop-time read of a cached artifact); no repo-code execution and no network, so it follows the default-ON-with-kill-switch principle; fails open at every seam. See `_crossworkspace_existence_advisory_lines` in `mcp/chameleon_mcp/hook_helper.py` and `build_cross_reverse_index` / `load_cross_reverse_index` in `mcp/chameleon_mcp/symbol_index.py`.
- `CHAMELEON_ARCHETYPE_FACTS=0` — kill switch for the per-edit archetype-scoped facts directive (default ON). On a Tier-2 (first-in-archetype) Edit/Write/NotebookEdit, the block leads with two compact directives scoped to the EDITED archetype: the class contract its files implement (base class, required methods, DSL macros — e.g. a Rails ActiveInteraction service: extends `ActiveInteraction::Base`, define `execute`) and "Already defined in this archetype — reuse these before creating a new one: …" (the archetype's own `key_exports`). Read from `conventions.json` (the same artifact the Tier-1 echo reads), scoped to one archetype, so it is additive over the repo-wide convention union injected once at SessionStart. Rendered OUTSIDE the imitate-spotlight (it is a chameleon directive, not witness data); every value is sanitized at the boundary. Bounded (8 methods / 8 macros / 40 exports with a `+N more` tail), fires only when the archetype actually has the data, no repo-code execution and no network (one cached artifact read), so it follows the default-ON-with-kill-switch principle. See `_archetype_facts_section` in `mcp/chameleon_mcp/hook_helper.py`.
- `CHAMELEON_COUNTEREXAMPLE=0` — kill switch for the per-edit off-pattern counterexample (default ON). When a team has taught a competing import (`/chameleon-teach-competing-import`, "prefer X over Y") and a real file still uses the discouraged form, the Tier-2 block pairs the canonical witness with that captured off-pattern line as a "do NOT write it this way" directive — the positive/negative contrast the in-context-learning literature favors over a positive example alone. When a team has taught SEVERAL competing imports for one archetype (winston→logger AND moment→date), every still-present off-pattern is shown, not just the last taught (`counterexamples.json` is a list per archetype, schema v2; a legacy v1 single-row artifact still loads). The artifact (`counterexamples.json`, trust-hashed, drop-stale) is built at teach time (a bounded repo scan) and at bootstrap/refresh, never on a hook hot path; the edit-time read is mtime-cached and fails open. It fires only when a genuine taught off-pattern is still present, so a clean archetype injects nothing. Rendered OUTSIDE the imitate-spotlight (a counterexample must not be copied) but still sanitized. No repo-code execution and no network, so it follows the default-ON-with-kill-switch principle; set to `0` to disable. A/B it with `--toggle counterexample` (the paired arm turns it off).
- `CHAMELEON_STOP_IDIOM_TERSE=0` — kill switch for the terse turn-end idiom self-review (default ON). At Stop, chameleon nudges a once-per-session re-check of the turn's edits against team idioms. Default (terse) scopes the review to the EDITED archetypes' idioms plus untagged/general ones (drops other-archetype idioms), summarizes to one line each the idioms the model already saw this session, and shows FULL text only for in-scope idioms not yet surfaced this session, so an idiom the model never saw is never reduced to a name. "Seen" is tracked at per-idiom-NAME granularity in the enforcement state's `idioms_shown_names`, computed from the actual `### ` headers that survived the char-capped Tier-2 block (`_shape_idioms_for_block`) — so an idiom truncated out of that block, or one from the deny path (which never emits idioms), correctly renders full. Principles are a one-line pointer to `.chameleon/principles.md` (they are already injected at SessionStart), and a turn touching no idiom-governed file does not fire (and does not burn the once-per-session marker, so a later governed edit still gets its review). Set to `0` to restore the legacy full dump of every idiom (reordered by edited archetype, char-capped) plus the full principles text. Pure advisory, no repo-code execution and no network, so it follows the default-ON-with-kill-switch principle. A/B it with `--toggle stop_idiom_terse` (the paired arm sets `=0`). See `_idiom_review_gate` in `mcp/chameleon_mcp/hook_helper.py` and `_render_stop_idioms` in `mcp/chameleon_mcp/tools.py`.
- `CHAMELEON_MULTIROOT_STOP=0` — kill switch for the multi-root Stop backstop (default ON). By default the Stop/SubagentStop hook discovers EVERY workspace root the session touched (per-edit hooks key enforcement state by each edited file's own workspace repo_id, not the launch cwd) and runs the turn-end gate pipeline per workspace against its own profile, so a session launched at a pure-coordinator monorepo root — whose own root is profile-less/untrusted, the tracked v2.38.28 dead spot — no longer leaves every touched workspace ungated at turn end. Discovery globs `_plugin_data_dir()/*/.enforcement.<session-marker>.json` and regroups the recorded files by `find_repo_root(file)`; each workspace is gated with per-workspace trust (`grants_root`, never unioned, so an ungranted sibling under a monorepo-shared repo_id stays untrusted), at most ONE reviewer spawn is paid across the whole Stop (the ranked-first armed root owns the budget; the rest run deterministic-only), and the Stop short-circuits on the FIRST blocking root so the anti-loop `stop_block_cap` is charged to one root per Stop. Advisories from every non-blocking workspace merge into one Stop context; one signed attestation is written per distinct run-root. A degenerate empty/None session_id (marker `unknown`) skips the glob (cwd root only) so a shared bucket cannot pull unrelated repos in; the fan-out is bounded by `CHAMELEON_STOP_MAX_ROOTS` (default 16, armed roots ranked first so the cap only ever drops advisory-only roots). Set to `0` to restore the legacy single-root path (cwd only). Pure advisory + deterministic gates, no repo-code execution and no network, so it follows the default-ON-with-kill-switch principle. A single-repo session is output-equivalent to the legacy path. See `_discover_stop_roots`, `_gate_one_root`, and `stop_backstop` in `mcp/chameleon_mcp/hook_helper.py`.
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
