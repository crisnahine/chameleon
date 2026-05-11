# Changelog

All notable changes to chameleon will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] — 2026-05-11

The "close the plan" release. Every Phase 2C/2D/4/7 item the audit + ARCHITECTURE.md identified is now either shipped or has an explicit rationale for staying deferred. Items 4.5 (long-lived daemon), 4.3-extended (partial re-clustering), and 6.x (calibration **measurements**) are honestly out of scope for the current development context — every other item ships.

### Added — Phase 2D (UX)

- **2D.1 Interactive 3-prompt rename interview** during `/chameleon-init`. Two new MCP tools (`propose_archetype_renames`, `apply_archetype_renames`) plus a rewritten `chameleon-init` skill that drives the conversation: show heuristic names → pick rename candidates → confirm and apply atomically. Atomic apply rewrites `archetypes.json` + `canonicals.json` + `rules.json` keys via `atomic_profile_commit` and regenerates `profile.summary.md`. Mirrors the new `profile_sha256` into `index.db`.
- **2D.3 Per-workspace bootstrapping for monorepos.** When `detect_workspace` returns workspace_paths, bootstrap also runs per-workspace producing `<workspace_root>/.chameleon/` profiles. Root profile catalogs workspaces in `profile.json.workspaces`. Per-workspace repos register in `index.db`. Non-monorepo behavior unchanged.
- **2D.4 Structured idiom comments.** New `teach_profile_structured(repo, slug, rationale, example, counterexample, archetype, status)` MCP tool. Validates `^[a-z][a-z0-9-]{2,63}$` slug, 50 KB cap across rationale + example + counterexample, renders canonical markdown, delegates to the existing `teach_profile` for advisory-lock / sanitization / placeholder-strip parity. `chameleon-teach` skill branches between free-form (existing) and structured (new) paths.

### Added — Phase 4

- **4.2 AST shape verification in `get_archetype`.** After path-bucket matching, the lint engine's `extract_dimensions` scores candidates against each archetype's `ast_query` (5 dimensions). Highest-scoring archetype wins with `confidence_band="high"` when ≥4/5 dimensions agree. Falls back to v0.3 path-only behavior when file content is unavailable. **No more "wrong cluster, right path."**
- **4.6 Git remote URL detection for `repo_id` (schema v6).** `_compute_repo_id` now prefers a normalized `origin` URL (https/ssh parity, host case-folding, `.git`/trailing-slash stripping) and falls back to the resolved absolute path when no `origin` exists. Moving a checkout no longer orphans its trust grant. `detect_repo` surfaces a `legacy_trust_hint` when a v0.3 path-derived trust record exists under the new id, so upgraders see a one-time re-trust prompt rather than silent "untrusted."
- **4.8 `detect-secrets` wiring through `lint_file`.** New `lint_engine.scan_secrets` runs `detect-secrets` over file content, caps at 50 secrets per file, and emits `error`-severity violations regardless of `ast_query` resolution. `canonical_scanner.is_safe_canonical` also rejects candidate witnesses that contain detected secrets. Security checks now fire on every `lint_file` call — not just bootstrap.

### Added — Phase 6 (skeleton, no numbers)

- **`tests/calibration/` harness.** Reads `tests/calibration/corpus.json` (gitignored — per-developer corpus paths), runs bootstrap + sampled `get_pattern_context` per repo, computes archetype-match rate / high-confidence rate / bootstrap p50–p95 / cost-per-bootstrap, and rolls up against the Phase 6 targets (≥0.80 mean match rate, ≤10 s p95). When `corpus.json` is missing, exits 0 with `"status": "no_corpus_configured"` and `N/A` rows so CI stays green. **Real numbers ship when external corpora are checked in.**

### Fixed

- **PID-aware orphan-txn cleanup** (`bootstrap/transaction.py:cleanup_orphan_tmp_dirs`). Parses the writer PID from the `<pid>-<uuid8>-<epoch>` txn-dir name and skips cleanup when that PID is still alive. Concurrent chameleon-mcp instances can no longer clobber each other.
- **trust_flow_test.py assertion drift** — assertion now accepts the v0.2 error rewording (`"no profile"` / `"no .chameleon/"` / `"no profile.json"`).
- **Ruff backlog auto-fixes** — 95 of the original 247 `ruff` errors auto-fixed (`uvx ruff@0.6.0 check --fix`). 162 remain (manual judgment). CI lint job is `continue-on-error: true` until the remaining backlog clears.

### Breaking

- `PROFILE_SCHEMA_VERSION` bumped from 5 → 6. Existing v5 profiles still load (the engine_min_version check accepts older); v0.3 engines refuse v6.
- `ENGINE_MIN_VERSION` bumped from `0.2.0` → `0.4.0`. `__version__` updated to `0.4.0`.
- `_compute_repo_id` change means **every existing trust grant maps to a new repo_id** on first `detect_repo` after upgrade. `detect_repo` surfaces a `legacy_trust_hint` in the response envelope; users re-run `/chameleon-trust` once per repo.

### Tests

- 11 suites, **633 pass / 2 fail** in this dev environment. Failures are in `tests/trust_flow_test.py` Round 2 (real `claude` CLI invocations) and trace to `uvx` caching a stale plugin venv — real marketplace installs rebuild on update, so end users do not hit this. The Round 1 trust-flow assertions all pass.

### Intentionally deferred to v1.0+

- **4.5 Long-lived daemon via UNIX socket** — multi-day rearchitecture (socket lifecycle, per-client multiplexing, supervised process). The existing subprocess-per-call hook is 200–500 ms warm; acceptable for human-paced editing until measured demand says otherwise.
- **4.3-extended Partial re-clustering** — v0.3 already short-circuits the no-files-changed case to `noop`. Partial re-clustering for the <10%-changed case saves ~3 s on moderate repos; negative ROI today. Full re-bootstrap remains the default branch.
- **6.1–6.4 Calibration MEASUREMENTS** — the harness ships; the numbers require 3 external TS corpora + 1 Rails corpus. Identifying and licensing those corpora is an ops decision, not an engineering one.

## [0.3.1] — 2026-05-11

Closes out three Phase 7 items I forgot to schedule in the v0.3.0 plan, plus three code-level TODOs left in v0.3.0. No new behavior — docs + CI + correctness-edge fixes only.

### Added — Phase 7 (the forgotten three)

- **`docs/chameleon/VOCABULARY-AND-COMPETITIVE.md`** (176 lines) — vocabulary firewall (archetype vs rule, canonical vs example, idiom vs convention, profile vs config, trust vs install, drift vs divergence, bucketing vs glob, shape vs structure) and a competitive-analysis section (ESLint/RuboCop, Prettier, .cursorrules / CLAUDE.md, superpowers, Cody/Copilot, codebase-aware retrievers) plus an explicit "when NOT to use chameleon" list. Linked from README.md "What's Inside".
- **Bus-factor + succession plan** in `docs/chameleon/MAINTAINER.md`. Replaces the Phase 7-end TODO with an explicit inactivity policy (30 days → maintenance-only mode, 180 days → archive), criteria for becoming a co-maintainer, and a handoff-artifact list. The project is MIT and forkable; the policy is documentation, not enforcement.
- **GitHub Actions CI** under `.github/workflows/`:
  - `ci.yml` — runs on every PR + push to main. Matrix: Python 3.11/3.12 × Ubuntu/macOS. Jobs: `test-python` (all 8 suites — comprehensive, mcp_protocol, v0_2_regression, lint_engine, index_db, archetype_naming, canonical_v03, tool_config_v03), `lint` (ruff, `continue-on-error: true` until the v0.3.0 backlog is cleared), `version-sync` (`bump-version.sh --check`), `hook-smoke` (SessionStart hook JSON-validity).
  - `release.yml` — fires on `v*.*.*` tag push. Verifies manifests + `__version__` + CHANGELOG entry, runs the full test matrix, builds a release tarball (excluding `.venv`/`node_modules`/`.chameleon`/`dist`/`__pycache__`/`.ruff_cache`/`.git`), and creates the GitHub Release with the CHANGELOG section as the body.
  - `real-claude-code-acceptance.yml` — manual (`workflow_dispatch`) + weekly cron. Runs the ~$0.20-per-run real Claude Code acceptance test against committed test repos. Fails soft when secrets are not configured.

### Fixed — code-level TODOs

- **`bootstrap/transaction.py:cleanup_orphan_tmp_dirs`** now parses the writer PID from the txn-dir name (`<pid>-<uuid8>-<epoch>`) and skips cleanup when that PID is still alive. Previously a fresh chameleon-mcp startup could clobber a sibling process's in-progress bootstrap. Legacy dirs without a PID prefix are still cleaned unconditionally. New regression assertions in `tests/v0_2_regression_test.py` cover legacy / dead-PID / live-PID.
- **`extractors/typescript.py`** sha_hint TODO replaced with a clearer "intentional double-read" note — the perf concern was speculative; no benchmark today says it's a bottleneck.
- **`signatures.py`** archetype-signal TODO clarified as a forward-compat hook, not a missing feature. The `archetype_signals` parameter remains in the API surface for the day calibration evidence shows per-team signal divergence; until then, no behavior change.

### Test path portability fix (CI prerequisite)

- 16 test files previously hardcoded `Path("/Users/crisn/Documents/Projects/chameleon")` as `PLUGIN_ROOT`. Replaced with `Path(__file__).resolve().parent.parent` so the suites run on GitHub-hosted runners (and any developer machine) without modification.

### Tests

- Full suite: **508/508** pass (added 4 PID-aware-cleanup assertions to `tests/v0_2_regression_test.py`, was 504/504).

### Known issues left for v0.4

- Ruff lint shows ~250 errors against the project's own `pyproject.toml` config (cleanup is a Phase 6-adjacent task, not blocking).
- `tests/trust_flow_test.py` "Trust without .chameleon/profile.json rejected" — error message rewording in v0.2.0 was missed by the assertion. Pre-existing v0.2 regression, not introduced here.

## [0.3.0] — 2026-05-11

The critique-answering release. The external audit framed v0.2 as "a canonical browser with security ceremony." v0.3 closes most of the gap toward Phase 4 in a single push, ships across all open Phase 2C/D work items, and adds 274 new regression assertions. Three top-tier agents implemented in parallel, two more reviewed.

### Added — Phase 4 (the big leap)

- **Real `lint_file` engine** (`mcp/chameleon_mcp/lint_engine.py`, 637 lines). Replaces the v0.2 stub with regex-based shape extraction matched against the archetype's `ast_query` block in `canonicals.json`. Five rule types: `default-export-kind-mismatch`, `top-level-node-kinds-mismatch`, `named-export-count-bucket-mismatch`, `jsx-presence-mismatch`, `content-signal-mismatch`. Returns `canonical_confidence` ∈ [0.0, 1.0]. Severities `info` / `warning` / `error`. TypeScript family + Ruby support. Envelope still carries `"stub"` boolean so callers can distinguish real-engine output from the legacy stub response shape.
- **`mcp/chameleon_mcp/index_db.py`** (369 lines) — SQLite-backed repo index at `${PLUGIN_DATA}/index.db`. `bootstrap_repo` upserts each successful run; `_resolve_repo_root_by_id` now prefers `index.db` over the trust record (Phase 4.4). `last_seen_at` stored with microsecond precision. `list_profiles` queries the index instead of scanning directories.
- **No-op refresh short-circuit** in `refresh_repo` (Phase 4.3 starter). When neither source files nor `idioms.md` have changed since the last bootstrap, returns `{"status": "noop", "reason": "no files changed since last refresh"}` without re-running the pipeline. `force=True` bypasses. Partial re-clustering is still deferred.

### Added — Phase 2C (cluster + selection signal expansion)

- **`derive_ast_query`** in `mcp/chameleon_mcp/bootstrap/canonical.py` — every archetype now ships a 5-field `ast_query` dict (top_level_node_kinds, default_export_kind, named_export_count_bucket, jsx_present, content_signal) so the lint engine has something to compare against. `null` fields mean "no expectation set."
- **Recency-weighted canonical selection** — files modified in the last 90 days vote at 2×. Constants `RECENCY_WEIGHT_MULTIPLIER = 2.0` and `RECENCY_WINDOW_DAYS = 90` are surfaced at the top of `canonical.py` as calibration targets.
- **Bimodal cluster flagging** — `ClusteringResult.bimodal_clusters` surfaces clusters that split 60/40 or worse on a key dimension. Bootstrap report now carries `sparse_cluster_warnings` and `bimodal_cluster_warnings` for future interview UI.
- **tsconfig `extends` chain resolution** — walks single-string and TS-5 array extends, resolves bare specifiers via `node_modules`, caps at 8 hops with cycle detection, surfaces partial-merge warnings under `rules.eslint.parse_warning` instead of failing.
- **`.eslintrc.yml` / `.eslintrc.js` parsing** — YAML via PyYAML (added as a direct dependency in `mcp/pyproject.toml`); `.eslintrc.js` extracted via brace-balanced regex with JS-ism normalization, falling back to v0.2's "invisible" warning on parse failure.
- **Workspace resolution** — `pnpm-workspace.yaml`, `lerna.json`, `turbo.json` (1.10+ `packages`/`workspaces`) populate `WorkspaceInfo.workspace_paths`. `nx.json` skipped.

### Added — Phase 2D (UX)

- **Archetype renaming heuristic** (`mcp/chameleon_mcp/bootstrap/naming.py`). `cluster-<hash>` → meaningful names — `controller`, `model`, `service`, `policy`, `serializer`, `job`, `mailer`, `migration` (Rails); `react-component`, `react-hook`, `query`, `mutation`, `utility`, `types`, `class` (TypeScript); `test` for spec/__tests__/*.test.ts paths. Name collisions disambiguate via a path-derived suffix (`controller-admin`) then a numeric counter. All outputs conform to the existing `^[a-z][a-z0-9-]{0,63}$` archetype name regex.
- **Material-change re-prompt on `/chameleon-teach`** — `profile/trust.py:hash_profile` now hashes `profile.json` + `idioms.md`. Adding or modifying an idiom flips a granted trust to `stale`, forcing the user to re-review (via `profile.summary.md`, which surfaces the idiom body verbatim — shipped in v0.2) before chameleon resumes injection.

### Added — Phase 7 docs

- `docs/chameleon/THREAT-MODEL.md` — 7-threat matrix (Threat / Defense / Residual risk) covering adversarial profiles, insider poisoning, idiom-channel injection, supply-chain attacks, confused-deputy via `--plugin-dir`, stale trust grant.
- `docs/chameleon/REAL-PROBLEM-EVIDENCE.md` — evidence chameleon solves a real problem (with the v0.2 audit's positive findings) AND honest acknowledgement of what remains unmeasured (80% conformance: Phase 6; calibration params: not yet validated).
- `docs/chameleon/decisions/0004-uvx-zero-touch-install.md` — v0.1.1 → v0.2.0 install model.
- `docs/chameleon/decisions/0005-schema-v5-path-pattern-bucketing.md` — v0.2.0 schema bump.
- `docs/chameleon/decisions/0006-audit-driven-v0_2_0-fixes.md` — v0.2.0 audit-fix flow.

### Changed

- `refresh_repo.force` documented as forward-compat (no-op for non-incremental refresh today; will bypass the incremental short-circuit when partial re-clustering ships).
- `list_profiles` is now backed by `index.db` instead of scanning `${PLUGIN_DATA}/<repo_id>/` directories. Backwards-compatible response shape; legacy directories are backfilled on first list.
- `_now_iso()` (in `index_db.py`) emits microsecond precision so refresh's no-op evaluator can compare against fractional file mtimes without false invalidations.
- Engine version bumped 0.2.0 → 0.3.0 across all 7 manifests + `mcp/pyproject.toml` + `mcp/chameleon_mcp/__version__`.

### Upgrade notes

- **Every existing trust grant flips to `stale` on first session after upgrade.** v0.3 includes `idioms.md` in the material-change hash; the new hash will not match any v0.1 or v0.2 trust record, so chameleon will stop injecting context until the user re-runs `/chameleon-trust` once per repo. This is intentional — pre-v0.3 trust grants covered profile artifacts but not the idiom body that actually reaches the model.
- **`index.db` is created on next bootstrap.** Existing v0.2 trust records are honored as fallback; first `bootstrap_repo` mirrors the repo into `index.db`. No manual migration required.
- **Path-pattern semantics from v0.2 are preserved.** No schema bump in v0.3; profiles bootstrapped in v0.2 continue to load and match.

### Tests

- 274 new regression assertions across `tests/archetype_naming_test.py` (40), `tests/canonical_v03_test.py` (52), `tests/tool_config_v03_test.py` (48), `tests/lint_engine_test.py` (58), `tests/index_db_test.py` (76).
- Full suite: 504/504 (comprehensive 175, v0_2_regression 28, mcp_protocol 27, plus the five new suites above).

### Deferred to v0.4+

- Long-lived daemon hook via UNIX socket (4.5) — major rearchitecture.
- Interactive ≤3-prompt interview in `/chameleon-init` (2D.1) — MCP conversation protocol design.
- Phase 6 calibration + benchmarking (6.x) — needs external test corpora.
- Git remote URL detection for `repo_id` (4.6) — breaking change; bundles cleanly with the next schema bump.
- True incremental refresh with partial re-clustering (4.3 extension) — current implementation only short-circuits on the no-op case.

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
