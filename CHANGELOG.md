# Changelog

All notable changes to chameleon will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-11

Initial release.

### Added

#### Plugin surface

- 8 skills: `using-chameleon` (auto-fires on SessionStart) plus 7 user-invocable slash commands: `/chameleon-init`, `/chameleon-refresh`, `/chameleon-status`, `/chameleon-teach`, `/chameleon-trust`, `/chameleon-disable`, `/chameleon-pause-15m` (all with `/cham-*` aliases).
- 15 MCP tools: `detect_repo`, `get_archetype`, `get_pattern_context`, `get_canonical_excerpt`, `get_rules`, `lint_file`, `get_drift_status`, `refresh_repo`, `bootstrap_repo`, `list_profiles`, `merge_profiles`, `teach_profile`, `trust_profile`, `disable_session`, `pause_session`.
- 4 hooks: `SessionStart`, `PreToolUse` (Edit/Write/NotebookEdit), `PostToolUse` (Bash), `UserPromptSubmit`.

#### Languages

- TypeScript via the TypeScript Compiler API (`scripts/ts_dump.mjs` long-lived Node subprocess).
- Ruby on Rails via the [Prism](https://github.com/ruby/prism) parser (`scripts/prism_dump.rb` long-lived Ruby subprocess).

#### Bootstrap pipeline

- File discovery with two-tier exclusion sets (cluster pool vs canonical pool).
- 50,000-file post-exclusion ceiling.
- 7-tuple cluster signature: `(path_pattern_bucket, content_signal_match, top_level_node_kinds, default_export_kind, named_export_count_bucket, import_module_set_hash, jsx_present)`.
- Canonical selection with secret + injection + poisoning scanners; fail-closed when no candidate passes.
- Atomic multi-file commit: `.chameleon/.tmp/<txn-id>/COMMITTED` sentinel + flock-serialized rename.
- Workspace detection (pnpm / yarn / lerna / turbo / nx for TS; Rails for Ruby).
- Tool config reading (`.prettierrc`, `tsconfig.json`, `.eslintrc*`, `.rubocop.yml`).

#### Trust + opt-out

- Trust states: `untrusted` / `trusted` / `stale` / `n/a`. Stale state surfaces re-trust prompt automatically when the profile changes after grant.
- 4-level opt-out hierarchy: `.chameleon/.skip` (per-repo) → `CHAMELEON_DISABLE=1` (per-user env) → `disable_session` (per-session) → `pause_session` (timed, auto-expires).

#### Drift tracking

- Per-edit confidence observations recorded in `~/.local/share/chameleon/<repo_id>/drift.db` with WAL hardening.
- `observed_drift_score` exposed via `get_drift_status`; high drift triggers `/chameleon-refresh` recommendation.

#### Git integration

- `scripts/chameleon-merge-driver.sh` for `.gitattributes` 3-way merges of `.chameleon/*.json`.

#### Security

- Tag-boundary sanitization (closes 9 evasion tokens including zero-width and NFC variants).
- `safe_open` helper: realpath + repo-boundary + lstat + null-byte / NFD / forbidden-segment rejection.
- HMAC-signed exec log with concurrent-safe key generation (race-tolerant `O_EXCL` create).
- Poisoning scanner with security-context awareness (no false positives on legitimate non-crypto MD5/SHA1 use).

#### Tooling

- `scripts/bump-version.sh` — atomic version bump across 7 manifest files (claude-plugin, cursor-plugin, codex-plugin, gemini-extension, root and mcp package.json) with drift detection + audit modes.
- `tests/run_all_orders.py` — runs the 5 core test suites in 4 randomized orderings to verify order-independence.
- 18 test files totaling 391+ test points across unit, integration, MCP-protocol, hook, and real-Claude-Code acceptance levels.

### Known limitations

- Subprocess-per-call hooks; long-lived daemon is a future enhancement.
- Real-Claude-Code acceptance tests assume a TypeScript repo and/or Ruby on Rails repo path provided via `CHAMELEON_TEST_TS_REPO` / `CHAMELEON_TEST_RUBY_REPO` env vars.
- Multi-hour session stability and 50k-file repo at the cap not exercised at scale.
- Concurrent Claude Code sessions on the same repo: paths exist, not stress-tested.
