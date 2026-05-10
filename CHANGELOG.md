# Changelog

All notable changes to chameleon will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-11

### Fixed (audit-driven)

External audit ([chameleon-test-report.md](https://github.com/crisnahine/chameleon/blob/main/docs/chameleon-test-report.md)) surfaced 10 bugs; two independent verification agents confirmed them. This release addresses all of them.

- **🔴 Critical — `refresh_repo` no longer wipes user idioms.** Bootstrap previously wrote an empty `idioms.md` template inside the atomic transaction on every refresh, silently destroying every `/chameleon-teach` capture. The orchestrator now reads the existing `idioms.md` before the transaction and re-emits its content into the commit, preserving Tier 2 dimensions across refreshes.
- **🟠 High security — `profile.summary.md` now surfaces active idiom bodies.** The trust gate instructs reviewers to read `profile.summary.md` before granting trust; previously the Idioms section was a hardcoded placeholder, so poisoned idioms reached the model context unreviewed. `_build_summary_md` now inlines the `## active` section verbatim.
- **🟠 High — `teach_profile` validation cluster:**
  - Empty / whitespace-only feedback is rejected instead of creating orphan idiom entries.
  - User-supplied `### slug` headers are honored as-is; the auto-wrapper fires only when no slug is present.
  - Level-1 and level-2 ATX headings in feedback bodies are escaped (`\#`, `\##`) so a `## deprecated` line in user input can no longer fork `idioms.md`'s section structure.
  - The `_(no idioms yet …)_` placeholder is dropped on first idiom add.
  - The read-modify-write is now wrapped in an advisory flock so concurrent `/chameleon-teach` calls don't lose idioms.
- **🟡 Medium (schema-breaking) — `path_pattern_bucket_for` no longer collapses `app/` and `spec/` clusters.** Prior versions used `parts[-3:-1]`, which mapped `app/controllers/api/v1/foo.rb` and `spec/controllers/api/v1/foo_spec.rb` into the same `"api/v1"` bucket; `get_archetype`'s `cluster_size` tiebreak then routinely surfaced spec clusters for app/ files. The new bucketing prepends the top-level segment (`app/api/v1` vs `spec/api/v1`), restoring discriminative path patterns. Bootstrap also now relativizes file paths before bucketing so cluster patterns match what the runtime archetype lookup computes.
- **🟡 Medium — `list_profiles` validates inputs.** `limit ≤ 0`, `limit > 1000`, and unknown `cursor` values now return failed envelopes with explicit error messages instead of silently coercing.
- **🟡 Medium — `trust_profile` differentiates path errors.** "must be absolute" / "does not exist" / "is not a directory" / "no .chameleon/" / "no profile.json" are now distinct errors instead of the previous catch-all "expected absolute repo path".
- **🟢 `lint_file` envelope carries `"stub": true`** + `stub_reason` so callers don't treat the always-empty violations list as a passing lint. Real lint engine ships in Phase 4.
- **🟢 `refresh_repo.force`** is now documented as a forward-compat no-op in the docstring (was silently discarded).
- **🟢 Helper `_resolve_repo_root_status`** added alongside `_resolve_repo_root_by_id` so future tools can distinguish "untrusted/unknown repo_id" from "trust record present but repo_root gone."

### Breaking

- `PROFILE_SCHEMA_VERSION` bumped from 4 → 5. The `paths_pattern` field in `archetypes.json` is no longer compatible with v4 profiles. The loader refuses to load v0.2 profiles on engines older than 0.2.0; engines ≥ 0.2.0 can run `/chameleon-refresh` to rebuild a v5 profile. Existing trust grants need to be re-granted after re-bootstrap because the rebuilt profile has a new SHA.
- `ENGINE_MIN_VERSION` bumped from `0.1.0` → `0.2.0`; `mcp/chameleon_mcp/__version__` bumped to `0.2.0`.

### Added

- `tests/v0_2_regression_test.py` — 25 assertions covering every fix above. Each assertion fails on v0.1.1 source and passes on v0.2.0.

## [0.1.1] — 2026-05-11

### Changed

- **Zero-touch install.** `.mcp.json` now invokes `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp` instead of pointing at a pre-built `.venv/bin/chameleon-mcp`. uv builds the Python venv on first launch (~5–10s), eliminating the manual `uv sync` step after marketplace install.
- **Lazy Node dep install.** The TypeScript extractor now runs `npm install` automatically inside `${CLAUDE_PLUGIN_ROOT}/mcp/` the first time it's invoked against a TS repo, instead of requiring users to run `npm install` manually. Ruby-only users never trigger this path.
- Path resolution in `extractors/typescript.py` and `extractors/ruby.py` now goes through a `plugin_root()` helper that prefers `CLAUDE_PLUGIN_ROOT` over file-relative resolution, so the MCP server works correctly when run from `uvx`'s isolated cache.

### Added

- `mcp/chameleon_mcp/plugin_paths.py` — single source of truth for plugin-root resolution. Honors `CLAUDE_PLUGIN_ROOT` (Claude Code), `CHAMELEON_PLUGIN_ROOT` (test override), then falls back to file-relative.

### Fixed

- README and INSTALL.md no longer instruct users to run `uv sync` and `npm install` manually after marketplace install. Both are now handled by the plugin itself.

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
