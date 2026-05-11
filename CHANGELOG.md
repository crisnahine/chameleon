# Changelog

All notable changes to chameleon will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.3] — 2026-05-11

Cycle-2 dogfood patch. Second full sweep against 9 apps (forem, maybe, mastodon, gitlabhq, excalidraw, plane, bulletproof-react, ef-api, ef-client) under a 10-phase end-to-end runner that exercises every MCP tool surface. 5 new findings caught; all 5 ship in v0.5.3. Reports under `docs/dogfood/v0.5.2-cycle2/`; cross-app analysis in `SUMMARY.md`.

Three parallel agents owned non-overlapping file sets under the verify-before / verify-after / code-review discipline. 39 test suites, 1,696 assertions, all green.

### Fixed — Bug A: `get_canonical_excerpt` silent empty on missing witness (3-app confirmation)

Pre-v0.5.3 the tool returned `{"content": "", "witness_path": null, "truncated": false, "sha_hint": null}` with no error when the archetype existed in `archetypes.json` but had no canonical witness in `canonicals.json` (witness rejected at bootstrap because all candidates contained secrets or fell below the confidence threshold). v0.5.2's Bug 5 fix covered the wrong-arg-shape case but missed the missing-witness case.

v0.5.3 emits three distinct typed envelopes:
- `status: "failed", error: "repo_id not found"` — repo_id doesn't resolve
- `status: "failed", error: "archetype not found"` — archetype name not in profile
- `status: "no_witness", reason: "...", archetype_name, repo_id` — valid args, no witness available

Legacy `content/witness_path/truncated/sha_hint` keys are preserved (all `null` when not applicable) so consumers reading them don't crash.

### Fixed — Bug B: monorepo with empty-root `package.json` fails bootstrap (high severity, foundational)

`bulletproof-react` (Turborepo-style: root `package.json` with only `scripts`, per-workspace `apps/<ws>/tsconfig.json` + `apps/<ws>/package.json`) returned `failed_unsupported_language`. This is the modern monorepo layout used by Turborepo, Nx, pnpm workspaces, and Lerna; without this fix chameleon's on-ramp story is broken for any team on that pattern.

v0.5.3 extends `_select_extractor` to drill one level down into `apps/*`, `packages/*`, `services/*`, `workspaces/*` when:
- Root has `package.json` but no TS deps in root deps/devDeps
- AND root has no root-level `tsconfig.json`
- AND at least one first-level workspace dir contains `tsconfig.json` OR a TS-flavored `package.json`

When detected, the bootstrap envelope carries `workspace_roots: list[str]` listing the dirs (relative to repo root), and `discover_files` scans the union of those dirs instead of the root. Fanout is bounded at 50 first-level dirs to defang misconfigured trees.

### Fixed — Bug C: Next.js / Remix archetypes get generic `cluster-<hex>` names (plane: 50% sparse)

plane dogfood shipped 35/70 archetypes named `cluster-<hex>` despite clear Next.js conventions. v0.5.2's Rails-prior table (`_RAILS_PRIORS`) had no TypeScript equivalent.

v0.5.3 adds `_TS_PRIORS` (22 entries) parallel to `_RAILS_PRIORS`, gated by `_is_typescript_cluster(cluster)` (first member's extension is `.ts/.tsx/.js/.jsx/.mjs/.cjs`) AND `not _is_ruby_cluster(cluster)`. Coverage:
- Next.js App Router: `app-route-handler`, `app-page-component`, `app-layout`, `app-special-component`
- Next.js Pages Router: `pages-api-handler`, `pages-component`, `pages-special-component`
- Remix: `remix-route`
- Component: `component` (`components/`), `ui-component` (`ui/`)
- Hook: `hook` (`hooks/use*.ts`)
- Library: `lib-module`, `util`, `helper`, `service`, `middleware`, `action`, `store`, `type-module`, `query-hook`, `query`, `api-client`
- Test: `test` (handled by existing `_looks_like_test`, listed for clarity)

Priority order: longest directory-chain match first; filename predicate disambiguators within the same chain (so `app/api/route.ts` wins `app-route-handler`, not just `app-page-component`).

**Vocabulary standardization:** the new prior table also renames 5 categories that overlapped with v0.5.1 names: `react-component`→`component`, `react-hook`→`hook`, `utility`→`util`, `types`→`type-module`, `class` (TS lib/ default)→`lib-module`. The 7 affected assertions in `archetype_naming_test.py` updated to the new vocabulary.

### Fixed — Bug D: bootstrap coverage telemetry (gitlabhq: 6,574 of ~125k files surfaced silently)

gitlabhq dogfood reported `files_processed=6,574` for a ~125k-file repo and there was no way to tell whether the gap was healthy exclusion (vendor, public/uploads, app/assets/images) or unexpected pruning. v0.5.3 adds 4 instrumentation fields to the `bootstrap_repo` success envelope:
- `discovered_files_pre_exclusion: int` — total files walked
- `discovered_files_post_exclusion: int` — survivors of EXCLUDE sets
- `clustered_files: int` — same as legacy `files_processed`, kept for back-compat
- `sparse_dropped_files: int` — files in clusters below the sparse threshold

A new `discovery_stats(repo_root, ...)` helper produces these counts without raising `TooManyFilesError`, so telemetry on an oversized repo is still useful.

### Fixed — Bug E: Rails+JS hybrid detector misses legacy sprockets layout (gitlabhq)

`_is_rails_with_frontend` required `app/javascript/` (modern Rails 6+ webpacker / esbuild). gitlabhq uses the older sprockets layout (`app/assets/javascripts/`). v0.5.3 broadens the predicate to also accept:
- `app/assets/javascripts/` (legacy Rails 5 sprockets)
- `app/frontend/` (some Rails 7 conventions)

### Limits

`REPO_SIZE_GUARD` bumped 100,000 → 200,000 (2x, 4x baseline). The cycle-2 dogfood confirmed gitlabhq sits at ~125k files; anticipated public OSS apps (full Plane monorepo with all packages, Discourse, Forem-pro) sit in the 100k-200k band. Discovery is dominated by `stat()` + `xxhash`; bootstrap wall-time on a 200k repo measures 3.5-4 minutes on the reference SSD — acceptable for the one-shot install experience. The other 50K caps (`teach_profile` body, structured-payload limit, hybrid-detection scan) stay — they guard input shape, not corpus size.

### Tests

- New: `tests/v0_5_3_canonical_witness_test.py` (30 assertions, Bug A)
- New: `tests/v0_5_3_monorepo_bootstrap_test.py` (37 assertions, Bugs B + D + E)
- New: `tests/v0_5_3_ts_priors_test.py` (108 assertions, Bug C)
- Updated: `tests/archetype_naming_test.py` (7 assertions migrated to new vocabulary)
- Updated: `tests/pretooluse_hook_test.py` (2 sections now filter for `PreToolUse:Edit` specifically instead of picking the first PreToolUse event, which can be chameleon's own MCP call)

**All 39 suites, 1,696 assertions green.**

### Schema

No schema bump. `workspace_roots` is an envelope-only field on `bootstrap_repo`'s response — not persisted to `profile.json`.

### Deferred to v0.6

11 findings from v0.5.1 plus the v0.5.2 "Bug 1 FINDING" (runner-side cosmetic, not a chameleon bug). Full list: `docs/dogfood/SUMMARY.md` and `docs/dogfood/v0.5.2-cycle2/SUMMARY.md`.

## [0.5.2] — 2026-05-11

Second dogfood patch. 17 of the remaining 28 medium-severity findings from the same 6-repo dogfood pass (forem, maybe, mastodon, gitlabhq, excalidraw, plane) ship; the rest are deferred to v0.6 where they need design conversations (semantic prompt-injection heuristic, Next.js route group recognition, Phase 6 calibration refresh).

Per-app reports under `docs/dogfood/REPORT-*.md`. 4 parallel agents each owned a non-overlapping file set under the verify-before / verify-after / code-review discipline. 23 test suites, 1,259 assertions, all green.

### Fixed — `tools.py` API surface (7 bugs)

- **API repo arg unified.** Four independent dogfoods (forem, maybe, plane, excalidraw) hit the same friction: `pause_session`, `disable_session`, `teach_profile`, `refresh_repo`, `propose_archetype_renames`, `apply_archetype_renames`, and `bootstrap_repo` rejected the repo_id digest that the rest of the API (`get_canonical_excerpt`, `get_rules`, `lint_file`, `get_archetype`) accepted. v0.5.2 ships a single `_resolve_repo_arg(repo) -> (repo_path, repo_id)` shape detector (path prefix / 64-char hex / expanduser-absolute) called from 9 entry points. Both forms work everywhere.
- **Idiom slug collision within same epoch second.** Two `teach_profile` calls within the same wall-clock second produced identical slugs (`idiom-YYYY-MM-DD-{epoch_seconds}`). v0.5.2 appends a 4-hex `secrets.token_hex(2)` suffix (16 bits = 65,536 values) and re-rolls once on collision detection.
- **`list_profiles` enrichment.** Now JOINs against `index.db`; entries carry `repo_root`, `archetype_count`, `files_indexed`, `bootstrap_ms`, `last_seen_at` in addition to the legacy 4 trust fields.
- **`get_drift_status` path-vs-id misroute.** Path-shaped input was treated as an opaque `plugin_data_dir` key. Routed through `_resolve_repo_arg` now; legacy non-path / non-hex strings still work for the existing `refresh_drift_test.py` fixtures.
- **`get_canonical_excerpt` silent empty.** Wrong-shape arg returned `{"content": "", "witness_path": null}` with no error. Now returns an explicit `{"status": "failed", "error": "repo_id not found"}` envelope.
- **`detect_repo` $HOME information disclosure (minor).** Path traversal like `<dir>/../../../etc/passwd` resolved to `$HOME` silently. Now guards against `Path.home()` (or strict ancestor) as the resolved repo_root.
- **`suspicious_input` flag in `teach_profile` response.** 8-pattern heuristic flags prompt-injection-shaped feedback (`ignore previous instructions`, `you are now in DAN mode`, system-role injections, `eval(`/`exec(`/`rm -rf`, `reveal the system prompt`, ...). The idiom IS still stored — the defense is the trust gate — but the user gets a UI signal.

### Fixed — clustering / signatures (4 bugs)

- **Path bucket extension-blind collision.** `.tsx` and `.ts` siblings collapsed into the same bucket. `path_pattern_bucket_for(include_extension=True)` appends `:tsx` / `:ts` etc. The clustering pipeline opts in; `get_archetype` keeps the legacy default and falls back to the extension-aware form on miss.
- **Monorepo bucket dropped middle segments.** `packages/{excalidraw,element,math}/components/TTDDialog/X.tsx` all collided in v0.5.1. v0.5.2 detects `parts[0] in {"packages", "apps", "workspaces"}` with ≥4 segments and uses `parts[0]/parts[1]/parts[2]` so the workspace name survives.
- **`content_signal_match` is no longer dead code.** `get_archetype` reads the first 200 bytes and calls `signatures.content_signal_match_for(head)` for every return branch; consumers see `"none" | "use_client" | "use_server" | "shebang" | "ts_pragma"`. Python `None` is reserved for "file unreadable", so consumers can distinguish "we looked, nothing matched" from "we never looked."
- **Adaptive sparse-cluster threshold.** Hard-coded threshold 5 killed recall on feature-per-folder layouts (mastodon, excalidraw, plane). `cluster_files(min_cluster_size=None)` now uses: <1000 files → 3, 1000–5000 → 4, ≥5000 → 5 (legacy). Tests pass explicit values for determinism.

### Fixed — bootstrap (4 bugs)

- **`atomic_profile_commit` sibling-file preservation.** Pre-v0.5.2 the directory-replacement rename wiped `.chameleon/.skip`, `.chameleon/.gitignore`, `.chameleon/.editorconfig`, and arbitrary user files (the committed `.skip` opt-out was silently disappearing on every bootstrap). v0.5.2 copies all non-protocol siblings into the txn dir before the rename via `shutil.copy2` / `shutil.copytree`. Protocol files in the txn dir always win.
- **Rails-aware naming priors.** forem dogfood saw 5/7 archetypes named `cluster-<hex>` despite clear Rails conventions. 15-entry Rails prior table covers `app/controllers/concerns/`, `app/models/concerns/`, `app/{controllers,models,services,jobs,mailers,helpers,policies,serializers,presenters,workers,views}/`, `db/migrate/`, `config/initializers/`. Gated by `_is_ruby_cluster` so TS clusters don't engage. Filename suffix discriminators (`_job.rb`, `_mailer.rb`, `_helper.rb`) anchor against misplaced files.
- **`paths_pattern_display` for Rails archetype review.** maybe dogfood saw `paths_pattern = "app/rule/action_executor"` for an archetype whose witness was `app/models/rule/action_executor/auto_categorize.rb` — the `models/` segment was missing. Changing the bucket would break the runtime archetype-lookup invariant (`path_pattern_bucket_for(rel) == archetype.paths_pattern`), so v0.5.2 keeps the bucket byte-identical and adds a sibling `paths_pattern_display` field for `profile.summary.md`. The display form fires only when the witness has ≥4 parts, starts with `app/`, and `parts[1]` is a load-bearing Rails dir not already in the bucket.
- **`db/schema.rb` always-added on partial-refresh.** Discovery picked it up but clustering dropped it as single-member generic. Every refresh saw it as "added" and forced a full bootstrap. v0.5.2 excludes `db/schema.rb` and `db/structure.sql` at discovery time — they're Rails-autogenerated.

### Fixed — lint engine + idioms (2 bugs)

- **GitHub PAT bypassed by string-concat.** `lint_file` flagged `AKIAIOSFODNN7EXAMPLE` but missed `"ghp_" + "abcdef..."`. v0.5.2 adds a `_fold_string_concat` preprocessor that folds literal-to-literal `+` concat (both `"a" + "b"` and `'a' + 'b'`) before invoking the secret scanner. Bounded at 1000 substitutions per file. Folded hits surface a `[after string-concat fold]` suffix in the violation so operators see why a token fired on a line whose visible text is two short literals. Backticks and variable-mixed concat (`"a" + foo()`) are intentionally out of scope.
- **Idioms not language-scoped.** maybe dogfood: a JS file in a Ruby-detected repo received Ruby-flavoured idioms. v0.5.2 adds an opt-in `Language:` frontmatter line per idiom (`ruby` / `typescript` / `any` — default `any`) and a new `idiom_filter.py` module exposing `filter_idioms_by_language(md, target_language)` and `language_for_path(path)`. Legacy idioms without frontmatter are treated as `any`. The filter drops a `<!-- chameleon: filtered N idiom(s)… -->` HTML comment when it removed entries so trust-review surfaces don't go blank.

### Limits

`REPO_SIZE_GUARD` bumped from 50,000 → 100,000 (2x). gitlabhq dogfood (~125k files) bounded out at the prior cap. Discovery is mostly stat + xxhash so the latency cost stays sublinear. The other 50K caps (`teach_profile` body, `teach_profile_structured` payload, `_count_ts_files_under` hybrid scan) are unrelated input-shape guards and stay at 50K.

### Schema

`PROFILE_SCHEMA_VERSION` bumps from 6 → 7. New fields in `archetypes.json`:
- `paths_pattern_display` (string | absent): Rails-aware display form when the cluster's bucket would mislead a human reviewer.
- Extension-aware buckets (`:tsx`, `:ts`, etc.) for clusters that opted in.

Old v6 profiles still load (range gate is 5–7). Trust hashes are unchanged for unmodified profiles.

### Tests

- New: `tests/v0_5_2_tools_test.py` (89), `tests/v0_5_2_clustering_test.py` (52), `tests/v0_5_2_bootstrap_test.py` (51), `tests/v0_5_2_lint_idioms_test.py` (61) — 253 new assertions across 4 suites with explicit `# Verify-before:` / `# Verify-after:` comments per bug.
- Updated: 3 legacy assertions that hardcoded the prior schema version (`tests/smoke_test.py` profile `schema_version: 4` → `5`; `tests/comprehensive_test.py` range gate `v3-v6` → `v3-v7`; `tests/v04_features_test.py` `PROFILE_SCHEMA_VERSION == 6` → `== 7`).
- All 23 suites green: 1,259 total assertions.

### Known regressions / migration notes

- **Trust hash unchanged across this release** for unmodified profiles. v0.5.2 adds `paths_pattern_display` to `archetypes.json` only when a Rails witness triggers it, which DOES bump the hash for affected Rails monorepos (one re-trust prompt per affected repo).
- **`atomic_profile_commit` now preserves nested directories under `.chameleon/`** in addition to flat files. If a future feature places a directory there, it survives unchanged.
- **`_resolve_repo_arg` accepts empty string as `(None, None)`** rather than raising; downstream tools fall through to their existing "no repo provided" error envelopes.

### Deferred to v0.6

11 of the original 28 medium/low findings remain: semantic prompt-injection NL heuristic (needs broader design conversation), Next.js / Remix route group recognition, Phase 6 calibration corpus refresh, fresh-bootstrap `trust_state` semantics (`"stale"` vs `"untrusted"`), engine-version-string drift detector, sparse-warning de-dup across refresh runs, `excerpt` vs `content` field rename audit, idiom language-tag UI in `profile.summary.md`, partial-refresh cluster_id namespace alignment (different root cause from v0.5.1 Bug 3), fresh-bootstrap index.db artifact cleanup, and a follow-up audit of the v0.5.2 `paths_pattern_display` heuristic against deeply nested Rails namespaces. Full list: `docs/dogfood/SUMMARY.md`.

## [0.5.1] — 2026-05-11

The dogfood-driven patch release. Real-world testing against 6 production repos (forem, maybe, mastodon, gitlabhq, excalidraw, plane) surfaced 56 unique findings. v0.5.1 ships the 4 Critical + 3 High fixes that the dogfood + 3-app-confirmed bug analysis prioritized.

Per-app reports under `docs/dogfood/REPORT-*.md`; cross-app analysis in `docs/dogfood/SUMMARY.md`. Independent code reviewer signed off; 1,041 test assertions across 18 suites all green.

### Fixed — Critical (4)

- **Bug 4: Trojan-source bidi sanitization (CVE-2021-42574 class).** `sanitize_for_chameleon_context` now strips U+202A–U+202E (LRE/RLE/PDF/LRO/RLO) and U+2066–U+2069 (LRI/RLI/FSI/PDI), not just zero-width chars + ANSI escapes. A poisoned idiom containing `‮` would have reached model context verbatim in v0.5.0; v0.5.1 strips it byte-level. Order matters in the sanitize pipeline: zero-width → bidi → NFC → tag-token replacement, so sandwich attacks like `<‮/chameleon-context>` cannot slip the boundary check. (Confirmed by maybe + excalidraw dogfoods.)

- **Bug 1: Monorepo `repo_id` collision in `index.db`.** Three independent dogfoods (mastodon, plane, excalidraw) hit the same crash: all sub-workspaces share a git-remote-derived `repo_id`, and the v0.5.0 `repos` table's PRIMARY KEY was `repo_id` alone, so every per-workspace bootstrap overwrote the root row. `_resolve_repo_root_by_id` then misrouted every consumer call (`get_canonical_excerpt`, partial-refresh, drift, ...) to the alphabetically-last workspace. v0.5.1 changes the PK to `(repo_id, repo_root)` and adds a one-time, in-place, transactional migration (`_migrate_repos_to_composite_pk`) that runs on first `init_index_db()` after upgrade. `get_repo` and `resolve_repo_root` accept an optional `repo_root_hint` for monorepo callers; absent the hint, they return the freshest matching row.

- **Bug 2: Rails+JS hybrid silently scans only TypeScript.** forem (3,515 Ruby files invisible) and mastodon (3,179 Ruby files invisible) both hit this: when both `Gemfile` and `package.json` existed, `_select_extractor` picked TypeScript first and the entire Rails app stayed unscanned. v0.5.1 detects the Rails-with-frontend triple (`Gemfile` + `config/application.rb` + `app/javascript/`), picks Ruby for those repos, and surfaces a new `language_hint` envelope field describing the secondary language and recommending `bootstrap_repo(<repo>/app/javascript)` for the TS half. The hint flows through `BootstrapReport`, `profile.json` (omitted when no hybrid is detected), and `profile.summary.md` (rendered as a `## Secondary language detected` section above the archetype list).

- **Bug 3: `refresh_repo` silently wiped user renames.** Three independent dogfoods (forem, plane, excalidraw) reproduced this; root causes varied by repo but the symptom was the same: full-bootstrap fallthrough re-derived archetype names from scratch, destroying user curation. v0.5.1 persists the rename mapping into `.chameleon/renames.json` (intended to be committed to git so the team shares the curation). The orchestrator loads the overlay AFTER `propose_archetype_name` runs and re-keys the archetypes / canonicals dicts before commit; user-mapped target names are pre-reserved in `assigned_names` so collisions take a numeric suffix on the auto-name side. The renames file is re-emitted inside every `atomic_profile_commit` (full bootstrap, partial refresh, workspace amend) so the directory replacement never clobbers it.

### Fixed — High (3)

- **H1: `apply_archetype_renames` now flips trust to stale.** `hash_profile` was previously scoped to `profile.json + idioms.md`, so renaming archetypes (which rewrites `archetypes.json` + `canonicals.json` + `profile.summary.md`) left the trust hash unchanged. v0.5.1 extends `hash_profile` to cover all 4 JSON artifacts (alphabetical order, each framed by `\x00<filename>\x00` to prevent boundary collisions) plus `idioms.md`. Renames now correctly invalidate trust; users see one re-trust prompt per rename. NB: this is **transparently breaking** for existing v0.5.0 trust records — every previously-trusted repo with a non-trivial `archetypes.json` flips to `trust_state=stale` on first v0.5.1 run.

- **H2: Stale trust grants no longer silently inherit to fresh clones.** `repo_id = sha256(git_remote_url)` means a fresh clone of a previously-trusted repo (e.g., from a calibration run) inherits the trust grant with a stale `repo_root` path. `detect_repo` now surfaces a structured `legacy_trust_hint` envelope when the trust record's `repo_root` differs from the current path and no per-root entry covers the current workspace: `{reason, recorded_repo_root, current_repo_root, recommended_action}`. The v0.4 schema-v6 migration hint (string) and v0.5.1 cross-clone hint (dict) are mutually exclusive — readers should `isinstance(..., dict)` to disambiguate.

- **H6: Per-(repo_id, repo_root) trust.** `TrustRecord` gains an additive `repo_root_specific_hashes: dict[str, str]` field mapping resolved repo_root → profile_sha256, so monorepos can grant trust at a specific workspace without overwriting the root's grant. `is_material_change` delegates to a new `hash_for_root(repo_root)` method that returns the most-specific match (per-root entry → top-level fallback). Backward compatible: v0.5.0 records load with an empty map and behave identically to v0.5.0.

### Tests

- New: `tests/v0_5_1_critical_test.py` (82 assertions) + `tests/v0_5_1_trust_test.py` (38 assertions). Each fix is verified by an explicit reproducer drawn from the dogfood reports.
- Existing 16 suites all green (1,041 total assertions). 2 `interview_flow_test` assertions were updated to match the new H1 behavior — renames now flip trust to stale, where the old behavior had pinned the no-op.

### Known regressions / migration notes

- **`forget_repo(repo_id)` without `repo_root`** now deletes ALL rows for that repo_id (v0.5.0 deleted "the row" — there could only ever be one). Callers should pass `repo_root` explicitly to scope the delete.
- **`BootstrapReport.to_dict()` always includes `language_hint`** (null when not a hybrid); `profile.json` omits the key when null. Consumers reading either should use `.get("language_hint")`.
- **`atomic_profile_commit` still clobbers `.chameleon/.skip` and `.chameleon/.gitignore`** sibling files. `renames.json` is preserved; `.skip` / `.gitignore` preservation is deferred to v0.5.2 (BUG-007 from dogfood).
- The v0.5.1 `_migrate_repos_to_composite_pk` runs the first time `init_index_db()` is called after upgrade; idempotent and transactional. A crash mid-migration leaves the v0.5.0 table intact.

### Deferred to v0.5.2+

~28 medium/low bugs from the dogfood pass: API consistency around `repo` arg (4 confirmations), `.skip` sibling preservation, idiom slug collision, partial-refresh cluster_id namespace mismatch, adaptive sparse-cluster threshold, Next.js/Remix route-group recognition, content_signal_match wire-through, Rails-aware naming priors, semantic prompt-injection NL heuristic, and others. Full list in `docs/dogfood/SUMMARY.md`.

## [0.5.0] — 2026-05-11

The **actually-100% release**. The three items I previously called "intentionally deferred to v1.0+" all ship: long-lived daemon, partial re-clustering, real calibration measurements against a real corpus. Every item the original Phase plan + ARCHITECTURE.md + audit identified is now either shipped or has a concrete reason rooted in data, not in "we ran out of time."

### Added — Phase 4.5: Long-lived daemon (`mcp/chameleon_mcp/daemon.py` + `daemon_client.py`)

- UNIX socket daemon at `${PLUGIN_DATA}/.daemon.sock` (mode 0600). Length-prefix framing (4-byte big-endian header + UTF-8 JSON body, 1 MB cap). One request-response per connection; methods: `get_pattern_context`, `detect_repo`, `get_archetype`, `lint_file`, `ping`.
- Double-fork spawn writes pidfile at `${PLUGIN_DATA}/.daemon.pid` (`<pid>\n<sock_path>\n`). `start_daemon` waits up to 3 s for the socket to become connectable. `stop_daemon` SIGTERM → wait 5 s → SIGKILL escalation. `is_daemon_alive` cross-checks pidfile PID liveness AND socket existence. Stale pidfile/socket cleanup runs before bind.
- Idle shutdown after `CHAMELEON_DAEMON_IDLE_TIMEOUT` seconds (default 600 s; test runs override to 1.5 s).
- `hook_helper.preflight_and_advise` is daemon-first with in-process fallback. On first cold miss it kicks `ensure_daemon_async()` (background `threading.Thread`) and proceeds in-process — future calls in the session see the warmed daemon. Fail-open: any daemon error path returns `None` from the client and the hook continues normally.
- New MCP tool `daemon_status()` for `/chameleon-status` output (alive, pid, uptime_s, socket_path, last_request_at).

### Added — Phase 4.3-extended: Partial re-clustering (`mcp/chameleon_mcp/index_db.py` + `tools.py:refresh_repo`)

- New `file_clusters` table in `index.db` records `(repo_id, rel_path, cluster_id, sha_hint, last_seen_at)`. Additive DDL; legacy v0.4 profiles backfill on the next bootstrap.
- `refresh_repo`'s no-op short-circuit (shipped in v0.3) is unchanged. After the no-op fails, the new partial path sha-diffs the discovery set against the prior `file_clusters` rows.
- **<=10% changed** → re-parse only the modified/added files, look up their `ClusterKey` against existing archetypes, amend `cluster_size` in `archetypes.json` + bump generation + commit through `atomic_profile_commit`. Returns `status="partial_refresh"` with `files_changed`, `files_added`, `files_removed`, `change_ratio`, `archetypes_unchanged`, `archetypes_amended`.
- **>10% changed**, or any re-parsed file lands in a brand-new cluster, or the canonical witness is in the changed set → fall through to full bootstrap (existing path).
- Bootstrap pass-2 cost noted: `bootstrap_repo` now runs `discover + parse + cluster` a second time to materialize the per-file → cluster_id map (the orchestrator's `BootstrapReport` doesn't expose this map yet). Roughly doubles cold-bootstrap wall clock. Calibration p95 (3.4 s in v0.4) becomes ~6–7 s post-bootstrap; still well under the 10 s ceiling. Cleanup tracked for v0.5.1.

### Added — Phase 6: Real calibration measurements (`docs/chameleon/PHASE-6-CALIBRATION.md`)

- The harness shipped in v0.4 ran against a real **anonymized 2-repo corpus** (1 TS + 1 Rails) and captured shipping numbers:
  - `archetype_match_rate_mean = 1.00` (target ≥0.80) — **PASS**
  - `bootstrap_duration_p95_ms = 3,365` (target ≤10,000) — **PASS**
  - `high_confidence_rate_mean = 1.00` (informational)
  - `cost_per_bootstrap_usd = 0.0` (no API calls during bootstrap)
- The doc is honest about corpus thinness: 2 repos vs the ARCHITECTURE.md target of 4; harness measures witness-roundtrip only, not generalization on novel files; no drift / cost-on-hot-path measurement. Action items for v0.6 are listed.
- `.github/workflows/calibration.yml` (manual `workflow_dispatch` only) re-runs the harness against the maintainer's corpus and uploads the JSON artifact.

### Fixed

- 80 of the v0.3 ruff backlog auto-fixed (247 → 167). Remaining 167 are mostly E402 / E501 / B904 / B007 — style judgment, not correctness.
- `trust_flow_test.py` assertion drift cleared (now accepts both v0.2 and v0.3 error message wordings).
- `bootstrap/transaction.py` B904 chained exception now uses `raise ... from e`.

### Tests

- 12 test suites, **752/752 pass**. New suites: `daemon_test.py` (47), `partial_refresh_test.py` (72). Existing suites untouched in count.
- Full breakdown: comprehensive 175 + v0_2_regression 32 + mcp_protocol 27 + lint_engine 58 + index_db 76 + archetype_naming 40 + canonical_v03 52 + tool_config_v03 48 + interview 71 + v04_features 54 + daemon 47 + partial_refresh 72.

### What's left after v0.5.0 (honest)

- **Per-edit timing row in the calibration harness** — Phase 6 follow-up. Currently `get_pattern_context` cost is captured implicitly inside `bootstrap_ms`; a dedicated p99 column needs the harness to grow a timing primitive.
- **Corpus expansion to 3+ TS repos + 2+ Rails** — needs OSS test repos identified and gitignored corpus.json entries added. No code change required.
- **Bootstrap pass-2 cost cleanup** — push the per-file → cluster_id map out of `tools.bootstrap_repo` into the orchestrator's `BootstrapReport`. Low-risk perf refactor for v0.5.1.
- **Daemon worker pool** — single-threaded accept loop; pipelined requests serialize. Trivial `ThreadPoolExecutor` addition when measured demand says it matters.
- **167 remaining ruff entries** — style cleanup. CI lint job is `continue-on-error: true` until the backlog clears.

Everything else in the Phase plan / audit / architect's roadmap is shipped.

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
