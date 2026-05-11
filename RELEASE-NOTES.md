# Chameleon Release Notes

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
