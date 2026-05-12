# Chameleon Release Notes

## 0.5.7 — 2026-05-12

Follow-up to v0.5.6. Fixes the 4 still-active v0.5.6 bugs plus 22 new bugs uncovered during a fresh from-scratch dogfood run, plus 4 architectural concerns from a deep code audit.

### Critical

- **BUG-NEW-002** — `find_repo_root` now prefers a `.chameleon` ancestor over a closer language manifest. Pre-fix, monorepos with `.chameleon` at the root and `package.json` at each workspace returned the workspace as `repo_root`, masking the root profile. Every file in workspace subdirs reported `profile_status: no_profile`. Two-pass walk: pass 1 walks up to the first marker (returns `.chameleon` immediately); pass 2 continues looking for an enclosing `.chameleon`. Defensive about test-isolation flakiness in tmp paths.
- **BUG-NEW-012** + **JSONC URL preservation** — tsconfig `extends` now resolves workspace-package aliases (e.g. `@plane/typescript-config/react-library.json`) via pnpm-workspace.yaml / package.json `workspaces` lookup. Additionally, the JSONC stripper is now state-aware: it no longer eats `//` inside string literals, so `"$schema": "https://json.schemastore.org/tsconfig"` no longer corrupts the JSON. Any tsconfig with a `$schema` URL silently failed to parse pre-fix.

### High

- **BUG-014 (continued)** — Pure-Ruby repos (ef-api, maybe) now extract `.rubocop.yml`. The BUG-019 sidecar walk-up logic fired on any repo without `package.json` or `tsconfig.json` and grabbed an unrelated ancestor; pure-Ruby repos with their own `Gemfile` got pulled past it. Gate now requires "no own JS signals AND no own Ruby signals".
- **BUG-019 (continued)** — Sidecar bootstrap picks the extractor by what's IN the sidecar, not by parent's primary language. forem `app/javascript/` (Rails primary repo) now bootstraps as TypeScript: 591 files / 37 archetypes (was 0 files / `failed_unsupported_language`).
- **BUG-NEW-010** — Hook scripts now log stderr to `~/.local/share/chameleon/.hook_errors.log` on non-zero exit. Pre-fix the bash `|| printf '{}'` swallowed Python helper failures, so users without `uv` got zero advisory injection with zero warning.
- **BUG-NEW-020** — `trust_profile` now calls `load_profile_dir` before granting. Refuses trust on a corrupted profile.json or unsupported schema_version. Pre-fix you could "trust" an unreadable profile and then wonder why chameleon never injected anything.

### Medium

- **BUG-003 (continued)** — Ad-hoc monorepos that have per-workspace tool configs (`apps/<x>/.eslintrc.cjs`) but no root-level ones now adopt the first workspace's configs as repo-wide. Source paths are workspace-prefixed so the user can tell which workspace contributed the config. bulletproof-react: `rules_extracted` 0 → 3.
- **BUG-023 (continued)** — `detect_repo` surfaces `profile_status: "profile_unsupported_schema_version"` when `profile.json` carries a schema_version > MAX_SUPPORTED_SCHEMA_VERSION. Loader already refused; detect_repo now matches.
- **BUG-NEW-001** — `daemon_status` reports `running_version` (from package metadata); `/chameleon-status` skill instructs the model to compare against `installed_plugins.json` and surface the mismatch with a clear restart recommendation. Catches BUG-018 in the wild.
- **BUG-NEW-005** — Bootstrap surfaces `nested_profile_warnings` when sub-workspaces have stale `.chameleon/` dirs (from prior bootstraps that targeted them directly).
- **BUG-NEW-008** — `content_signal_match` is always `"strong" | "weak" | "none"` (never `null`). Eliminates schema drift on the no-archetype miss path.
- **BUG-NEW-011** — Trust-stale message now explains the cause: refresh changes the profile sha, which invalidates the grant.
- **BUG-NEW-015** — `hooks.json` PostToolUse matcher now `Bash|Edit|Write|NotebookEdit` (was Bash-only). Drift observation logic targets edits.
- **BUG-NEW-021** — Bootstrap now populates drift.db's `files` table with one row per clustered file. Pre-fix the table got 0 rows from bootstrap (only PreToolUse hooks added rows). Refresh's incremental detection now has a baseline.
- **BUG-NEW-023** — `get_drift_status` uses `calendar.timegm` (UTC) instead of `time.mktime` (local TZ). 8-hour `days_since_refresh` drift on PST hosts fixed.

### Low / UX

- **BUG-NEW-006** — Free-form `teach_profile` slug derived from the rationale's first non-empty line (kebab-case, ≤5 words) instead of the opaque `idiom-<date>-<epoch>-<rand>`.
- **BUG-NEW-007** — Don't escape `#` / `##` inside fenced code blocks in idioms.md. `# frozen_string_literal: true` renders literally now.
- **BUG-NEW-014** — Frustration patterns expanded to cover "annoying", "hate", "frustrated", expletives. Removed naive solo `stop` (false-triggered on "don't stop now" / "ayaw og stop").
- **BUG-NEW-018** — Pause skill docs note that `pause_session` accepts any 1-240 minute value; `-15m` is the default-alias.
- **BUG-NEW-022** — `edit_observations` retention: hard cap 50 000 triggers cleanup; soft cap 10 000 is the post-cleanup ceiling. Trims by 90-day age first, then by row id.

### Architecture

- **`_constants.py`** — Centralized definitions for `profile_status`, `trust_state`, `bootstrap_status`, `confidence_band`, `content_signal_match` enum values. Pinned by `tests/v0_5_7_constants_test.py`.
- **`_thresholds.py`** — Env-overridable thresholds: `CHAMELEON_WORKSPACE_FANOUT_CAP`, `CHAMELEON_EDIT_OBS_HARD_CAP`, `CHAMELEON_MAX_EXTENDS_HOPS`, etc. (11 in total).
- **Duplicated-logic contract** — `tests/v0_5_7_duplicated_logic_test.py` pins `tools.py` ⟷ `orchestrator.py` ⟷ `canonical.py` to byte-equal output for `_read_renames_overlay`, `_hash_cluster_key`, `_compute_repo_id`. Drift now surfaces in CI rather than production.

### Audit + verification

- `docs/test-audit-2026-05-12.md` — 3 212-line audit of 9 focus files, 120 functions, 1 376 testable scenarios bucketed (GOOD/BAD/EDGE/GAP/SLOP), cross-referenced against the 73 existing test files.
- `tests/v0_5_7_audit_gap_tests.py` — 26 high-priority gap tests from the audit (drift baseline, retention boundary, fail-open contracts, schema acceptance v3..v7, schema rejection v8+, 32-level walk cap, hook fail-open).
- v0.5.7 dogfood across 9 Testing Apps: response sizes dropped 10x-50x (95KB-1.9MB → 9.7KB-53KB), rubocop now extracts on 5/5 Ruby apps, plane workspace alias resolves through full extends chain. Side-by-side in `_chameleon_test_results/v0.5.7/_dogfood_v0.5.7_vs_v0.5.5.md`.

## 0.5.6 — 2026-05-12

Fixes 26 bugs surfaced by the May 12 dogfood run across 9 real apps (bulletproof-react, ef-client, excalidraw, plane, ef-api, gitlabhq, maybe, forem, mastodon). One commit per bug.

### Critical

- **BUG-009 / BUG-008** — `sparse_cluster_warnings` no longer busts the MCP response cap. Warnings are aggregated by `paths_pattern` (same-pattern singletons collapse to one row) and the resulting list is hard-capped at 50 entries with a truncation marker. Pre-fix, bootstrap of ef-client / gitlabhq returned 0.5-2 MB JSON that the MCP transport refused.
- **BUG-016** — Rails-with-frontend detection regression test locks in `app/javascript/` + `app/assets/javascripts/` (gitlabhq) + `app/frontend/` (Rails 7) coverage. The fix had shipped in v0.5.3; the bug surfaced because the running MCP was v0.5.2.

### High

- **BUG-001** — When bootstrap fails on a root that looks like an ad-hoc monorepo, the response now carries `discovery_hints` listing each `apps/*` / `packages/*` child that has its own package.json or Gemfile.
- **BUG-002** — New loose-merge clustering tier. Sparse clusters sharing the same `paths_pattern` and AST shape (Jaccard >= 0.5) fold into one cluster marked `cluster_tier="loose"`. Closes the gap where 90% of files used to return `archetype: null`.
- **BUG-003** — `.eslintrc.{js,cjs,mjs}` parser now shells out to `node` to evaluate the config and JSON-stringify the export. Regex parser kept as a fallback when Node isn't on PATH.
- **BUG-014** — `.rubocop.yml` extractor: top-level `AllCops`, `plugins`, `require`, and individual cops land under `rules.rubocop` in rules.json. Ruby files now get linting guidance from `get_pattern_context`.
- **BUG-019** — Sidecar bootstrap (e.g. `<rails-repo>/app/javascript`) inherits tool configs and extractor selection from a parent up to four levels up. The `language_hint.note`-suggested command no longer fails.
- **BUG-020** — Parse `eslint.config.{js,mjs,cjs,ts}` (ESLint 9+ flat config). Mastodon's 10KB `eslint.config.mjs` no longer extracts zero rules.

### Medium

- **BUG-004** — `trust_profile` accepts either an absolute path or a repo_id hex digest (matches every other tool).
- **BUG-010** — TS detection falls back to scanning for `.ts`/`.tsx` files (depth 3, capped at 50) so hoisted-deps workspaces like `excalidraw-app` are recognized. Guards against workspace-coordinator roots so fanout still wins where appropriate.
- **BUG-011** — Top-level `archetypes_detected` sums across all successful workspaces; a new `archetypes_per_workspace` map gives the per-workspace breakdown.
- **BUG-015** — Archetype fallback by longest path-prefix overlap. `app/controllers/application_controller.rb` now resolves to the `controller` archetype (confidence: low) instead of `archetype: null`.
- **BUG-017** — Reciprocal `language_hint`: when TS wins at the root but a Gemfile and >=50 .rb files exist, the envelope surfaces `primary=typescript, secondary_detected=ruby`.
- **BUG-018** — New `scripts/prune-plugin-cache.sh` removes stale cached plugin versions. README now includes an "Upgrading" section that calls out the restart-Claude-Code requirement.
- **BUG-021** — `detect_repo` distinguishes `profile_corrupted` from `profile_present` (validates `profile.json` is parseable JSON).
- **BUG-022** — `get_pattern_context` corrupted-profile early return now uses the same envelope shape as the healthy path. The `archetype.archetype` key (not `name`) is consistent; `content_signal_match` and `idioms` fields are always present.
- **BUG-024** — Preflight hook gates canonical injection on `trust_state`. Untrusted profiles emit a one-time trust prompt per session (marker file) and suppress canonical/rules until `/chameleon-trust` runs. Closes the security gap where the skill rule said "no injection" but the hook injected anyway.
- **BUG-025** — `/chameleon-status` skill trimmed to features that actually exist (profile summary, trust state, drift). Phase-4 telemetry surfaces (value attribution, MCP error rate, p99 hook latency) moved to a Future Work section.

### Low

- **BUG-005** — `trust_state` returns `n/a` when there's no profile.
- **BUG-006** — Rename candidates exclude the current archetype name and any derivative of it.
- **BUG-007** — `ENGINE_MIN_VERSION` reads from package metadata at import time instead of being hardcoded to `"0.4.0"`.
- **BUG-012** — "No source files" is uniformly `failed_unsupported_language` (was split between `failed` and `failed_unsupported_language`).
- **BUG-013** — Collision disambiguation prefers path segments over numeric counters (`react-component-button` / `react-component-icons` rather than `react-component-10`).
- **BUG-023** — Refuse to load a profile with `schema_version` newer than this engine supports.
- **BUG-026** — `bootstrap_repo` requires `force=true` to overwrite a committed profile.

### Upgrade

After installing v0.5.6, restart Claude Code so the MCP subprocess reloads. Then optionally run `scripts/prune-plugin-cache.sh --apply` to remove old cached versions.

## 0.1.0 — 2026-05-11

Initial release.

### Highlights

- **Two-language support**: TypeScript and Ruby on Rails.
- **15 MCP tools**: `detect_repo`, `get_archetype`, `get_pattern_context`, `get_canonical_excerpt`, `get_rules`, `lint_file`, `get_drift_status`, `refresh_repo`, `bootstrap_repo`, `list_profiles`, `merge_profiles`, `teach_profile`, `trust_profile`, `disable_session`, `pause_session`.
- **8 skills**: `using-chameleon` (auto-fires on SessionStart) plus 7 user-invoked slash commands.
- **4 hooks**: `SessionStart`, `PreToolUse` (Edit/Write/NotebookEdit), `PostToolUse` (Bash), `UserPromptSubmit`.
- **Atomic profile commit**: `.chameleon/.tmp/<txn-id>/COMMITTED` sentinel + flock-serialized rename, safe under concurrent bootstrap calls.
- **Trust + material-change flow**: trust_state values `untrusted` / `trusted` / `stale` / `n/a`; preflight surfaces re-trust hint when stale.
- **Opt-out hierarchy**: `.chameleon/.skip` (per-repo) → `CHAMELEON_DISABLE=1` (per-user) → `/chameleon-disable` (per-session) → `/chameleon-pause-15m` (timed). All four wired and verified.
- **Drift tracking**: each Edit/Write hook records a confidence observation in `~/.local/share/chameleon/<repo_id>/drift.db`; `get_drift_status` returns `observed_drift_score` + recommended_action.
- **Git merge driver**: `scripts/chameleon-merge-driver.sh` integrates with `.gitattributes` for clean 3-way merges of `.chameleon/*.json`.
- **Security**: tag-boundary sanitization (9 evasion tokens covered including zero-width and NFC variants), poisoning scanner with security-context awareness, secret scanner (detect-secrets + fallback regex), HMAC-signed exec log with concurrent-safe key generation.
- **Performance**: TypeScript repo of ~2,400 files bootstraps in ~3s; Ruby on Rails repo of ~4,800 files bootstraps in ~3s.

### Test coverage

391+ test points across 17 test files:

| Suite | Coverage |
|---|---|
| `smoke_test.py` | 54 — baseline unit + integration |
| `comprehensive_test.py` | 175 — every helper, every MCP tool surface |
| `bootstrap_mechanism_test.py` | 43 — Claude Code SessionStart hook chain |
| `mcp_protocol_test.py` | 27 — stdio MCP protocol end-to-end |
| `stubs_implemented_test.py` | 22 — drift.db + merge_profiles |
| `find_repo_root_test.py` | 16 — non-git repo detection |
| `hmac_key_edge_cases_test.py` | 17 — wrong-uid, chmod, concurrent gen |
| `optouts_test.py` | 22 — all 4 opt-out levels |
| `trust_flow_test.py` | 18 — confirmation token + Claude Code roundtrip |
| `cold_start_init_test.py` | 22 — fresh-repo bootstrap |
| `refresh_drift_test.py` | 10 — drift detection on synthetic + real repos |
| `teach_roundtrip_test.py` | 13 — idiom round-trip |
| `pretooluse_hook_test.py` | 9 — PreToolUse fires in real Claude Code |
| `git_merge_driver_test.py` | 6 — `.gitattributes` integration |
| `material_change_test.py` | 10 — stale trust re-prompt |
| `claude_code_acceptance_test.py` | 26 — both languages via real Claude Code |
| `all_commands_acceptance_test.py` | 42 — all 7 slash commands + 13 MCP tools × 2 stacks |

`tests/run_all_orders.py` runs the 5 core suites in 4 randomized orders to verify order-independence.

### Known limitations

- **Multi-hour session stability**: not exercised. drift.db growth over weeks unverified.
- **50,000-file repo at the cap**: ceiling exists in code, not exercised at scale.
- **Concurrent Claude Code sessions on the same repo**: paths exist, not stress-tested.
- **Marketplace publishing**: only verified via `--plugin-dir`; never published.
- **Long-lived daemon model**: subprocess-per-call hooks. Daemon model is a future enhancement.
