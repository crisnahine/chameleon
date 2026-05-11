# chameleon — Architecture Overview

A 5-minute orientation for new contributors. Read this before your first PR.
Full design lives in [ARCHITECTURE.md](../../ARCHITECTURE.md) (~2,000 lines);
this doc covers the load-bearing parts.

## What chameleon does

chameleon is a Claude Code plugin that auto-derives a repo's actual coding
conventions and injects archetype-aware guidance before every Edit/Write.
It clusters files into archetypes via AST + statistical signals, picks
canonical examples per archetype, captures team-specific idioms via
`/chameleon-teach`, and surfaces those artifacts to the model on each edit.
Profiles are committed to git so the team shares them; trust is per-user.
v1 supports TypeScript; v1.5 added Ruby on Rails. Nothing else.

## Component map

```
chameleon/
├── .claude-plugin/   plugin.json + marketplace.json (Claude Code manifests)
├── .cursor-plugin/   Cursor manifest
├── .codex-plugin/    Codex CLI manifest
├── gemini-extension.json   Gemini CLI manifest
├── hooks/            session-start, preflight-and-advise, posttool-recorder, callout-detector
├── skills/           using-chameleon (auto) + 7 slash commands (init, refresh,
│                       status, teach, trust, disable, pause-15m)
├── mcp/              chameleon-mcp Python server (FastMCP, stdio transport)
│   └── chameleon_mcp/
│       ├── server.py             tool registration + entry point
│       ├── tools.py              MCP tool implementations
│       ├── bootstrap/            discovery → cluster → canonical → commit
│       ├── extractors/           typescript.py (ts_dump.mjs), ruby.py (prism_dump.rb)
│       ├── profile/              schema, migrations, secret/poisoning scanners
│       ├── drift/                sqlite_config.py (WAL hardening)
│       └── safe_open.py          shared file-read sandbox
├── scripts/          ts_dump.mjs (long-lived Node), prism_dump.rb (long-lived Ruby),
│                       bump-version.sh, chameleon-merge-driver.sh
└── tests/            unit, integration, MCP protocol, real-Claude-Code acceptance
```

## The advisory flow

```
SessionStart hook
  → loads using-chameleon SKILL.md
  → emits profile primer (archetype names, paths, trust state, drift)
  → cache_control split: static prefix cached, ephemeral suffix (cost, drift) not

User asks for an edit
  → using-chameleon skill prompts the model to call MCP first

PreToolUse hook (Edit/Write/NotebookEdit)
  → safety hard-deny (path traversal, secrets, lockfiles, vendored, etc.)
  → safe_open: realpath + repo-boundary + lstat + null-byte/NFD/forbidden-segment
  → dedup: if model already invoked get_pattern_context this turn, skip
  → MCP get_pattern_context with 2s timeout
  → on success: tag-boundary sanitize, inject as <chameleon-context>
  → on timeout/error: fail-open silent, edit proceeds, telemetry logged

PostToolUse Bash hook
  → HMAC-signed exec log to ${TMPDIR}/.chameleon_exec_log/<repo_id>/

UserPromptSubmit hook
  → callout-detector: surface /chameleon-disable on frustration phrases
```

The boundary rule: safety layer is fail-closed (block on error). Advisory
layer is fail-open (proceed on error with a warning). Conflating them
breaks one or the other.

## Bootstrap pipeline

`/chameleon-init` and `/chameleon-refresh` both run this:

```
1. Discovery       walk the repo, exclude generated/vendor/lockfiles,
                   apply 50k post-exclusion ceiling
2. AST parse       ts_dump.mjs (TS Compiler API) or prism_dump.rb (Prism)
                   subprocess, batched stdin → NDJSON stdout
3. Cluster sig     7-tuple per file: (path_pattern_bucket, content_signal,
                   top_level_kinds, default_export, named_export_count_bucket,
                   import_hash, jsx_present). Exact match = same cluster.
4. Canonical       pick the recency-weighted, secret-scanned, injection-scanned,
                   poisoning-scanned witness per cluster. Fail closed if no
                   candidate passes the gates.
5. Atomic commit   write to .chameleon/.tmp/<txn-id>/, write COMMITTED sentinel
                   LAST, flock-serialized rename. Loaders refuse to read
                   .chameleon/ if COMMITTED is missing.
```

Idempotence: running bootstrap twice on the same repo state produces a
byte-identical profile.

## Trust and opt-out

**Trust** is per-user, per-repo, non-blocking. The profile lives at
`.chameleon/profile.json` (committed). The trust grant lives at
`~/.local/share/chameleon/<repo_id>/.trust`. `/chameleon-trust` reads
`profile.summary.md` (which now includes active idiom bodies — see v0.2.0
audit fix) and asks the user to type the repo basename to confirm.
Granting trust on a profile SHA does not authorize future content; new
canonicals/idioms after grant re-prompt.

**Opt-out hierarchy** (most specific wins):
1. `.chameleon/.skip` — per-repo, committed
2. `CHAMELEON_DISABLE=1` — per-user env
3. `disable_session` MCP tool / `/chameleon-disable` — per-session
4. `pause_session` / `/chameleon-pause-15m` — timed, auto-expires

## Drift tracking

`PreToolUse` records per-edit confidence observations to
`~/.local/share/chameleon/<repo_id>/drift.db` (SQLite, WAL,
busy_timeout=30000, retry-with-jitter on SQLITE_BUSY).
`get_drift_status` exposes `observed_drift_score` = ratio of low-confidence
edits to total edits in the last N days. High drift triggers a
`/chameleon-refresh` recommendation in `/chameleon-status`.

## Invariants worth knowing before changing code

- **Atomic commits.** Every multi-file profile write goes through
  `bootstrap.transaction.atomic_profile_commit`: write to `.chameleon/.tmp/<txn-id>/`,
  write `COMMITTED` last, rename. Loaders gate on the sentinel. Don't add
  writes that bypass this.
- **Profile schema versioning.** `PROFILE_SCHEMA_VERSION` in
  `bootstrap/orchestrator.py` and `CURRENT_SCHEMA_VERSION` in `profile/schema.py`
  must stay in sync. Breaking changes ship with a migration script + fixture
  pair (see [MAINTAINER.md](MAINTAINER.md#schema-migration-authoring)).
- **Idiom preservation across refresh.** `refresh_repo` MUST read the existing
  `idioms.md` before the transaction and re-emit its content. v0.1 had a bug
  here that silently wiped every `/chameleon-teach` capture; the v0.2 audit
  fix added a regression test.
- **Lazy npm install.** The TypeScript extractor runs `npm install` inside
  `${CLAUDE_PLUGIN_ROOT}/mcp/` the first time it's needed (TS repo only).
  Ruby-only users never trigger this path. Don't move install side effects
  to plugin load time — they belong inside the extractor.
- **Tag-boundary sanitization.** Before injecting `<chameleon-context>`,
  escape any literal `</chameleon-context>`, `</chameleon`, `<chameleon-context>`
  in canonical/idiom content (plus zero-width and NFC variants). Regression
  fixtures cover 9 evasion tokens.
- **safe_open is mandatory.** Every file read in the MCP server goes through
  `safe_open(repo, rel_path)`. realpath + prefix-match + lstat + null-byte
  rejection + NFD `..` rejection + Windows separator rejection. Bypass it
  and you ship a path-traversal CVE.
- **Hook fail-open vs fail-closed.** Advisory MCP call fails → proceed.
  Safety check fails → block. Wiring an advisory error into a deny is a
  productivity regression; wiring a safety error into a fail-open is a
  security regression.

## Where to read more

- Full design, threat model, calibration targets, failure-mode runbook:
  [ARCHITECTURE.md](../../ARCHITECTURE.md)
- Maintainer runbook (release checklist, key rotation, schema migration
  authoring): [MAINTAINER.md](MAINTAINER.md)
- Architecture decisions: [decisions/](decisions/) (start with ADR-0001
  for the best-effort-vs-framework-aware framing)
- Test layout, parameterization, command list: [../../CONTRIBUTING.md](../../CONTRIBUTING.md)
