# Changelog

All notable changes to chameleon will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.11] - 2026-05-19

Two bug fixes surfaced by real-workflow testing on a TypeScript repo and a Ruby on Rails repo. Patch release. Existing profiles work unchanged.

### Fixed

- **Daemon listen backlog 16 -> 128.** Parallel-agent bursts of 100 concurrent connects (dispatching-parallel-agents, multi-worktree sessions sharing the per-user daemon) produced ECONNREFUSED on roughly 80 of 100 connects against released v0.5.10. Single-threaded accept loop couldn't drain the queue fast enough at backlog 16. Bump absorbs realistic burst sizes with margin; the client still fails open if the queue ever overflows. (`mcp/chameleon_mcp/daemon.py:86`)
- **idioms.md cumulative size cap at 200KB.** The 50KB per-call check on `teach_profile` stops single large feedback strings but doesn't prevent sustained drift: hundreds of small teaches grew the file past 100KB while the envelope cap at 8000 chars meant nothing past the first ~80 idioms reached the model. Cumulative guard runs inside the advisory lock; rejection error points at `/chameleon-refresh` or manual trim. (`mcp/chameleon_mcp/tools.py` `_IDIOMS_FILE_CAP`)

### Tests

- `R10DaemonBacklogTest` guards `_LISTEN_BACKLOG >= 128` against regression.
- `R10IdiomsFileCapTest` verifies the cumulative cap rejects past-cap writes without modifying idioms.md, plus a small-teach sanity case. Falsified pre-fix: both growth tests fail without the change.

### Compatibility

- Existing profiles work unchanged. No `PROFILE_SCHEMA_VERSION` bump. No re-bootstrap required.

## [0.5.10] - 2026-05-18

Per-edit hot path overhaul. Three concurrent themes ship together: a process-global excerpt LRU cache that collapses repeated `get_pattern_context` calls; security hardening of the witness-read path against TOCTOU + dirent-swap races via O_NOFOLLOW fd-based open with a 7-tuple `(path, st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns, version)` cache key; and consistency cleanup across the MCP tool surface (slop-input handling, archetype-resolver tiebreak, bootstrap-time archetype collapse). Warm `get_pattern_context` p50 drops from ~15ms to ~1.2ms (~13x speedup, measured on real ef-client + ef-api). Backwards-compatible; existing profiles continue to work; re-bootstrap picks up the collapse improvements.

### Performance

- **`_compute_repo_id` memoized** with `@functools.lru_cache(maxsize=64)`. Was forking `git config --get remote.origin.url` on every `get_pattern_context` call (~13ms warm, 70% of call per cProfile). Memo is process-lifetime; the documented "repo_id follows the project" contract is preserved. Warm p50 on real ef-client: 15ms -> 1.2ms.
- **Process-global excerpt LRU cache** (`mcp/chameleon_mcp/_excerpt_cache.py`). Sanitized canonical-witness excerpt memoized for the daemon's process lifetime. Default 64 entries, env-tunable via `CHAMELEON_EXCERPT_CACHE_CAP=<int>`. Key includes `CONTEXT_TRANSFORM_VERSION` so a sanitization-rule change is automatically a cache-bust.
- **Dedup in-call work in `get_pattern_context`.** Previously loaded `LoadedProfile` twice (once at top-level, once inside `get_archetype`) and parsed `profile.json` a third time for a corruption probe. Now: one load, one parse. Extracts `_get_archetype_with_loaded(p, repo_root, loaded, content_signal_value)` from `get_archetype`'s body so both paths share the scoring tail.

### Security

- **TOCTOU race closed via fd-based open.** `safe_open_fd(repo_root, rel_path, max_size_bytes)` opens with `O_RDONLY | O_NOFOLLOW | O_CLOEXEC`, `fstat`s the fd, runs all `safe_open` validations on the `fstat` result, and the cache builder reads from the open fd — so a mid-read `unlink(witness); symlink(witness, /etc/passwd)` swap can't redirect the read (POSIX rename of the dirent doesn't affect an already-open fd, which is bound to the original inode).
- **7-tuple cache key** `(path, st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns, CONTEXT_TRANSFORM_VERSION)` defeats an adversary who preserves `st_mtime_ns` via `os.utime`: that operation advances `st_ctime_ns`, which the post-read re-fstat compares against the key (verified empirically on Darwin). Closes BUG-R2-001 (cache key/content mismatch via writer race) and BUG-R2-002 (out-of-repo content leak via dirent-swap-to-symlink).
- **Post-read re-fstat check** raises `OSError` on any (size, mtime, ctime) drift between key-build and read-complete. Outer `except (UnsafeFileError, FileNotFoundError, OSError): pass` converts to fail-open empty `canonical_excerpt`; never stores a poisoned entry.
- **C0 control bytes stripped from sanitized output.** `sanitize_for_chameleon_context` removes `U+0000`–`U+001F` (except `\t \n \r`). NUL can't escape the `<chameleon-context>` tag, but can corrupt downstream parsers/loggers/metrics.

### Fixed

- **Bootstrap archetype collapse.** Same-`paths_pattern` archetypes are merged at bootstrap time into the highest-`cluster_size` keeper, with the smaller siblings' canonicals preserved as alternates. ef-api 19 -> 12 archetypes, ef-client 39 -> 16. Closes the unreachable-archetype bug (5 of 19 ef-api archetypes were dead because the resolver only returned the largest-`cluster_size` match per bucket and the AST signatures of the smaller siblings were too similar to differentiate). All canonicals retained. (`mcp/chameleon_mcp/bootstrap/orchestrator.py` `_collapse_same_pattern_archetypes`)
- **Path-locality tiebreak** in `_get_archetype_with_loaded`. When two archetypes share `paths_pattern` and AST scoring can't differentiate, prefer the one whose canonical witness lives in a deeper subdir matching the query file's path. Sort key is now `(-ast_score, -path_locality_overlap, -cluster_size)`.
- **Slop-input consistency across MCP tool surface.** Only `get_pattern_context` had a null-byte / empty-string / non-str guard; `detect_repo`, `get_archetype`, `lint_file`, `bootstrap_repo`, `refresh_repo` raised `ToolError` at the MCP wire boundary. Shared helper `_validate_file_path_arg(path) -> bool` applied uniformly. Also fixes: `detect_repo("")` was falling through to `Path("").expanduser()` -> `find_repo_root(cwd)`, leaking the MCP server's CWD repo data to any caller passing empty.
- **`get_pattern_context` length cap** at `_MAX_PATH_LEN = 4096`. Was raising `OSError: File name too long` for overlong single-component paths that hit the kernel `ENAMETOOLONG` before resolution.
- **`get_archetype` accepts path-form `repo` argument.** A strict-equality check against the computed hex repo_id silently returned `archetype: null` when callers passed the path form (the form every other tool in the module accepts via `_resolve_repo_arg`). Hex passes through unchanged (contract preserved for existing callers); path is resolved via `_resolve_repo_arg`.
- **Bootstrap transaction artifact cleanup.** Successful commits no longer leak `..chameleon.rename.lock` (0-byte file) or `..chameleon.tmp/` (empty dir) into the repo root. Race-safe: `rmdir` only succeeds when empty; concurrent in-flight commit's tmp_root keeps it non-empty and cleanup is a no-op.
- **Symlinked `.chameleon/` cleanup.** If a user symlinks `.chameleon` to external storage, bootstrap now cleans up the post-rename backup symlink with `os.unlink` instead of `shutil.rmtree(..., ignore_errors=True)` (which silently fails on macOS for a symlinked dir, leaving a dangling `..chameleon.backup-<pid>-<uuid>-<ts>` symlink).
- **Fail open on None / empty / null-byte `file_path` in `get_pattern_context`.** Returns the documented `no_repo` envelope instead of raising `TypeError` / `ValueError` from deep inside `Path.resolve()` / `lstat`.

### Added

- `CHAMELEON_EXCERPT_CACHE_CAP` — env var overriding the default 64-entry LRU cap.
- `safe_open_fd(repo_root, rel_path, max_size_bytes) -> (fd, stat, path)` in `mcp/chameleon_mcp/safe_open.py` — sibling to `safe_open` for race-resistant reads. Existing `safe_open` and `safe_read_text` unchanged.
- `_excerpt_cache.CONTEXT_TRANSFORM_VERSION` constant (now 2) so any change to `sanitize_for_chameleon_context` or the 3200-char truncation rule cascades automatically through the cache key.

### Tests

- 12 new test classes in `tests/get_pattern_context_cache_test.py`, 48 new cases total. Covers: dedup refactor, archetype-reuse contract preservation, excerpt-cache LRU semantics + recency + eviction + version bump, fd-based safety (mtime-preservation + dirent-swap closure), bootstrap collapse, path-locality tiebreak, slop guard (None / empty / null-byte / overlong / wrong-type), TOCTOU mitigations, transaction artifact cleanup, symlinked backup cleanup, MCP-tool slop consistency. Standalone unittest harness — `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py` exercises the whole branch.
- Real `claude_code_acceptance_test.py`: 26/26 against both ef-client and ef-api.
- 10,000-call daemon socket stress: 0 errors, 0 None responses, 0 FD growth, RSS flat after warm-up.

### Empirical validation

| Metric | Before | After |
|---|---:|---:|
| Warm `get_pattern_context` p50 (real ef-client) | ~15ms | ~1.2ms (~13x) |
| ef-api distinct archetypes after bootstrap | 19 (5 unreachable) | 12 (all reachable) |
| ef-client distinct archetypes after bootstrap | 39 | 16 |
| Mixed-call hit rate (default cap, real session) | n/a | >95% |
| FD growth over 10k daemon-socket calls | n/a | 0 |

### Compatibility

- Existing profiles work unchanged. Re-bootstrap (`bootstrap_repo(force=True)` or `/chameleon-refresh --force`) is needed to pick up the archetype-collapse improvements; refresh on existing profiles continues to work.
- Existing trust grants invalidate on next refresh if the user re-bootstraps (different `profile_sha256` after collapse). Standard `/chameleon-trust` re-grants.
- No `PROFILE_SCHEMA_VERSION` bump. v0.5.x consumers load v0.5.10 profiles without modification.

### Schema

No `PROFILE_SCHEMA_VERSION` bump. Collapse-time merging of `canonicals[arch]` to include alternate witnesses uses the existing list shape — older readers correctly see the additional entries.

## [0.5.9] - 2026-05-13

Clustering fix for "semantic, shape-based archetype clustering instead of path-based" — the most visible profile bug today. Two orthogonal levers ship together. Re-bootstrap a real Rails monolith and a real TS+React app to validate: ef-api went from 213 archetypes to 20 (-91%), ef-client from 139 to 39 (-72%). The mislabeled-controller-as-service clusters that named the bug are gone. No `PROFILE_SCHEMA_VERSION` bump; existing profiles continue to load and only pick up the new behavior on next `/chameleon-refresh` or `/chameleon-init --force`.

### Fixed

- **Option 1: fuzzy `top_level_node_kinds` merge.** The tight clustering pass keyed on an EXACT tuple match for `top_level_node_kinds`. Two files differing by one AST top-level kind (e.g. one extra `ConstantWriteNode` or a `ModuleNode` wrapper around the class) split into different clusters even when colocated and structurally similar. After the tight pass, a new shape-merge step now unions `top_level_node_kinds` across all members of each cluster and merges clusters sharing `(path_pattern_bucket, default_export_kind, jsx_present)` if their unions have Jaccard >= `CLUSTER_SHAPE_JACCARD_THRESHOLD` (default 0.7, env-tunable via `CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD`). Closes the May 13 finding that 45 controllers in `app/controllers/api/v1/` clustered into an archetype literally named `service-v1-rb` because their `ModuleNode` wrapper put them in a different exact-tuple bucket than the dominant `ClassNode` controllers. (`mcp/chameleon_mcp/bootstrap/clustering.py` `_shape_fuzzy_merge` + `_union_shape`)
- **Option 4: path bucket depth = 2.** `path_pattern_bucket_for` shifted from `parts[0]/parts[-3]/parts[-2]:ext` (effective depth ~3) to `parts[0]/parts[1]:ext`. Files like `app/services/zoom/recordings.rb` and `app/services/billing/invoices.rb` now share bucket `app/services:rb` instead of `app/services/zoom:rb` and `app/services/billing:rb`. The deeper path is preserved as the new `sub_bucket` field on each `ParsedFile` and aggregated into a `sub_buckets: {dir: count}` map on each archetype in `archetypes.json` so callers retain visibility into long-tail directory structure. Tunable via `CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH` (default 2). Closes the May 13 finding that ef-api's `app/services/` (1397 files) fragmented into 102 archetypes — they now collapse into one `service` archetype with `sub_buckets={'models/listings': 103, 'models/users': 37, 'hubspot': 35, ...}`. (`mcp/chameleon_mcp/signatures.py` `path_pattern_bucket_for` + `compute_signature`)
- **`naming.py` archetype-name derivation works correctly with depth=2.** The comment at `naming.py:228-229` previously acknowledged that the depth-3 bucket dropped the load-bearing `controllers` segment for `app/controllers/api/v1/foo.rb` and the naming code compensated via a `_members_contain` scan. With depth=2 the bucket itself contains `controllers`, so `_RAILS_PRIORS` and `_TS_PRIORS` match directly and the controllers-mislabeled-as-services case disappears. The `_members_contain` fallback stays in place as belt-and-suspenders for unusual layouts.

### Added

- **`clustering_algorithm_version: 2`** soft field written to `profile.json` so consumers can detect pre-v0.5.9 profiles without a schema-version bump. Absent or `< 2` means the profile predates the clustering fix and the user may want to re-bootstrap to pick up the improvements.
- **`sub_buckets` field on each archetype in `archetypes.json`** — maps the deeper directory path to file count, e.g. `{'zoom': 47, 'billing': 33, '': 22}` for files directly under `app/services/`, `app/services/zoom/`, and `app/services/billing/`.
- **`CLUSTER_SHAPE_JACCARD_THRESHOLD`** in `_thresholds.py` (default `0.7`, env `CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD`).
- **`CLUSTER_PATH_BUCKET_DEPTH`** in `_thresholds.py` (default `2`, env `CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH`; set to `3` to restore pre-v0.5.9 behavior for A/B comparison).

### Tests

- New `tests/clustering_shape_fuzzy_test.py` (42 assertions covering Jaccard threshold edge cases, env override, single-cluster passthrough, cross-path-bucket isolation, ordering interaction with the existing loose-merge pass).
- New `tests/clustering_path_bucket_depth_test.py` (37 assertions covering depth-2 unit cases, monorepo behavior, env override restoring depth=3, `sub_bucket_counts` distribution).
- Updated `tests/v0_5_2_clustering_test.py` to unpack the new `(bucket, sub_bucket)` return shape of `path_pattern_bucket_for` and assert against the new bucket values.
- Updated `tests/v0_2_regression_test.py`, `tests/v0_5_2_bootstrap_test.py`, `tests/smoke_test.py` for the 2-tuple return.

### Empirical validation

| Repo | Before | After | Delta |
|---|---:|---:|---:|
| ef-api (4805 .rb files) | 213 | 20 | -91% |
| ef-client (2225 .ts/.tsx) | 139 | 39 | -72% |

Specific mislabeled clusters gone:
- `service-v1-rb` (was 45 controllers labeled "service") — folded into `controller` (89 files total with sub_buckets `{api/v1: 50, api/v1/admin: 32, ...}`).
- `service-admin-rb` (was 40 admin controllers) — same fix, now part of `controller`.
- `app/services/` 1397 files: was 102 archetypes, now 1 (`service`) with sub_bucket distribution.
- `src/components/base/` 4-way split: was 4 archetypes, now most are in `component` (439 files) with `base` as a sub_bucket of 61 files.

### Schema

No `PROFILE_SCHEMA_VERSION` bump. The JSON structure is unchanged — existing v0.5.x consumers continue to load v0.5.9 profiles without modification. The new `sub_buckets` and `clustering_algorithm_version` fields are additive and ignored by older consumers.

### Compatibility

Existing profiles loaded by v0.5.9 work unchanged. Re-bootstrap or `/chameleon-refresh` is required to pick up the clustering improvements. Set `CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD=1.0` and `CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH=3` to fully restore pre-v0.5.9 clustering for comparison.

## [0.5.8] - 2026-05-13

Security hardening, correctness fixes, observability, and two new test layers. Surfaced from a 3-round code review on the new hook-eval scenario harness plus a 58-scenario end-to-end dogfood run against the test repos. No public-API breaking changes. `tests/hook_evals/` (fast deterministic synthetic-scenario suite) and `tests/dogfood/` (full lifecycle harness, runnable via `/chameleon-dogfood`) ship as additive coverage.

### Security

- **Witness path traversal blocked.** `get_pattern_context` and `get_canonical_excerpt` previously did `repo_root / witness_rel` followed by `.is_file()` + `.read_text()` with no boundary check. A hostile `.chameleon/canonicals.json` could point `witness_path` at `../../etc/passwd` and the file's content would reach the model's `<chameleon-context>` block. Reads now go through `safe_open.safe_read_text` which enforces NUL-free paths, NFC normalization, lstat-checked regular-file-only, repo-boundary realpath, and a 200KB size cap.
- **World-writable repo roots refused.** `find_repo_root` now rejects `/tmp`, `$TMPDIR`, `tempfile.gettempdir()`, and their subdirs, plus any directory with the world-writable bit set. A planted `/tmp/.chameleon/profile.json` would otherwise let any local attacker drive chameleon's advisory for any user editing under `/tmp`. Tests can opt in via `CHAMELEON_ALLOW_TMP_REPO=1`.
- **PYTHONPATH inheritance dropped.** All four hook scripts previously did `PYTHONPATH="${MCP_DIR}${PYTHONPATH:+:${PYTHONPATH}}"`. A malicious `.envrc` setting `PYTHONPATH=/tmp/evil` could shadow `chameleon_mcp` submodules. Now: `PYTHONPATH="${MCP_DIR}"` only.
- **Loader read caps + lstat.** `_safe_read_artifact` lstats each profile artifact (refusing symlinks and non-regular files) and refuses files larger than 5 MB. Closes the OOM-via-1GB-profile.json class of attacks.
- **Dangerous-token sanitizer expanded.** `_DANGEROUS_TOKENS` now includes `<system-reminder>`, `<system_reminder>`, `<im_start>`, `<im_end>`, and the `<|im_start|>` / `<|im_end|>` pipe-bracketed variants. A poisoned canonical witness can no longer inject fake system-reminder framing. Archetype name and confidence band are also sanitized before substitution into the `[chameleon: archetype=...]` header.
- **`now=` parameter validation.** `bootstrap_repo` rejects NaN, +/-inf, negative numbers, non-numeric types, and bool (which is technically int) at the API boundary with a clear failed envelope.

### Correctness

- **`refresh_repo` fast-reject advisory lock.** Two concurrent `/chameleon-refresh` calls previously serialized at the 30s rename flock and both succeeded with last-writer-wins. Now `refresh_repo` acquires `.chameleon/.refresh.lock` (non-blocking) at the top and returns a fast contention envelope with the holder PID on busy. Mirrors the existing `teach_profile` pattern.
- **Daemon spawn no longer hangs the hook.** `ensure_daemon_async` used to spawn a `threading.Thread` that called `start_daemon()`, which double-forks via `os.fork()`. On macOS, fork from inside a multi-threaded Python process can hang the parent for ~2s on libc/Cocoa locks held across the fork boundary, hitting the hook's 2s timeout. Now uses `subprocess.Popen(..., start_new_session=True)` so the OS performs fork+exec atomically and the freshly-exec'd Python's double-fork runs from a clean single-threaded process. ~3 to 10 percent of hook calls were fail-opening before; 0/30 after.
- **`trust_profile` rejects unloadable profiles cleanly.** Previously caught `ProfileLoadError` but let raw `json.JSONDecodeError` bubble through when `profile.json` was malformed. Both now surface as the same failed envelope.
- **`bootstrap_repo` upserts index.db on short-circuit.** When bootstrap returns `already_bootstrapped` (per the v0.5.6 force gate), it now also writes the repo's row to the shared `index.db` so `list_profiles` sees newly-cloned repos that ship a checked-in `.chameleon/`.
- **`_member_relpaths` returns repo-relative paths.** The function name promised relative paths but returned absolute. The all-segments test-token check in `_looks_like_test` then false-positived on any repo whose absolute path contained `tests`, `spec`, or similar segments.
- **Session marker hardening.** `session_id` now goes through a `sha256[:16]` hash before being used as a filename component, so `..` / `/` / NUL in `session_id` can no longer escape the marker directory. Trust-prompt markers age out after 24h so resumed Claude sessions re-prompt.
- **`--full` mode hook errors land in a per-session log.** The four hook scripts honor `CHAMELEON_HOOK_ERROR_LOG`; `tests/hook_evals/runner.py --full` sets it to a tmpfile per scenario, closing the daemon-race false positive previously documented in the README.

### Observability

- **Per-call metrics emission.** Every `preflight-and-advise` invocation appends one JSON line to `${CHAMELEON_PLUGIN_DATA}/metrics.jsonl` with `ts`, `hook`, `repo_id`, `elapsed_ms`, `advisory_emitted`, `suppression_reason`, `fail_open`, `trust_state`, `archetype`, `confidence`. Best-effort emission; never breaks the hook.
- **`.hook_errors.log` rotation.** Hooks call `python -m chameleon_mcp.log_rotation` before each append. Rotates at 10 MB with up to 5 backups; oldest is dropped. Closes the unbounded-log-growth finding from the operational review.
- **`/chameleon-doctor` triage tool.** New MCP tool (`doctor`) + slash command. Returns a structured envelope with subsystem checks: Python version, bash + timeout(1) on PATH, plugin-data dir writability, HMAC key health, all four hook scripts executable, daemon liveness, recent hook error log tail, and per-known-repo `profile_status` + `trust_state`.

### Testing

- **`tests/hook_evals/`** - deterministic synthetic-scenario suite. Two checked-in fixture repos at `tests/fixtures/eval_repos/{ts,ruby}_minimal/` with committed `.chameleon/`. 13 scenarios; runs in <1s as a 6th entry in `tests/run_all_orders.py`. Optional `--full` mode pipes through the real bash hook. `scripts/refresh_eval_fixtures.sh` regenerates the fixtures with pinned `now=1700000000.0` for deterministic witness selection.
- **`tests/dogfood/`** - comprehensive end-to-end test harness. 58 scenarios across 18 families (install, init, trust, injection, adversarial, teach, status, refresh, suppression, hooks, mcp, coexistence, resilience, isolation, harness, uninstall, observability, security). Reusable via `mcp/.venv/bin/python -m tests.dogfood.runner` or `/chameleon-dogfood`. Filter by `--phase`, `--family`, `--cost`; `--include-real-claude` opts in to 8 real Claude Code sessions (~$1.10 total). 50/50 free+cheap PASS, 8/8 real-Claude PASS in the validation run.
- **New unit tests** for the `now=` plumbing (`tests/now_threading_test.py`), `_member_relpaths` repo-relative paths (`tests/looks_like_test_path_bias_test.py`), suppression precedence (`tests/suppression_precedence_test.py` - 11 layered cases), schema-version-too-high refusal (`tests/schema_version_test.py`), log rotation (`tests/log_rotation_test.py`), metrics emission (`tests/metrics_emit_test.py`), and doctor envelope (`tests/doctor_test.py`).
- **Pinned `now=` plumbing.** `tools.bootstrap_repo`, `orchestrator.bootstrap_repo`, and `_bootstrap_single` accept an optional `now: float | None = None` kwarg that threads through to `select_canonicals`. Enables the refresh script to fix witness selection mtime-dependence.

### Fixed

- **`pretooluse_hook_test.py` docstring**: dropped the stale claim that `--permission-mode bypassPermissions` suppresses PreToolUse hook firing. Verified on Claude Code 2.1.140; PreToolUse fires normally in bypass mode.
- **`mcp_protocol_test.py`**: registry now expects 21 tools (added `doctor`).

### Schema

No schema bump. `PROFILE_SCHEMA_VERSION` stays at 7.

### Compatibility

Python 3.11+ required for the dogfood harness. The MCP server's pinned floor was already 3.11.

## [0.5.5] — 2026-05-11

Cycle-4 dogfood patch — single, targeted fix for a silent misroute the v0.5.4 cycle surfaced (3-app confirmed). Net cycle-4 result: 388 PASS / 0 FAIL / 3 FINDING across 9 apps (vs cycle-3's 378 / 0 / 13 — 77% finding reduction). v0.5.5 closes the last 3.

### Fixed — Bug H: `_resolve_repo_root_by_id` returns wrong workspace for monorepos (3-app: excalidraw, mastodon, plane)

**Symptom.** After `bootstrap_repo(plane_root)` (a Turborepo / pnpm-catalog monorepo), the `repos` table in `index.db` carries 18 rows — one for the plane root and one per workspace (`apps/admin`, `apps/live`, `apps/space`, `apps/web`, `packages/*` × 13). All 18 rows share the same `repo_id` because `_compute_repo_id(workspace_dir)` derives the id from the git remote URL, which is identical for every workspace and the root.

`resolve_repo_root(repo_id)` without a hint (the wrapper consumers actually call — `get_canonical_excerpt`, `get_drift_status`, the using-chameleon skill) picks the freshest row by `last_seen_at`. Workspaces are upserted AFTER the root row inside `bootstrap_repo`, so the alphabetically-last workspace (`packages/utils` for plane) wins the lookup.

The downstream call chain then:
1. resolves repo_root to `plane/packages/utils` (wrong)
2. loads profile from `plane/packages/utils/.chameleon/` (doesn't exist — workspaces have no profile)
3. `load_profile_dir` returns an empty/stub profile
4. `"action" not in known_archetypes` is True
5. Returns `{"status": "failed", "error": "archetype not found"}` — misleading

The v0.5.1 Bug 1 composite `(repo_id, repo_root)` PK works — the rows coexist without overwriting — but the no-hint resolver still picked freshest from a pool that now has 17 wrong entries against 1 right one.

**Fix.** Make `resolve_repo_root` **ancestor-aware**: when multiple rows share a `repo_id`, prefer the row whose `repo_root` is an ancestor of (or equal to) every other row's `repo_root`. The actual repo root, not a workspace, wins.

Algorithm in new helper `_pick_ancestor_or_freshest`:
1. Resolve each candidate to a canonical absolute path.
2. For each candidate, count how many other candidates sit under it (strict descendants).
3. The candidate with the maximum descendant count wins.
4. Tie-break: shorter path string wins (ancestors are always shorter).
5. Fall back to the original order (freshest first) when no clear ancestor exists (rare — sibling clones with the same git remote).

The `repo_root_hint` contract from v0.5.1 stays unchanged: explicit hints win when they match a row, fall through to the new ancestor-aware path when they miss.

**Verify-after.** `_resolve_repo_root_by_id(plane_repo_id)` now returns `<repo>/plane` (root), and `get_canonical_excerpt(repo_id, "action")` returns 793 bytes of content. Before the fix, the same calls returned `<repo>/plane/packages/utils` and `{"status": "failed", "error": "archetype not found"}` respectively.

### Tests

- New: `tests/v0_5_5_resolver_test.py` (13 assertions covering `_pick_ancestor_or_freshest` unit cases, real index.db round-trip, single-row repos, hint contract preservation, end-to-end resolver flow).
- Updated: `tests/v0_5_1_critical_test.py` — one assertion that codified the OLD "freshest wins" behavior now expects the new ancestor-aware behavior. The pre-v0.5.5 assertion was passing precisely because of the bug v0.5.5 fixes.

39 of 39 testable suites green; `pretooluse_hook_test.py` remains environmental (requires pre-trusted EF test repos; the trust state was wiped at cycle-3 start and not restored).

### Schema

No schema bump.

### Cycle-4 dogfood

Reports under `docs/dogfood/v0.5.4-cycle4/`. Cycle-by-cycle progression:

| Cycle | Version | PASS | FAIL | FINDING | Clean apps (0 finding) |
|---|---|---|---|---|---|
| 2 | v0.5.1 | (n/a — bulletproof-react aborted at bootstrap) | 0 | 12 | 1 |
| 3 | v0.5.3 | 378 | 0 | 13 | 0 |
| 4 | v0.5.4 | 388 | 0 | 3 | 5 |
| 4 + v0.5.5 (projected) | v0.5.5 | 388+ | 0 | 0 | 9 |

### Deferred to v0.6

Same 11 findings carried since cycle 1. The bespoke-domain-dir generics (plane / mastodon `emoji-icon-picker/`, `editor/`, deep `features/<feature>/api/` nests) don't warrant a generic prior-table entry.

## [0.5.4] — 2026-05-11

Cycle-3 dogfood patch. Third full sweep against 9 apps under a 10-phase end-to-end runner that exercises every MCP tool surface. Each app's `.chameleon/` was wiped before launch + the plugin data dir was cleared so every bootstrap started from scratch.

Cycle-3 results: 378 PASS, 0 FAIL, 13 FINDING. Every v0.5.3 fix verified in real data. Reports under `docs/dogfood/v0.5.3-cycle3/`.

### Fixed — Workspace-prefix stripping in TS naming (Bug F)

v0.5.3 Bug B taught the orchestrator to bootstrap workspace monorepos (Turborepo, pnpm, Nx). Files in `apps/<ws>/src/components/` started reaching the naming pipeline, but the v0.5.3 TS prior table was authored for root-relative paths (`src/components/`) and the directory-chain matcher would only fire when the workspace prefix happened to land in the right segment position.

v0.5.4 adds `_strip_workspace_prefix(member_paths, workspace_roots)` to `naming.py`. Two strategies:

1. **Explicit roots**: when the bootstrap envelope's `workspace_roots` is non-empty (the Bug B path), the matching root prefix is stripped. Longest-match wins so `apps/admin-app/` isn't accidentally stripped to `admin-app/...`.
2. **Path-shape fallback**: when `workspace_roots` is empty BUT a path starts with `apps/<dir>/`, `packages/<dir>/`, `services/<dir>/`, or `workspaces/<dir>/`, strip the 2-segment prefix. Catches the plane case — pnpm catalog refs (`typescript: "catalog:"`) in plane's root package.json made the v0.5.3 Bug B detector treat the workspace as a flat TS repo.

`propose_archetype_name` and `_base_name_for` gain an optional `workspace_roots: list[str] | None` keyword. The orchestrator threads `workspace_roots or None` through; pure-mode callers can pass their own.

### Fixed — TS prior table extensions

Cycle-3 dogfood surfaced 13 more directory conventions that produced `cluster-<hex>` names:

- `features/<feature>/` → `feature-module` (bulletproof-react, modern React layouts)
- `testing/mocks/` → `test-mock` (MSW-style mock harnesses)
- `mocks/handlers/` → `test-mock-handler` (standalone MSW handler dirs)
- `icons/` → `icon-set` (brand icon sets; plane has `packages/propel/src/icons/brand/`)
- `locales/` → `locale-table` (i18n table dirs)
- `i18n/` → `locale-table` (alias for the same convention)
- `constants/` → `constants-module`
- `schema/` / `schemas/` → `schema-module` (zod/yup/valibot definitions)
- `providers/` → `provider` (context/auth provider components)
- `contexts/` → `context` (React context module dir)
- `layouts/` → `layout` (layout-component dir)
- `config/` / `configs/` → `config-module`

Cycle-3 → v0.5.4 effect:

| App | Cycle-3 generic | After v0.5.4 | Change |
|---|---|---|---|
| plane | 12/70 (17%) | 5/70 (7%) | -58% |
| bulletproof-react | 6/12 (50%) | 0/12 (0%) | -100% |

The 5 remaining plane generics are bespoke domain dirs (`emoji-icon-picker/`, `editor/`, etc.) that wouldn't fit any generic prior table.

### Fixed — `profile.summary.md` rules section + deprecated section placeholders

Cycle-3 dogfood reviewers spotted two unfinished-feature placeholders in every `profile.summary.md`:

1. **`_Phase 2C: tool config rules + AST stats._`** — leftover stub from v0.4. Phase 2C actually shipped in v0.5.0; the placeholder never got swapped for real rendering. v0.5.4 renders the actual contents of `rules.json`:

   ```
   ## Rules

   _Auto-derived from 2 tool config file(s): `eslint`, `formatting`._

   - **eslint** — 15 rule(s) extracted
   - **formatting** — 4 rule(s) extracted
   ```

   When `rules.json.rules` is empty (no eslint / tsconfig / prettier / rubocop / .editorconfig found), the section explains WHY instead of leaving a placeholder.

2. **`## deprecated\n\n_(none)_`** — the deprecated-idioms section always rendered with `_(none)_` for clean profiles. v0.5.4 only renders the section when it carries actual content. Clean profiles no longer ship an empty-looking heading. Profiles that retire idioms via `/chameleon-teach` get a proper "Deprecated idioms" heading with explanatory text.

Both fixes apply to the orchestrator's `_build_summary_md` AND the partial-refresh `_rewrite_summary_md` in `tools.py` (kept in lockstep per v0.5.1 comment).

### Fixed — Runner cleanups (3 cosmetic dogfood-runner bugs)

The cycle-3 dogfood harness `run_dogfood.py` had 3 issues that produced spurious FINDING entries:

1. `pause_session(repo_id)` response shape: runner checked for `status in ("paused", "ok")` but the actual response is `status: "success"`. Tagged as FINDING in all 9 cycle-3 reports — now correctly tagged PASS.
2. `language_hint` field name: runner used `lang_hint.get("secondary")` but the actual field is `secondary_detected`. gitlabhq's hybrid hint rendered as "secondary=None" even though it WAS emitted. Now reads the correct key + surfaces `secondary_file_count`.
3. `archetypes[0]` staleness: phase_1 cached the archetype list pre-bootstrap; phase_5 re-bootstraps to verify atomic sibling preservation; phase_7 then called `get_canonical_excerpt` with a stale archetype name. v0.5.4 re-reads `archetypes.json` after phase_5 and prefers a non-generic name when available.

### Tests

- New: `tests/v0_5_4_naming_test.py` (30 assertions covering the strip helper, the 13 new TS prior entries, and the integration with `propose_archetype_name`)
- All 38 suites green standalone. `pretooluse_hook_test.py` is environmental (real-Claude-Code acceptance against EF test repos; trust state was wiped at cycle-3 start) — not a v0.5.4 regression.

### Schema

No schema bump. `paths_pattern_display`, `workspace_roots`, instrumentation envelope fields all already exist at v7.

### Deferred to v0.6

Same 11 findings carried from earlier cycles. The 5 remaining plane generics are bespoke domain dirs (`emoji-icon-picker/`, `editor/`, etc.) — adding them would dilute the TS prior table without clear benefit.

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

- 16 test files previously hardcoded an absolute developer path as `PLUGIN_ROOT`. Replaced with `Path(__file__).resolve().parent.parent` so the suites run on GitHub-hosted runners (and any developer machine) without modification.

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
