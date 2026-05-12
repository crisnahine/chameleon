# Chameleon test audit — 2026-05-12

Scope: every Python module + hook script listed below, mapped to GOOD / BAD / EDGE / GAP / SLOP scenarios per public function and cross-referenced against the 73 test files in `tests/`.

Focus files (LOC):
- `mcp/chameleon_mcp/tools.py` (3531)
- `mcp/chameleon_mcp/bootstrap/orchestrator.py` (1842)
- `mcp/chameleon_mcp/bootstrap/tool_config.py` (936)
- `mcp/chameleon_mcp/profile/trust.py` (318)
- `mcp/chameleon_mcp/profile/loader.py` (243)
- `mcp/chameleon_mcp/bootstrap/transaction.py` (242)
- `mcp/chameleon_mcp/drift/observations.py` (224)
- `mcp/chameleon_mcp/hook_helper.py` (446)
- `hooks/*` (bash scripts)

Test-suite roster used for cross-reference (73 files):
`adversarial_profile_test.py, all_commands_acceptance_test.py, archetype_naming_test.py, bootstrap_mechanism_test.py, canonical_v03_test.py, claude_code_acceptance_test.py, cold_start_init_test.py, comprehensive_test.py, daemon_stress_test.py, daemon_test.py, drift_concurrent_writes_test.py, find_repo_root_test.py, git_merge_driver_test.py, hmac_key_edge_cases_test.py, index_db_test.py, interview_flow_test.py, interview_test.py, lint_engine_test.py, material_change_test.py, mcp_protocol_test.py, optouts_test.py, partial_refresh_test.py, pretooluse_hook_test.py, refresh_drift_test.py, smoke_test.py, stress_50k_test.py, stubs_implemented_test.py, teach_roundtrip_test.py, tool_config_v03_test.py, trust_flow_test.py, v04_features_test.py, v0_2_regression_test.py, v0_5_1_critical_test.py, v0_5_1_trust_test.py, v0_5_2_bootstrap_test.py, v0_5_2_clustering_test.py, v0_5_2_lint_idioms_test.py, v0_5_2_tools_test.py, v0_5_3_canonical_witness_test.py, v0_5_3_monorepo_bootstrap_test.py, v0_5_3_ts_priors_test.py, v0_5_4_naming_test.py, v0_5_5_resolver_test.py, v0_5_6_archetype_prefix_fallback_test.py, v0_5_6_archetypes_sum_test.py, v0_5_6_bootstrap_force_test.py, v0_5_6_corrupted_profile_test.py, v0_5_6_discovery_hints_test.py, v0_5_6_engine_version_test.py, v0_5_6_eslint_cjs_test.py, v0_5_6_eslint_flat_test.py, v0_5_6_language_hint_misdetect_test.py, v0_5_6_loose_merge_test.py, v0_5_6_preflight_trust_gate_test.py, v0_5_6_rails_with_frontend_test.py, v0_5_6_rename_candidates_test.py, v0_5_6_rename_disambiguation_test.py, v0_5_6_rubocop_extractor_test.py, v0_5_6_schema_version_test.py, v0_5_6_sidecar_bootstrap_test.py, v0_5_6_sparse_warnings_cap_test.py, v0_5_6_trust_repo_id_test.py, v0_5_6_trust_state_test.py, v0_5_6_ts_signal_fallback_test.py, v0_5_6_unified_status_test.py, v0_5_7_repo_root_resolution_test.py, v0_5_7_rubocop_extraction_test.py, v0_5_7_schema_version_detect_test.py, v0_5_7_sidecar_bootstrap_test.py, v0_5_7_tsconfig_workspace_alias_test.py, v0_5_7_workspace_tool_configs_test.py`

---

## mcp/chameleon_mcp/tools.py

### `_envelope(data, truncated=False, next_cursor=None)` at line 22

**Existing tests:** indirect via every test that calls a public MCP tool (`comprehensive_test.py`, `mcp_protocol_test.py`).

**GOOD:**
- Returns `{"api_version": "1", "data": {...}}` for empty `data`.
- Carries `truncated: True` only when explicitly requested.
- Carries `next_cursor` value when supplied.

**BAD:**
- `data=None` (not a dict) — module makes no defensive type check.
- `truncated="yes"` (truthy non-bool) — silently passes through.

**EDGE:**
- `next_cursor=""` (empty string) — included in envelope per `if next_cursor is not None`.
- Very large `data` dict (e.g., 10 MB serialized) — no upper bound on envelope size.

**GAP (untested):**
- No direct unit test for `_envelope` shape stability (api_version key, key order).
- No test that `truncated=False` and `next_cursor=None` produce exactly `{"api_version", "data"}` (i.e., no spurious keys).

**SLOP:**
- `api_version` is hardcoded `"1"` — no test confirms callers can rely on this; a string vs int regression would be silent.

---

### `_resolve_repo_arg(repo)` at line 55

**Existing tests:** indirect via `trust_flow_test.py`, `v0_5_1_trust_test.py`, `v0_5_6_trust_repo_id_test.py`, `comprehensive_test.py`, `stubs_implemented_test.py` (all use both path + repo_id shapes).

**GOOD:**
- Absolute path string for existing dir returns `(resolved_path, repo_id)`.
- 64-char lowercase hex returns `(resolved_path_or_None, repo_id_unchanged)`.
- `~/`-prefixed path expands via `expanduser`.
- `./`-prefixed path detected as pathy but rejected (not absolute after expanduser).
- `../`-prefixed path detected as pathy but rejected.

**BAD:**
- Non-string `repo` (int, bytes, None) returns `(None, None)`.
- Empty string `""` returns `(None, None)`.
- 63-char hex (one short) drops through to path check, fails (no leading `/`), returns `(None, None)`.
- 65-char hex (one long) same path.
- Uppercase hex `[0-9A-F]{64}` — regex `^[0-9a-f]{64}$` rejects; falls through.
- Hex with non-hex char (`g`) — falls through to path check.
- Path with embedded null byte — `Path(...)` may raise ValueError.
- Path that produces OSError on `expanduser` (no `$HOME`).

**EDGE:**
- 64-char hex that also happens to look path-shaped (impossible because of leading-`/` check).
- Absolute path to a regular file (not dir) — returns `(path, None)` per "directory doesn't exist" branch.
- Symlinked dir — `resolve()` follows; tests don't cover broken symlinks at root.
- Windows-style `C:\foo` on macOS — `is_absolute()` is False on POSIX; falls through to `(None, None)`.
- Path that resolves to `/` — accepted as dir; computes repo_id of `/`.

**GAP (untested):**
- No direct unit test enumerating each return-tuple shape — coverage is incidental.
- No test for the `OSError on resolve()` branch (line 115-116) producing `(path, repo_id_via_unresolved)`.
- No test for `_compute_repo_id` throwing arbitrary `Exception` (line 119) returning `(path, None)`.
- No tests using a 64-char string that contains all-valid hex but the corresponding `_resolve_repo_root_by_id` returns None — confirm `(None, repo_id)` shape.
- No test that uppercased hex (`A-F`) is rejected.

**SLOP:**
- "Path-shape detection" doc lists exactly four prefixes but code doesn't test against `\\` (Windows UNC) — undefined behavior on Windows clients.
- Two-branch fall-through (path → hex → expanduser) can be confusing to audit; no test pins the order semantics.

---

### `_normalize_git_url(url)` at line 136

**Existing tests:** none directly; covered indirectly via `_compute_repo_id` via bootstrap tests.

**GOOD:**
- `git@github.com:user/repo.git` → `https://github.com/user/repo`.
- `https://github.com/user/repo.git` → `https://github.com/user/repo`.
- `ssh://git@github.com/user/repo` → `https://github.com/user/repo` (case-insensitive host folded).
- `https://GitHub.com/USER/Repo` → `https://github.com/USER/Repo` (host lowercased, path preserved).
- `https://api.internal.example.com/repo.git` → not in well-known hosts; scheme + host preserved as-is.
- Bitbucket SSH: `git@bitbucket.org:team/repo.git` → `https://bitbucket.org/team/repo`.
- Azure DevOps both case-insensitive hosts present (`dev.azure.com`, `ssh.dev.azure.com`).

**BAD:**
- `None` input — early-return after `(url or "").strip()` yields `""`.
- Empty whitespace `"   "` → returns `""`.
- Non-URL garbage `"not-a-url"` → returns garbage stripped (function is total).
- URL with internal whitespace `https://foo .com/x` → regex doesn't reject; returns the broken URL.

**EDGE:**
- SSH scp-form with `.git/` (trailing slash + .git) — covered by `.git/?$` regex.
- URL with port `https://github.com:443/user/repo` → port falls into `host` group; not handled.
- IPv6 host `https://[::1]/x` → `proto_match` regex's host class `[^/]+` accepts brackets; host_l fold lowercases nothing.
- User@host with `@` mid-host (`git@user@gitlab/foo`) — splits at first `@`.
- Trailing whitespace inside URL `https://github.com/x \n` — only outer `strip()`; embedded newline preserved.
- Unicode in URL host (IDN punycode) — no normalization.

**GAP (untested):**
- No test of `_normalize_git_url("")` returning `""`.
- No assertion that two different scheme-equivalent URLs hash to the same repo_id (i.e., the regression-protective end-to-end test).
- No test for port-bearing URL.
- No coverage for SSH with custom user (`deploy@git.example.com:repo.git`).
- No test for the well-known host `https://www.github.com/x` (subdomain `www`) — function does NOT fold this since `www.github.com` isn't in the set.

**SLOP:**
- "Function is total" guarantee not asserted; a future regex tweak could regress to a raise without anyone noticing.
- `_CASE_INSENSITIVE_HOSTS` covers 5 hosts; no test enforcing the list (e.g., to catch accidental removal).

---

### `_git_remote_url(repo_root)` at line 193

**Existing tests:** `comprehensive_test.py` (incidental via bootstrap), `find_repo_root_test.py`.

**GOOD:**
- Returns origin URL for a repo with `git config remote.origin.url=https://...`.
- Returns None for a directory that's not a git repo.
- Returns None when origin is configured but empty string.

**BAD:**
- Repo at a path that contains shell-metacharacters — passed via list args so safe; no test confirms.
- Path doesn't exist — git returns non-zero; function returns None.

**EDGE:**
- 2-second timeout boundary — function catches `TimeoutExpired`; no test exercises a hung git command.
- `FileNotFoundError` if `git` binary missing — caught; returns None.
- `OSError` (permission to spawn) — caught.
- Non-UTF8 bytes in remote URL — `text=True` may raise; no test.

**GAP (untested):**
- No test simulating git binary absent (covered only in env where git is installed).
- No test for the timeout branch (would need fault injection).
- No test for an origin URL with surrounding whitespace (trim works) vs internal newlines (kept).
- No test for `--get` returning multiple lines (multi-remote).

**SLOP:**
- 2-second timeout is documented in code but no test confirms.
- Hardcoded `remote.origin.url` excludes upstreams configured as `upstream` or `origin2` — not a bug, but undocumented.

---

### `_compute_repo_id(repo_root)` at line 216

**Existing tests:** `find_repo_root_test.py`, `v0_5_1_trust_test.py`, `comprehensive_test.py`, `v0_5_6_trust_repo_id_test.py`.

**GOOD:**
- Path-derived id for non-git directory matches `_legacy_path_repo_id` exactly.
- Git-remote-derived id collapses `ssh://` and `https://` to the same hash.
- Different repo paths produce different ids (path fallback).
- Two checkouts of the same remote URL collapse to the same id.

**BAD:**
- `repo_root` doesn't exist — `resolve()` still works on macOS/Linux for missing paths; id is computed.
- Symlinked repo_root — `resolve()` collapses symlink; tests don't cover broken symlink loops.

**EDGE:**
- Repo whose origin URL is the literal string `"\n"` after git's strip — returned as None.
- Repo with origin URL longer than UTF-8 single line — no upper bound check.
- Resolved path containing non-UTF-8 byte sequences (would fail `.encode("utf-8")`) — `str(Path.resolve())` is `str` so always UTF-8.

**GAP (untested):**
- No test for cache-friendliness (function is pure but called repeatedly; no memoization).
- No test that the function is deterministic across re-resolved paths.
- No test that detects schema-v6+ vs v5 id divergence (the canonical → legacy migration boundary).

**SLOP:**
- Function may shell out to git on every call (no caching); hot-path implications unverified.

---

### `_legacy_path_repo_id(repo_root)` at line 236

**Existing tests:** indirectly via `comprehensive_test.py` and `trust_flow_test.py` migration paths.

**GOOD:**
- Returns 64-char sha256 hex digest for a real directory.
- Same input produces same output (deterministic).

**EDGE:**
- Differs from `_compute_repo_id` when a git remote exists.
- Identical to `_compute_repo_id` when no git remote.

**GAP (untested):**
- No test that pre-v0.4 repos (with cached legacy id) still resolve under the migration hint.
- No test confirming the function never falls back to git inspection (it is path-only by contract).

**SLOP:**
- One-line wrapper; no docstring example of when callers should prefer this over `_compute_repo_id`.

---

### `detect_repo(file_path)` at line 246

**Existing tests:** `comprehensive_test.py` (`detect_repo: profile_present`...), `v0_5_6_corrupted_profile_test.py`, `v0_5_7_schema_version_detect_test.py`, `v0_5_6_trust_state_test.py`, `find_repo_root_test.py`, `v0_5_1_trust_test.py`, `mcp_protocol_test.py`.

**GOOD:**
- File inside a profiled, trusted repo → `profile_status="profile_present", trust_state="trusted"`.
- File inside profiled, untrusted repo → `trust_state="untrusted"`.
- File inside profiled repo with stale trust → `trust_state="stale"`.
- File outside any repo → `profile_status="no_repo", trust_state="n/a"`.
- File in repo without `.chameleon/` → `profile_status="no_profile", trust_state="n/a"`.

**BAD:**
- `file_path` is None → `Path(None)` raises TypeError; no defensive guard.
- `file_path` is a bytes object → similar.
- Path contains null byte — Path constructor may raise.
- Path traversal payload `~/../../../etc/passwd` resolves to ancestor of $HOME → returns no_repo (BUG-006 defense).

**EDGE:**
- File path that resolves to `$HOME` itself → no_repo per line 308.
- File path that resolves to a strict ancestor of $HOME (`/Users`) → no_repo.
- File path resolves to the filesystem anchor `/` → no_repo via `resolved == Path(resolved.anchor)`.
- `home.resolve()` fails with OSError (very unusual) — `home = None` and check skipped; weird ancestor case proceeds.
- Profile present but `profile.json` unreadable as JSON → `profile_status="profile_corrupted", trust_state="n/a"`.
- Profile present, schema_version > MAX_SUPPORTED → `profile_status="profile_unsupported_schema_version"`.
- Legacy trust hint fires only when current trust is None AND legacy id has a record AND ids differ.
- Stale clone hint (dict form) fires only when stale AND recorded `repo_root` differs AND workspace lacks own grant.

**GAP (untested):**
- No test for `OSError` in the `home.resolve()` branch (line 304).
- No test for `Path.is_dir()` raising on permission error mid-walk.
- No regression for the suppression rule when `trust.repo_root_specific_hashes` contains the current resolved path (line 400-401).
- Symbolic link from `/private/var/...` → `/var/...` (macOS) — partial coverage in comprehensive_test but no targeted assertion.
- No test for legacy trust hint when `legacy_id == repo_id` (the path-only fallback case where no migration hint should fire).

**SLOP:**
- The two `legacy_trust_hint_value` forms (string vs dict) share one field; consumers must duck-type.
- `legacy_repo_id` only present in the string-hint branch; no schema doc.
- Error message at line 372 contains an ellipsis character `…` which is unusual in error messages (mostly harmless but undocumented).
- `profile_corrupted` and `profile_unsupported_schema_version` both set `trust_state="n/a"` even when a trust record exists — silent demotion may surprise callers.

---

### `_prefix_overlap_fallback(rel_str, archetypes)` at line 433

**Existing tests:** `v0_5_6_archetype_prefix_fallback_test.py` (BUG-015).

**GOOD:**
- File at `app/controllers/foo.rb` matches archetype with `paths_pattern="app/controllers/v1"` (overlap=2).
- File `src/components/Button.tsx` matches archetype `paths_pattern="src/components"` (overlap=2).
- Multiple archetypes overlap — longest-prefix wins; size breaks ties.

**BAD:**
- `archetypes` is empty dict → returns `(None, [])`.
- Archetype has no `paths_pattern` → skipped.
- All archetypes have unrelated paths → returns `(None, [])`.

**EDGE:**
- File at root (no `/`) — `file_dir=""`, `file_segments=[]`, returns `(None, [])`.
- File without extension and archetype with extension suffix — extension-filter mismatches; skipped.
- Archetype paths_pattern with no `:ext` suffix (legacy v5 form).
- Archetype paths_pattern starts with `/` (absolute-style — empty leading segment).
- Two archetypes with identical overlap and identical cluster_size — alphabetic tiebreak NOT enforced (uses insertion order via sort key tuple).

**GAP (untested):**
- No test for ext-suffix mismatch path-bucket vs file ext.
- No test exercising the `cluster_size` tiebreak when overlap is equal.
- No test for an archetype whose `paths_pattern` has more segments than the file (function still computes a prefix score but cluster_size determines).
- No test that the function works on Windows-style paths (uses `/` literal).

**SLOP:**
- Uses `zip(..., strict=False)` which silently truncates when arch_segments is longer than file_segments — could be surprising.
- Tiebreak by `cluster_size` then name alphabetic is implicit in the negative-tuple sort; not documented in docstring.

---

### `get_archetype(repo, file_path)` at line 479

**Existing tests:** `stubs_implemented_test.py`, `v0_5_2_lint_idioms_test.py`, `v0_5_3_canonical_witness_test.py`, `v0_5_2_tools_test.py`, `v0_5_6_archetype_prefix_fallback_test.py`, `comprehensive_test.py`.

**GOOD:**
- File inside profiled repo with exact path-bucket match → archetype name + high/medium confidence.
- File matches multiple buckets; AST scoring picks the closest.
- File with content_signal `"use client"` returns `content_signal_match: "use_client"`.
- TSX file with JSX present surfaces high confidence when JSX shape agrees.

**BAD:**
- `repo` doesn't match the file's `_compute_repo_id` → returns empty archetype envelope (low confidence).
- Profile dir missing — returns empty envelope; no crash.
- Profile loadable but `archetypes` key empty — returns no archetype.
- File at impossible path (`/nonexistent/file.ts`) — `p.is_file()` False; fall through with `content_signal_match="none"`, low confidence.

**EDGE:**
- File present but `p.is_file()` False (broken symlink) — content_signal_value stays `"none"`.
- File >100KB — read capped at 100,000 bytes for AST scoring; first 200 bytes for content_signal.
- Mac symlink `/var/...` → `/private/var/...` — both resolved paths used for `relative_to` (lines 594-608).
- File at `repo_root/.chameleon/...` — should not be a normal file under analysis but function still tries.
- Profile carries a v0.5.x extension-blind bucket while file matches a v0.5.2 extension-aware bucket — both checked.
- ast_query with all fields null → confidence stays low.
- ast_query matches all 5 fields → `best_score >= 4` triggers `high`.
- ast_query matches exactly 1 field → `medium`.
- File AST extraction throws (language mismatch) — `extract_dimensions` returns its own snapshot regardless; tests don't isolate failure path.

**GAP (untested):**
- No test for the unreadable-file branch where `read_bytes()` raises OSError (line 671).
- No test where `loaded.profile["language"]` is something other than ts/ruby (None branch).
- No coverage for `repo_root_resolved.resolve()` failing.
- No test for the empty `exact_matches` + empty `fallback_matches` + empty `_prefix_overlap_fallback` result (truly no archetype anywhere).
- No coverage for the `content_signal_value is None` fall-through (line 555-556).
- No test for the legacy `snapshot.content_signal` mapping at line 750-751 when head read fails but content read succeeds.

**SLOP:**
- 200-byte vs 100KB read happen in distinct branches but no guarantee they agree (defensive comment notes this).
- Cluster-size tiebreak among AST-tied candidates — not tested with two same-AST-score candidates.
- `content_signal_match` alphabet is documented as `{"none", "use_client", "use_server", "shebang", "ts_pragma"}` but no test asserts this exhaustively.
- Comment on line 547-551 mentions schema as `{"strong", "weak", "none"}` — disagrees with the alphabet above. Doc bug.

---

### `_empty_pattern_envelope(repo_id, profile_status, trust_state)` at line 760

**Existing tests:** `v0_5_6_corrupted_profile_test.py` (`BUG-022`).

**GOOD:**
- Returns shape with `archetype.archetype=None`, `archetype.alternatives=[]`, idioms="", rules=[].
- All three input fields are passed through to `repo.{id, profile_status, trust_state}`.

**EDGE:**
- `repo_id=None` accepted.
- `profile_status` string is opaque — function does not validate values.

**GAP (untested):**
- No test that the envelope shape is byte-identical to the healthy `get_pattern_context` envelope keys (set equality).
- No regression that the `archetype.confidence_band` defaults to `"low"`.

**SLOP:**
- `archetype.archetype` (nested same-name key) is an unfortunate shape — consumers must look up `archetype["archetype"]`.

---

### `get_pattern_context(file_path)` at line 797

**Existing tests:** `comprehensive_test.py` (extensive), `pretooluse_hook_test.py`, `v0_5_6_corrupted_profile_test.py`, `v0_5_2_lint_idioms_test.py`, `claude_code_acceptance_test.py`.

**GOOD:**
- Trusted profile + matched archetype → returns archetype + canonical_excerpt + rules + idioms.
- Archetype matched but witness file missing — `canonical_data` stays empty with default shape.
- Multiple archetypes via fallback — first one wins.

**BAD:**
- File outside repo → empty envelope, `profile_status="no_repo"`.
- Profile JSON corrupt → empty envelope, `profile_status="profile_corrupted"`.
- Loaded profile but `load_profile_dir` raises — empty envelope with corrupted status.

**EDGE:**
- Idioms text > 8000 chars → truncated with `\n... [truncated]`.
- Witness file content > 3200 chars → truncated with `\n... [truncated]`.
- Sanitization (ANSI / tag-boundary) applied to both canonical content and idioms.
- File path with `~` — `expanduser`.

**GAP (untested):**
- No test for the witness read-failure branch (OSError line 877).
- No test for the case where `loaded.idioms_text` exists but is whitespace-only.
- No test for the empty `canonicals` list within an archetype (witness was dropped at bootstrap).
- No test that `meta.mtime_token` actually invalidates when artifacts change.
- No test for `meta.computed_at` being a parseable ISO timestamp (regression-protective).
- No coverage for the trusted-but-load-fails sequence (load_profile_dir throws after trust check).

**SLOP:**
- Re-uses `get_archetype` internally — adds a duplicate file read (200-byte + 100KB) per call. No test for performance.
- Two truncation budgets (canonical 3200, idioms 8000) hardcoded — drift from public docs not asserted.
- Always emits `trust_state` derived from `is_material_change`; doesn't surface the `legacy_trust_hint` envelope here (only in `detect_repo`).

---

### `_resolve_repo_root_by_id(repo_id, repo_root_hint=None)` at line 910

**Existing tests:** `index_db_test.py`, `v0_5_1_critical_test.py`, `v0_5_5_resolver_test.py`, `material_change_test.py`.

**GOOD:**
- index.db returns matching repo_root → returns resolved Path.
- index.db miss → falls back to trust record's `repo_root`.
- Both miss → returns None.
- `repo_root_hint` provided → index.db prefers the matching row over freshest-overall.

**BAD:**
- `repo_id` is empty string — index lookup returns None; trust lookup uses empty id; returns None.
- `repo_id` is None — code paths may TypeError; no defensive check.

**EDGE:**
- Cached path no longer exists on disk → returns None even though index has the row (line 936).
- Trust record's repo_root no longer exists → returns None.
- Monorepo: same repo_id under two workspace_paths; without hint, picks freshest.

**GAP (untested):**
- No test for the fallthrough-to-trust branch when the cached path returned by index doesn't exist (line 938-941).
- No test for `index_db.resolve_repo_root` returning a string that's a file (not a dir).
- No test for `record.repo_root` being a string that's a file (line 947-948).
- No test that the hint actually filters multiple matches.

**SLOP:**
- Two resolution mechanisms (index.db + trust) — no contract for which one is authoritative when they disagree.
- Function returns `Path.resolve()` results — silently follows symlinks; some callers may not expect this.

---

### `get_canonical_excerpt(repo, archetype)` at line 951

**Existing tests:** `comprehensive_test.py`, `v0_5_3_canonical_witness_test.py`, `v0_5_2_tools_test.py`.

**GOOD:**
- Trusted profile + valid archetype + valid witness → returns sanitized content with sha_hint.
- Repo is path or repo_id (shape-detect).
- Witness file >3200 chars → truncated.

**BAD:**
- Repo not resolvable → `status="failed", error="repo_id not found"`.
- Archetype name not in profile → `status="failed", error="archetype not found"`.
- Archetype known but `canonicals.json` has no entry → `status="no_witness"`.
- Profile load fails — returns minimal envelope (legacy compat shape).
- Witness path on disk is missing → returns empty content with witness_path set.
- Witness path read fails (OSError) → returns empty content.

**EDGE:**
- Witness path is empty string in canonicals entry → `status="no_witness"`.
- repo_root resolved to a path that doesn't exist → returns "repo_id not found".

**GAP (untested):**
- No test for the case where `canonicals.json` has an entry whose `witness` key is missing entirely (line 1052 default).
- No test that the three failure envelopes have distinct sets of fields (`error` vs `reason`).
- No test that sanitization fires on the witness content (e.g., `</chameleon-context>` stripped).
- No test exercising `Path.is_file()` on a symlinked witness.

**SLOP:**
- Three distinct `status` values (`failed`, `no_witness`, plus the legacy implicit success) — schema is in the docstring, not formalized.
- Legacy envelope shape (no status) returned for "witness missing on disk" — inconsistent with the typed failure envelopes for other branches.

---

### `get_rules(repo, archetype=None)` at line 1104

**Existing tests:** `comprehensive_test.py`, `v0_5_2_tools_test.py`.

**GOOD:**
- Valid repo_id + None archetype → returns all rule items.
- Valid repo_id + matching archetype substring → returns filtered subset.

**BAD:**
- Unresolvable repo → empty `{"rules": []}`.
- Profile load fails → empty `{"rules": []}`.

**EDGE:**
- archetype="" — falsy, treated as "all rules" (matches the `is None` test? No: explicit `is None`, so empty string falls into the filter path and matches everything via substring).
- Rules dict is None instead of dict — `rules_dict.items()` would TypeError; no defense.
- Archetype is a substring of a category name (`"format"` matches `"formatting"`).

**GAP (untested):**
- No test for the empty-string archetype behavior.
- No test for non-string archetype (int) — `archetype in str(k)` may behave unexpectedly.
- No test that the filter is case-sensitive.

**SLOP:**
- Filter is substring `archetype in str(k)` — false positives possible (e.g., archetype="ts" matches "typescript" by accident).
- Inconsistent with the documented "archetype" filter — really a prefix-or-substring match.

---

### `lint_file(repo, archetype, content)` at line 1125

**Existing tests:** `lint_engine_test.py`, `v0_5_2_lint_idioms_test.py`, `stubs_implemented_test.py`, `comprehensive_test.py`.

**GOOD:**
- Trusted repo + valid archetype + content with ast_query → real lint with violations + canonical_confidence.
- Content includes a secret → `secret_violations` come back first; non-secret violations after.
- Truncation at 100KB → `truncated: True`.
- Archetype has null ast_query → `stub: False, reason="no ast_query..."`.

**BAD:**
- Repo not resolvable AND not a valid path → `stub: True` envelope.
- Content is None — `len(None)` TypeError; no defense (signature says `str`).
- `archetype` is None — `canonicals.get(None)` returns [].
- Profile load fails → `stub: True, stub_reason="profile failed to load..."`.

**EDGE:**
- Repo is a path-shaped value that doesn't go through `_resolve_repo_root_by_id` (line 1184) but matches the fallback check (line 1186-1193).
- Content exactly 100,000 chars → not truncated (`>` not `>=`).
- Content exactly 100,001 chars → truncated.
- Language detection picks witness extension over profile-level language.
- Profile's language is "python" (not ts/ruby) → coerced to None for lint engine.

**GAP (untested):**
- No test for the "valid path but no profile.json" fallback (line 1186-1193) — depends on path mismatching repo_id.
- No test that secret violations precede AST violations in the merged list (line 1273).
- No test for `_lint` raising — function does not wrap in try/except, so an uncaught exception propagates.
- No test for `_canonical_confidence` returning non-float.
- No test for `language=None` short-circuiting the engine cleanly.

**SLOP:**
- Two code paths return `stub: True` (unresolvable repo, profile load fail) but use different `stub_reason` strings — no test asserts the strings.
- 100KB cap is hardcoded; ARCHITECTURE.md says "lint_file size contract" but no test extracts the constant.
- `secret_violations` always computed even when repo is unresolvable — security-positive but undocumented.

---

### `get_drift_status(repo)` at line 1291

**Existing tests:** `refresh_drift_test.py`, `material_change_test.py`, `v0_5_2_tools_test.py`.

**GOOD:**
- Valid repo_id + recent trust grant → `days_since_refresh=0, recommended="fresh"`.
- repo_id with stale grant (>90 days) → `recommended="profile may be stale"`.
- repo with high drift score (>0.5) → `recommended="observed drift is high"`.
- repo with no trust grant → `recommended="no trust grant found"`.

**BAD:**
- Empty/None `repo` → `status="failed"`.
- Path-shaped junk that doesn't exist → `status="failed"`.
- Repo string with `..` or `/` or `\` → `status="failed"` (path-traversal defense at line 1343).

**EDGE:**
- 31-day boundary → `consider refresh if codebase changed`.
- granted_at not in expected ISO format → `days_since_refresh=None`.
- drift_score is None (no observations) → falls through to age-based recommendation.
- `time.mktime` is timezone-sensitive; granted_at parsed via `time.strptime` then `time.mktime` (which interprets local time) — possible off-by-one near DST transitions. Note: `_iso_to_epoch` exists separately and uses `calendar.timegm`; this function does NOT use it, which is itself a potential bug.

**GAP (untested):**
- No test for the path-traversal defense (line 1343).
- No test for opaque non-hex non-path key behavior (line 1334-1348 — preserved for drift-recording callers).
- No test for the `granted_at` parse failure branch.
- No test for the timezone-sensitivity of `time.mktime`.
- No test for the resolved_path branch (line 1326-1333) when path-shaped input fails to resolve.

**SLOP:**
- Uses `time.mktime` not `calendar.timegm` — assumes local timezone matches UTC; DST bug possible.
- Recommendation thresholds (30, 90 days; 0.5 drift) hardcoded; no test pins boundaries.

---

### `_content_sha_hint(path)` at line 1391

**Existing tests:** indirect via `partial_refresh_test.py`.

**GOOD:**
- Returns 16-char hex digest for a small file.
- Same content → same hash.

**BAD:**
- xxhash not installed → returns None (`ImportError` caught).
- File doesn't exist → returns None (`OSError` caught).
- Permission denied on read → returns None.

**EDGE:**
- Empty file → returns hash of empty bytes (xxhash deterministic).
- Very large file (`read_bytes()` loads all into memory) — no streaming.

**GAP (untested):**
- No test enforcing the no-xxhash fallback.
- No test for performance on large files.
- No test that the result is byte-stable across Python versions.

**SLOP:**
- Loads entire file into memory; on a 1GB file this OOM-risks the bootstrap.
- Comment says "xxhash64 is sufficient for change detection" — no test validates this against the bootstrap's stored sha_hint format.

---

### `_hash_cluster_key_for(key)` at line 1411

**Existing tests:** indirect via `partial_refresh_test.py`.

**GOOD:**
- Mirrors `canonical._hash_cluster_key` byte-for-byte (16-char prefix of sha256).
- Deterministic across calls.

**BAD:**
- `key.to_dict()` raises → propagates.

**EDGE:**
- Key contains values that don't JSON-serialize (sets, tuples) → `json.dumps` raises.
- Key contains unicode characters → preserved verbatim.

**GAP (untested):**
- No regression test comparing the output of this function vs `canonical._hash_cluster_key` on the same key (the doc says they must be byte-identical).
- No test for the 16-char truncation boundary.

**SLOP:**
- Duplicated logic (canonical.py also defines it); no contract-test enforces equality. A future refactor that adds a salt to one side would silently break partial refresh.

---

### `_compute_file_cluster_map(repo_root, paths_glob=None)` at line 1424

**Existing tests:** `partial_refresh_test.py`.

**GOOD:**
- Returns list of `(rel_path, cluster_id, sha_hint)` rows after re-running discover+parse+cluster.
- Returns empty list for repo with no candidates.

**BAD:**
- No supported extractor → returns None.
- Discovery raises → returns None.
- Parse raises → returns None.

**EDGE:**
- Members at paths outside repo_root → relative_to raises; falls back to absolute path string.
- paths_glob narrows discovery; verify the glob is honored.

**GAP (untested):**
- No test for the None return paths individually (extractor None, discovery raise, parse raise).
- No test that the per-file sha_hint matches `_content_sha_hint` output on the same file.
- No test that the cluster_id matches the orchestrator's stored cluster_id post-bootstrap (round-trip).

**SLOP:**
- Second-pass re-discovery duplicates orchestrator work — expensive on big repos.
- Comment says cost is bounded by `REPO_SIZE_GUARD (200_000 files)` but no test exercises near-limit behavior.

---

### `_reparse_changed_files(repo_root, paths)` at line 1486

**Existing tests:** `partial_refresh_test.py`.

**GOOD:**
- Returns dict `{rel_path: (cluster_id, sha_hint)}` for parseable files.
- Empty paths → returns empty dict (line 1502).

**BAD:**
- No extractor → returns None.
- Parse raises → returns None.

**EDGE:**
- Files outside repo_root → relative_to raises; absolute string fallback.
- File that parses but doesn't land in any cluster — never appears in result dict.

**GAP (untested):**
- No test for the "parses but no cluster" case (file silently dropped).
- No test for partial parse_result skipped files.

**SLOP:**
- Returns None vs empty dict for different failure modes — caller must distinguish carefully.

---

### `_attempt_partial_refresh(repo_root, repo_id, profile_dir, candidates, prev_state, started_at)` at line 1529

**Existing tests:** `partial_refresh_test.py`.

**GOOD:**
- Modified+added files ≤ 10% of prev_state → partial refresh succeeds.
- No changes at all → returns None (forces full refresh for summary re-render).
- Member moved from cluster A to B (both known) → archetypes.cluster_size updated.

**BAD:**
- Profile JSON files unreadable → returns None.
- New cluster_id discovered → returns None (canonical selection needs full corpus).
- Modified file IS the current canonical witness → returns None.
- Re-parse misses a file (ts_dump skipped) → returns None.
- atomic_profile_commit raises → returns None (state unchanged).

**EDGE:**
- change_ratio exactly 0.10 → still partial (`>` not `>=`).
- change_ratio = 0.10000001 → falls to full refresh.
- All files modified, none added/removed (change_ratio=1.0) → returns None.
- Removed file whose witness path matches → returns None.
- prev_state empty but candidates non-empty → change_ratio = N/1 → > 0.10; returns None.

**GAP (untested):**
- No test for the renames.json preservation path (line 1758-1764) when the file is unreadable.
- No test for the idioms.md preservation when read raises OSError (line 1742-1745).
- No test for the generation-counter consistency post-amend (loader will reject mismatched generations).
- No test for index.db rollback when atomic commit succeeded but file_clusters insert fails.
- No test for the case where all files are unchanged but a no-op short-circuit upstream failed (line 1602-1606).

**SLOP:**
- Multiple bail-out branches return None for very different reasons — caller can't distinguish "ratio exceeded" from "witness was modified" without surfacing diagnostics.
- Algorithm comment lists 12 steps but actual code has implicit ordering — drift risk.
- 10% ceiling baked in as a constant (`PARTIAL_REFRESH_CHANGE_RATIO_CEILING`) — no test that verifies boundary precisely.

---

### `refresh_repo(repo, force=False)` at line 1846

**Existing tests:** `refresh_drift_test.py`, `partial_refresh_test.py`, `comprehensive_test.py`, `v0_5_2_tools_test.py`, `v0_5_6_bootstrap_force_test.py`.

**GOOD:**
- Existing trusted repo with no file changes → `status="noop"`.
- Existing repo with small change set → partial refresh.
- Existing repo with large change set → falls to full re-bootstrap.
- `force=True` → bypasses both short-circuits.

**BAD:**
- `repo` not resolvable → `status="failed"`.
- `repo` is a path that exists but is not a directory → `status="failed"`.
- Discovery raises → full re-bootstrap.

**EDGE:**
- No cached index row → full re-bootstrap.
- No extractor → full re-bootstrap.
- idioms.md is newer than last_seen_at → no-op short-circuit broken; partial path tried.
- last_seen_at is empty / unparseable → `last_seen_epoch=0`; nothing_newer is False; partial-or-full path.
- prev_state empty → partial path skipped, full re-bootstrap.

**GAP (untested):**
- No test for the case where `index_db.get_repo` returns a row but the profile.json file is missing on disk (line 1908).
- No test for `_iso_to_epoch` returning 0.0 because of malformed cached ISO string.
- No test for the touch-row-on-noop branch (line 1951-1958).
- No test that `force=True` re-emits all bootstrap envelope fields (including workspace_reports).
- No test for the case where `discover_files` returns a count that matches cached `files_indexed` but the files are completely different (false-positive noop).

**SLOP:**
- Multiple internal `bootstrap_repo(..., force=True)` fallbacks duplicate the BUG-026 comment 5 times — refactor candidate.
- `_iso_to_epoch` exists as a separate helper but `get_drift_status` uses `time.mktime` not this — inconsistency.

---

### `_iso_to_epoch(ts)` at line 1989

**Existing tests:** indirect via `refresh_drift_test.py`.

**GOOD:**
- ISO 8601 UTC `"2026-05-12T13:30:00Z"` → correct epoch.
- ISO with microseconds `"2026-05-12T13:30:00.123Z"` → preserved as fractional seconds.

**BAD:**
- Non-string input → returns 0.0 (TypeError caught by the explicit `if not ts` check, but only at the top — int/None would not pass `endswith` check).
- Wait — actually the function does `if not ts: return 0.0` which catches None/empty string.
- An int input would call `"." in ts` raising TypeError.

**EDGE:**
- Empty string → 0.0.
- Whitespace-only `"   "` — passes the `if not ts` check (truthy); `"." in "   "` is False; falls through to second-precision parse which fails; returns 0.0.
- Bad format (`"2026-05-12"` without time) → 0.0.
- Microseconds without `Z` — falls to second-precision path which fails.

**GAP (untested):**
- No direct unit test for this helper.
- No test for the microsecond-precision branch.
- No test for non-string input.
- No test confirming `calendar.timegm` (UTC) semantics vs `time.mktime` (local).

**SLOP:**
- Doc explains the calendar.timegm choice but no test enforces it; a refactor could regress to mktime silently.
- `"if not ts"` is shallow — `0` and `False` also short-circuit.

---

### `bootstrap_repo(path, mode="full", paths_glob=None, force=False)` at line 2021

**Existing tests:** `bootstrap_mechanism_test.py`, `cold_start_init_test.py`, `comprehensive_test.py`, `v0_5_2_bootstrap_test.py`, `v0_5_3_monorepo_bootstrap_test.py`, `v0_5_6_bootstrap_force_test.py`, `v0_5_7_sidecar_bootstrap_test.py`, `v0_5_7_workspace_tool_configs_test.py`.

**GOOD:**
- Path with no `.chameleon/` → runs full bootstrap, returns success envelope.
- Path with `.chameleon/COMMITTED` → returns `status="already_bootstrapped"` unless `force=True`.
- `force=True` overrides the already_bootstrapped guard.
- Monorepo with detected workspaces → root + per-workspace reports.
- TS repo → TypeScript extractor; Ruby repo → RubyExtractor.

**BAD:**
- `path` empty/None → `status="failed"`.
- `path` doesn't exist → `status="failed"`.
- `path` is a file, not a dir → `status="failed"`.
- Bootstrap raises mid-way → propagates (no top-level catch).

**EDGE:**
- Repo with both `Gemfile` and `tsconfig.json` and `app/javascript/` → Rails-with-frontend → Ruby.
- Repo with workspaces at `apps/x` whose path resolves to the same repo_root → skipped (`ws_root == repo_root.resolve()`).
- index.db write fails post-success → silently ignored (BUG: no test).

**GAP (untested):**
- No test for the OSError on `resolved_path.resolve()` (line 2053-2055).
- No test for the post-success index.db population branch for workspaces (line 2114-2141).
- No test that `_compute_file_cluster_map` returning None doesn't break the envelope.
- No test for the `mode` parameter (forward-compat placeholder).

**SLOP:**
- `mode` parameter is unused (`del mode`) — should probably be removed or implemented.
- `already_bootstrapped` envelope returns `profile_path` but not `repo_id` — minor asymmetry with success envelope.
- Three index.db `upsert_repo` calls inside one function — refactor opportunity.

---

### `list_profiles(cursor=None, limit=100)` at line 2146

**Existing tests:** `index_db_test.py`, `comprehensive_test.py`, `daemon_test.py`.

**GOOD:**
- Returns paginated list of profiles with `next_cursor` when more exist.
- Returns total_known count.
- Trusted profiles surface `trusted_at`, `trusted_by`.

**BAD:**
- `limit=0` → failed envelope.
- `limit=1001` → failed envelope.
- `limit="100"` (string) → failed envelope (`isinstance(int)` check).
- Unknown cursor → failed envelope.

**EDGE:**
- limit=1 → returns 1 profile and a next_cursor if more.
- limit=1000 (max) → accepted.
- cursor="" (empty string) → behaves like None (start from top) — actually `index_db.list_repos` would handle this.
- Backfill from legacy dirs runs on every call (potentially expensive on large $PLUGIN_DATA).

**GAP (untested):**
- No test for the cursor=invalid path that returns "unknown cursor".
- No test for the pagination boundary (next_cursor present iff there are more rows).
- No test that the backfill is idempotent (re-running list_profiles doesn't duplicate).
- No test that the envelope's `total_known` matches the sum across pages.
- No test for the trust-check optimization at line 2194 — only consult trust state if the per-repo dir exists.

**SLOP:**
- Backfill is a side-effect on every list call; no test that it's cheap when nothing to backfill.
- `trust_state` only "trusted" or "untrusted" — drops "stale" and "n/a" distinctions.
- `granted_at` field surfaces as `trusted_at` — naming mismatch.

---

### `_backfill_index_from_legacy_dirs()` at line 2218

**Existing tests:** indirect via `index_db_test.py`, `list_profiles` tests.

**GOOD:**
- Walks ${PLUGIN_DATA}/*/ ; ignores hidden dirs; ignores dirs without trust records.
- Skips repos already in index.db.
- Inserts via `index_db.upsert_repo` with trust record's `granted_at` as `last_seen_at`.

**BAD:**
- ${PLUGIN_DATA} doesn't exist → returns early.
- `iterdir` raises (permission) → caught.

**EDGE:**
- Directory name is not a valid hex digest — still tried; index.db may reject or accept.
- Trust record missing `repo_root` → skipped.

**GAP (untested):**
- No test for the permission-denied branch.
- No test for the "trust record exists but profile.sha256 is empty" case.
- No test for non-hex-digest directory names.
- No test for the idempotency (running twice doesn't duplicate).

**SLOP:**
- `granted_at` may be the only ISO timestamp on legacy records; using it as `last_seen_at` is approximate.

---

### `merge_profiles(repo, base, ours, theirs)` at line 2257

**Existing tests:** `git_merge_driver_test.py`.

**GOOD:**
- Union of archetypes; conflict resolved by higher cluster_size.
- Tie broken by alphabetic witness path.
- Writes result to `ours` per git merge driver convention.

**BAD:**
- `ours` or `theirs` doesn't exist → failed envelope.
- Invalid JSON in either file → failed envelope with `merged_profile_path=None`.

**EDGE:**
- Both empty archetype dicts → result is empty.
- Identical archetypes → union is no-op.
- `base` is unused (per comment) — passed but ignored.

**GAP (untested):**
- No test for the alphabetic witness path tiebreak when sizes are equal.
- No test that `theirs` is preserved after merge (function only writes `ours`).
- No test for very large profiles with thousands of archetypes (perf).
- No test that the merge survives an archetype with no `cluster_size` key (default 0).

**SLOP:**
- `base` is intentionally unused but still in signature; not annotated as forward-compat.
- No safety against writing to a non-`ours` path (the docstring says "writes to ours").
- Result skips a re-canonical-selection step; user MUST run `/chameleon-refresh` post-merge — but no envelope flag enforces this.

---

### `teach_profile(repo, feedback)` at line 2334

**Existing tests:** `teach_roundtrip_test.py`, `v0_5_2_lint_idioms_test.py`, `interview_flow_test.py`, `comprehensive_test.py`.

**GOOD:**
- Adds new idiom under `## active`; preserves existing.
- User-supplied `### slug` header used verbatim.
- Auto-derived slug from rationale's first non-empty line.
- Falls back to timestamp slug on collision.

**BAD:**
- Empty/whitespace-only feedback after sanitization → failed envelope.
- Feedback > 50KB → failed envelope.
- Repo not resolvable → failed envelope.
- Profile dir missing → failed envelope.
- Lock held by another `/chameleon-teach` → failed envelope with holder pid.

**EDGE:**
- Same-second back-to-back teach calls → 4-hex random suffix prevents collision; one retry.
- Rationale-derived slug already exists in idioms.md → falls back to timestamp slug.
- Feedback contains `## deprecated` → escaped to `\## deprecated`.
- Feedback contains `# heading` inside fenced code block → NOT escaped.
- Feedback contains a suspicious pattern (`ignore previous instructions`) → idiom IS stored, envelope carries `suspicious_input: True`.

**GAP (untested):**
- No test for the rationale-slug derivation when first line is all punctuation.
- No test that `_slug_from_rationale` rejects too-short candidates (`< 4` chars).
- No test for the explicit code-fence-state tracking in `_escape_markdown_section_headings` (BUG-NEW-007).
- No test for the lock timeout path (`LockHeldError`).
- No test that the placeholder `_(no idioms yet ...)_` is stripped on first add.
- No test for the second-retry collision branch (unlikely but reachable).

**SLOP:**
- Slug derivation logic embedded inside `teach_profile` (`_slug_from_rationale` is a nested def) — hard to unit-test.
- 50KB cap measured post-sanitization, but the suspicious-pattern scan runs on raw feedback — inconsistent boundaries.
- "Suspicious input" is logged in response but no test enforces what consumers do with it.

---

### `_escape_markdown_section_headings(text)` at line 2499

**Existing tests:** indirect via `teach_roundtrip_test.py`.

**GOOD:**
- `# heading` → `\# heading`.
- `## active` → `\## active`.
- `### subheading` → unchanged (level 3+ untouched).
- Code fence (` ``` `) toggles in/out state.
- Indented headers `  # foo` → `  \# foo` (indent preserved).

**BAD:**
- text=None — `text.split("\n")` raises AttributeError.

**EDGE:**
- Single `#` or `##` alone → escaped.
- Code fence with info string ` ```python ` → toggles.
- Unbalanced code fences → second toggle off never happens; remaining lines treated as inside fence.
- Headers inside a nested fenced block — escape suppressed.

**GAP (untested):**
- No test for the BUG-NEW-007 fenced-code-block branch.
- No test for the unbalanced fence handling.
- No test for indent preservation.
- No test for `### ` (level 3) being untouched.

**SLOP:**
- Fence detection uses `lstrip().startswith("```")` — doesn't check that the line is just a fence; ` ```ts foo ` is a fence start.
- "Levels 1 and 2 escaped, ### untouched" — non-obvious; tested behavior would be helpful.

---

### `disable_session(repo, session_id)` at line 2542

**Existing tests:** `optouts_test.py`, `daemon_test.py`.

**GOOD:**
- Valid repo + session_id → writes marker file, returns success.

**BAD:**
- Empty session_id → failed envelope.
- Non-string session_id → failed envelope.
- Unresolvable repo → failed envelope.

**EDGE:**
- session_id contains path-traversal chars (`../foo`) — `write_session_disable` may or may not sanitize.
- session_id is extremely long (>255 chars) — filesystem may reject.

**GAP (untested):**
- No test for the path-traversal session_id case.
- No test that the marker file actually suppresses subsequent preflight calls in the same session.
- No test for filename-too-long session_id.

**SLOP:**
- session_id is appended directly to a filename — no sanitization documented.

---

### `pause_session(repo, minutes=15)` at line 2576

**Existing tests:** `optouts_test.py`.

**GOOD:**
- Valid repo + minutes in [1, 240] → writes pause file, returns expiry.
- Default minutes=15.

**BAD:**
- minutes=0 → failed envelope.
- minutes=241 → failed envelope.
- minutes="15" (string) → failed envelope (isinstance check).
- minutes=-1 → failed envelope.
- Unresolvable repo → failed envelope.

**EDGE:**
- minutes=240 (max) → accepted, 4-hour pause.
- minutes=1 (min) → accepted.

**GAP (untested):**
- No test for the upper boundary 241 explicitly.
- No test that the expiry time is correctly future-dated (ISO format).
- No test that an existing pause file is overwritten cleanly.

**SLOP:**
- 240-minute (4-hour) cap is arbitrary; no doc rationale.
- expires_at format is ISO 8601 Z; no test enforces the exact pattern.

---

### `trust_profile(repo, confirmation_token)` at line 2611

**Existing tests:** `trust_flow_test.py`, `v0_5_1_trust_test.py`, `v0_5_6_trust_repo_id_test.py`, `comprehensive_test.py`, `v0_2_regression_test.py`, `v0_5_6_trust_state_test.py`.

**GOOD:**
- Valid repo + token = repo basename → grants trust.
- Valid repo + token = `yes-trust-<repo_id_short>` → grants trust.
- Repo can be path or repo_id.

**BAD:**
- Empty repo → failed envelope.
- Repo doesn't exist → failed envelope.
- No `.chameleon/` → failed envelope.
- No `profile.json` → failed envelope.
- Profile JSON corrupted → failed envelope ("profile is not loadable" — BUG-NEW-020).
- Profile schema_version > MAX_SUPPORTED → failed envelope.
- Wrong confirmation_token → failed envelope.

**EDGE:**
- Token is the repo's basename but case-mismatched ("Foo" vs "foo") → fails.
- Token is `yes-trust-12345678` for a repo whose id starts with that prefix → accepted.
- Profile loads but has schema_version=99 → load_profile_dir raises → failed envelope.

**GAP (untested):**
- No test for the case-sensitivity of the confirmation token.
- No test for the BUG-NEW-020 path with a non-corruptible-but-unsupported schema_version.
- No test that grant_trust is NOT called when load_profile_dir raises.

**SLOP:**
- Two valid token forms (basename vs `yes-trust-<id>`) — minor UX confusion.
- `repo_path.exists()` then `is_dir()` is two stat calls; could be one.

---

### `_sanitize_user_input(text)` at line 2684

**Existing tests:** indirect via `teach_roundtrip_test.py`, `adversarial_profile_test.py`.

**GOOD:**
- Strips ANSI escapes.
- Strips zero-width unicode.
- Normalizes NFC.
- Escapes `</chameleon-context>` tags.

**BAD:**
- text=None — sanitization module may handle or raise.

**EDGE:**
- Empty text → empty string.
- Text with only ANSI → empty string after strip.
- Text with `<chameleon-context>` (opening, not closing) — may or may not escape.

**GAP (untested):**
- No test that sanitize_for_chameleon_context is fully covered by adversarial_profile_test.

**SLOP:**
- One-line wrapper — moot to unit-test independently.

---

### `_looks_suspicious(text)` at line 2740

**Existing tests:** indirect via teach_profile tests; explicit pattern coverage in `adversarial_profile_test.py`.

**GOOD:**
- Detects "ignore previous instructions".
- Detects "you are now in DAN mode".
- Detects `eval(...)`, `exec(...)`, `rm -rf`.
- Detects `system:` and `<system>` tags.

**BAD:**
- text=None → returns (False, None).
- text="" → returns (False, None).
- text=42 → returns (False, None) (isinstance str check).

**EDGE:**
- Pattern split across newlines (`ignore\nprevious\ninstructions`) — `\s+` matches newlines.
- Unicode lookalikes (`іgnore` with Cyrillic і) — would not match (the regex is ASCII).
- ALL CAPS — case-insensitive regex matches.
- Embedded in a larger benign text — still matches (substring scan).

**GAP (untested):**
- No test for the unicode lookalike bypass.
- No test that the 8 patterns are exhaustively tested.
- No test for false positives (e.g., "I evaluated my options" — `eval(` not matched without paren).
- No test for the "you are now <mode>" pattern with arbitrary 32-char prefix.

**SLOP:**
- 8 patterns hardcoded; no test enforces the count or which are present.
- Label strings (e.g., "ignore previous instructions") are visible in response — no test of stability.

---

### `_slugify(value)` at line 2788

**Existing tests:** indirect via `interview_flow_test.py`, `archetype_naming_test.py`.

**GOOD:**
- "User Controller" → "user-controller".
- "abc" → "abc".
- "users_v2" → "users-v2".

**BAD:**
- None → None.
- "" → None.
- "123" → None (leading digit).
- "---" → None (all hyphens stripped).

**EDGE:**
- Longer than 64 chars → truncated.
- Unicode chars (`café`) → stripped to ASCII.
- Mixed case → lowercased.

**GAP (untested):**
- No direct unit test for `_slugify` boundary cases.
- No test that the 64-char cap is exact.
- No test for emoji or non-Latin characters.

**SLOP:**
- 64-char cap is implicit (matches `ARCHETYPE_NAME_RE.pattern`); no test verifies the regex match.

---

### `_propose_alternatives_for(current_name, archetype, canonical)` at line 2806

**Existing tests:** `interview_flow_test.py`, `archetype_naming_test.py`, `v0_5_6_rename_candidates_test.py`, `v0_5_6_rename_disambiguation_test.py`.

**GOOD:**
- Generates 3-5 candidates from witness filename stem, paths_pattern tail, node kinds.
- "ClassNode" → "class" friendly mapping.
- JSX-present cluster → suggests "react-component".

**BAD:**
- canonical=None → no witness candidates.
- archetype={} → no candidates from archetype hints.

**EDGE:**
- Current name = "cluster-abc" → BUG-006: derivative names like "cluster-abc-foo" filtered out.
- Witness with double extension (`.test.ts`, `.spec.tsx`) — two regex passes strip both.
- paths_pattern tail is a version (`v1`, `v2.0`) — skipped.

**GAP (untested):**
- No test for the third extension strip (e.g., `.d.ts.bak`).
- No test for the witness-tail directory walking when only the first segment is generic (`app/x/y.rb` → tries `x`).
- No test for the "combined current-tail" candidate at line 2886.
- No test for empty top_level_node_kinds list.

**SLOP:**
- 7 distinct candidate strategies — order matters but isn't formalized.
- `_NODE_KIND_TO_NAME` covers TS + Ruby — silently dependent on language; no test asserts ruby ClassNode mapping.

---

### `propose_archetype_renames(repo, top_n=8)` at line 2895

**Existing tests:** `interview_flow_test.py`, `archetype_naming_test.py`, `v0_5_6_rename_candidates_test.py`.

**GOOD:**
- Returns top-N archetypes ranked by cluster_size descending.
- Each row carries current_name, cluster_size, canonical_file, paths_pattern, suggested_alternatives.

**BAD:**
- top_n=0 → failed envelope.
- top_n=65 → failed envelope.
- top_n=-1 → failed envelope.
- top_n="8" → failed envelope (isinstance check).
- Repo not resolvable → failed envelope.
- No `.chameleon/` → failed envelope.
- Profile load fails → failed envelope.

**EDGE:**
- top_n > number of archetypes → returns all archetypes (no error).
- Ties broken by archetype name (alphabetic).

**GAP (untested):**
- No test for the cluster_size=0 edge (sorted lowest, but still present).
- No test that ranking is stable across re-runs.
- No test that an archetype with no canonical entry surfaces `canonical_file=""`.

**SLOP:**
- 64 upper limit is arbitrary; no doc rationale.

---

### `_validate_renames(renames, existing_names)` at line 2965

**Existing tests:** `interview_flow_test.py`, `v0_5_6_rename_disambiguation_test.py`.

**GOOD:**
- Valid rename mapping → returns `(effective, None)`.
- No-op rename (`old == new`) → dropped.

**BAD:**
- renames not a dict → error.
- key/value not string → error.
- Key not in existing names → error.
- Value doesn't match `ARCHETYPE_NAME_RE` → error.
- Two renames collide on the same target → error.
- Target collides with an unrenamed existing → error.

**EDGE:**
- Empty mapping → returns `({}, None)`.
- All no-ops → returns `({}, None)`.
- Target = "" → fails regex.

**GAP (untested):**
- No test for non-string key (e.g., int 42).
- No test for the collision-with-other-renamed-source case (A→B, C→A; should be OK since A is being renamed away).

**SLOP:**
- Error messages are reasonable but no test pins their exact format.

---

### `_rewrite_summary_md(profile_data, archetypes_data, canonicals_data, idioms_text, rules_data=None)` at line 3012

**Existing tests:** indirect via `apply_archetype_renames` tests.

**GOOD:**
- Renders archetype list, rules section, idioms section.
- Skips the deprecated section when empty.
- Falls back to v0.5.4 placeholder when rules_data is None.

**BAD:**
- profile_data missing keys → uses `.get(..., "")` defaults; no crash.
- archetypes_data without "archetypes" key → empty list.

**EDGE:**
- canonicals empty for an archetype → "(none)" placeholder.
- language_hint present with `secondary_detected` → renders "## Secondary language" block.
- Idioms text contains both active and deprecated sections.

**GAP (untested):**
- No test that the output is byte-identical to orchestrator's `_build_summary_md` for the same inputs.
- No test for the language_hint branch.
- No test for rules_data with a tool block whose body is not a dict.

**SLOP:**
- Code duplication with orchestrator — drift risk.
- "Keep this in sync if the orchestrator's output changes" comment is the only guard.

---

### `_read_renames_overlay(profile_dir)` at line 3168

**Existing tests:** indirect via `apply_archetype_renames` tests.

**GOOD:**
- Reads `.chameleon/renames.json` and returns `{auto_name: user_name}`.
- Missing file → returns `{}`.
- Malformed JSON → returns `{}`.

**EDGE:**
- schema_version > 1 → returns `{}`.
- `renames` key missing → returns `{}`.
- `renames` value not a dict → returns `{}`.
- Entries with non-string key or value → filtered out.

**GAP (untested):**
- No test for the schema_version=0 / negative case.
- No test that a malformed entry doesn't poison the whole dict.

**SLOP:**
- Tolerant-by-design — fail-quiet but no warning emitted.

---

### `_merge_rename_overlay(existing, incoming)` at line 3192

**Existing tests:** `interview_flow_test.py` (partial).

**GOOD:**
- Incoming source matches an existing key → updates value.
- Incoming source matches an existing value → walks back to original auto-name key and updates.
- Incoming source is brand-new → adds new entry.

**EDGE:**
- Same source appears in both existing and as value of a different entry → first match wins (key check before value check).
- Cyclic remappings (A→B, B→C with C→A in incoming) — not validated.

**GAP (untested):**
- No explicit unit test for the three branches.
- No test for the cyclic case.

**SLOP:**
- Function is pure but lacks a doctest example.

---

### `apply_archetype_renames(repo, renames)` at line 3226

**Existing tests:** `interview_flow_test.py`, `v0_5_6_rename_disambiguation_test.py`.

**GOOD:**
- Effective renames applied → rewrites archetypes.json, canonicals.json, rules.json, profile.summary.md.
- No-effective-renames (all no-ops) → returns success with `renames_applied: 0`.
- Renames merged into `.chameleon/renames.json`.

**BAD:**
- Repo not resolvable → failed envelope.
- Repo dir doesn't exist → failed envelope.
- No `.chameleon/` → failed envelope.
- Profile load fails → failed envelope.
- _validate_renames returns error → failed envelope.
- atomic_profile_commit raises → failed envelope.

**EDGE:**
- Renames target an archetype that also has a rules.json entry → renamed there too.
- A rename target collides with an existing archetype name being renamed away — validated as OK.

**GAP (untested):**
- No test for the index.db update branch failing silently (line 3368).
- No test that idioms.md content survives the atomic write.
- No test for the case where renames.json was malformed pre-apply (overlay reset).

**SLOP:**
- Deep-copies all four artifacts via `json.dumps`/`json.loads` round-trip — expensive for large profiles.

---

### `teach_profile_structured(repo, *, slug, rationale, example, counterexample, archetype, status)` at line 3394

**Existing tests:** `teach_roundtrip_test.py`, `v0_5_2_lint_idioms_test.py`.

**GOOD:**
- Valid slug + rationale → renders structured idiom; delegates to teach_profile.
- `status="deprecated"` → rendered under deprecated header.
- archetype + example + counterexample → all rendered.

**BAD:**
- Invalid slug → failed envelope.
- Empty rationale → failed envelope.
- status not in {active, deprecated} → failed envelope.
- archetype doesn't match ARCHETYPE_NAME_RE → failed envelope.
- Total size > 50KB → failed envelope.

**EDGE:**
- slug exactly 64 chars matching regex → accepted.
- slug exactly 3 chars (regex requires `{2,63}`) — wait, slug must be 3-64 chars per `^[a-z][a-z0-9-]{2,63}$`.
- No example/counterexample → renders without their sections.

**GAP (untested):**
- No test for the size cap boundary exactly at 50,000.
- No test that the rendered markdown contains the example/counterexample inside code fences.
- No test for delegation back-pressure (what if teach_profile fails).
- No test for the archetype validation being skipped when archetype=None.

**SLOP:**
- Builds the rendered text and delegates — duplicates teach_profile's lock + sanitization implicitly.

---

### `daemon_status()` at line 3479

**Existing tests:** `daemon_test.py`.

**GOOD:**
- Returns `alive`, `pid`, `socket`, `uptime_s`, `last_request_at`, `running_version`.
- Successful ping → `last_request_at` populated.

**EDGE:**
- Daemon not alive → `last_request_at=None`.
- importlib.metadata fails → `running_version=None`.
- Ping times out (0.5s) → `last_request_at=None`.

**GAP (untested):**
- No test for the running_version fallback path.
- No test for the ping-timeout path explicitly.

**SLOP:**
- 0.5s timeout hardcoded; no rationale.

---

## mcp/chameleon_mcp/bootstrap/orchestrator.py

### `_is_rails_with_frontend(repo_root)` at line 51

**Existing tests:** `v0_5_6_rails_with_frontend_test.py`, `cold_start_init_test.py`, `pretooluse_hook_test.py`.

**GOOD:**
- Gemfile + config/application.rb + app/javascript → True.
- Gemfile + config/application.rb + app/assets/javascripts → True.
- Gemfile + config/application.rb + app/frontend → True.

**BAD:**
- Missing Gemfile → False.
- Missing config/application.rb → False (rules out vendored gems / SDKs).
- All three JS dir candidates missing → False.

**EDGE:**
- Symlinked app/javascript pointing outside repo → still True (`.is_dir()`).
- Gemfile present but config/application.rb is a directory → `.is_file()` False.
- Multiple sidecar dirs present → still True.

**GAP (untested):**
- No test exercising symlinked sidecar dirs.
- No test that the predicate fires only when ALL three of (Gemfile, application.rb, one JS sidecar) are present.

**SLOP:**
- Three sidecar conventions hardcoded — no extension point for app teams using non-standard layouts.

---

### `_rails_frontend_dir(repo_root)` at line 90

**Existing tests:** indirect via `v0_5_6_rails_with_frontend_test.py`.

**GOOD:**
- Returns `app/javascript` first if present.
- Falls back to `app/assets/javascripts`, then `app/frontend`.
- Returns None if none present.

**EDGE:**
- All three dirs present — modern (app/javascript) wins.
- Only the legacy dir present.

**GAP (untested):**
- No direct unit test for the precedence order.

**SLOP:**
- Order hardcoded; not documented as priority list.

---

### `_count_ts_files_under(directory)` at line 109

**Existing tests:** indirect via `v0_5_6_rails_with_frontend_test.py`.

**GOOD:**
- Returns count of `*.ts`, `*.tsx`, `*.js`, `*.jsx`, `*.mjs`, `*.cjs` under directory.
- Returns 0 for non-directory.
- Returns 0 for empty dir.

**EDGE:**
- Symlink loop → `rglob` may visit twice; cap at 50000 stops runaway.
- Permission-denied dir → `OSError` caught per ext.
- Mixed-case extensions (`.TS`, `.TSX`) — `rglob` is case-sensitive on POSIX.

**GAP (untested):**
- No test for the 50000 cap firing.
- No test for the symlink-loop case.
- No test for `.ts` files inside `node_modules` (rglob would count them).

**SLOP:**
- No exclusion list — `node_modules`, `dist`, etc. all counted.

---

### `_ad_hoc_discovery_hints(repo_root)` at line 131

**Existing tests:** `v0_5_6_discovery_hints_test.py`.

**GOOD:**
- Walks apps/*, packages/*, services/*, workspaces/* one level deep.
- Returns up to 50 hints with subdir, abs_path, language.
- Language inferred from package.json/tsconfig.json (ts) or Gemfile (ruby).

**EDGE:**
- Cap at 50 — additional matches dropped.
- Permission error on parent dir → skipped.
- Symbolic links to outside repo — still relative_to errors caught.

**GAP (untested):**
- No test for the 50-hint cap firing.
- No test that the language inference is correct on a mixed-language workspace.

**SLOP:**
- Conventional dir list hardcoded; no extension point.

---

### `_count_ruby_files_under(directory)` at line 175

**Existing tests:** indirect via `_select_extractor`.

**GOOD:**
- Counts `*.rb` files; returns 0 for non-dir.

**EDGE:**
- Cap at 50000.
- Permission denied → caught.

**GAP (untested):**
- Same as `_count_ts_files_under` gaps.

**SLOP:**
- Mirror of TS counter — refactor candidate.

---

### `_select_extractor(repo_root)` at line 192

**Existing tests:** `comprehensive_test.py`, `cold_start_init_test.py`, `v0_5_6_rails_with_frontend_test.py`, `v0_5_6_ts_signal_fallback_test.py`.

**GOOD:**
- Rails-with-frontend → RubyExtractor.
- Pure TS repo → TypeScriptExtractor.
- Pure Ruby repo → RubyExtractor.
- Mixed without Rails signal → TypeScriptExtractor (precedence).

**EDGE:**
- Neither TS nor Ruby signals → None.
- Workspace-monorepo without root signals → handled by caller via `_detect_workspace_ts_monorepo`.

**GAP (untested):**
- No test for the ordering when an extractor's `can_handle()` raises.

**SLOP:**
- Precedence hardcoded — no test enforcing order with explicit assertion.

---

### `_detect_workspace_ts_monorepo(repo_root)` at line 226

**Existing tests:** `v0_5_3_monorepo_bootstrap_test.py`, `v0_5_6_discovery_hints_test.py`.

**GOOD:**
- Returns workspace_roots when root package.json has no TS deps but apps/* etc. have tsconfig.
- fanout_capped=True when >50 entries under a parent.

**BAD:**
- No package.json at root → returns ([], False).
- Root has tsconfig.json → returns ([], False).
- Root package.json has TS deps → returns ([], False).

**EDGE:**
- Root package.json unreadable (OSError) → returns ([], False).
- Parent dir read raises → continues.
- Mix of qualifying and non-qualifying children — only qualifying surface.

**GAP (untested):**
- No test for the fanout cap exactly at 50 vs 51.
- No test for the OSError branch on package.json.
- No test that the result list is sorted.

**SLOP:**
- 50-entry cap is _WORKSPACE_FANOUT_CAP, hardcoded.

---

### `_is_ts_workspace(workspace_dir)` at line 296

**Existing tests:** indirect via `_detect_workspace_ts_monorepo`.

**GOOD:**
- tsconfig.json present → True.
- package.json with `typescript`/`ts-node`/`vite` token → True.

**EDGE:**
- Both files missing → False.
- package.json unreadable → False.

**GAP (untested):**
- No test for the boundary "only tsconfig.json, no package.json".

**SLOP:**
- Token list is sparse (`typescript`, `ts-node`, `vite`); modern toolchains may add more (`tsx`, `bun`, etc.) without detection.

---

### `_glob_for_extractor(extractor)` at line 315

**Existing tests:** indirect via bootstrap tests.

**GOOD:**
- Ruby → `**/*.rb`.
- TypeScript → `**/*.{ts,tsx,js,jsx,mjs,cjs}`.

**EDGE:**
- Unknown extractor type — falls through to TS default.

**GAP (untested):**
- No test that the fallback returns the TS glob for an unknown language.

**SLOP:**
- Only two languages supported; switch-statement style.

---

### `BootstrapReport` dataclass at line 349 + `to_dict()` at line 455

**Existing tests:** every bootstrap test consumes the dict envelope.

**GOOD:**
- `to_dict` returns all expected keys.
- `archetypes_detected` = root + workspace total (BUG-011).
- Per-workspace breakdown in `archetypes_per_workspace`.

**EDGE:**
- Workspace with status≠"success" — excluded from sum.
- archetypes_per_workspace key for an empty workspace_path falls back to repo_root.

**GAP (untested):**
- No test for the `archetypes_per_workspace` exact key contents.
- No test that the `archetypes_detected_root` is reachable (vs the summed value).
- No test that `clustered_files` alias matches `files_processed`.

**SLOP:**
- Aliases (`clustered_files`, `archetypes_detected_root`) add cognitive load; no schema enforcing them.

---

### `_compute_repo_id(repo_root)` at line 505 (orchestrator-local wrapper)

**Existing tests:** indirect.

**GOOD:**
- Delegates to tools._compute_repo_id.

**GAP (untested):**
- No regression test that the two implementations stay in lockstep (the wrapper's whole purpose).

**SLOP:**
- Re-imports `tools._compute_repo_id` every call — minor overhead but stable.

---

### `_load_user_renames(profile_dir)` at line 527

**Existing tests:** indirect via `v0_5_1_critical_test.py`.

**GOOD:**
- Returns user-rename overlay from renames.json.
- Missing file → {}.
- Malformed → {}.
- schema_version > 1 → {}.

**EDGE:**
- Entries with non-string keys/values filtered.
- Empty string key/value filtered (line 555).

**GAP (untested):**
- No test for the empty-string filter.
- No test that the schema_version=0 case behaves.

**SLOP:**
- Duplicated tolerantly-permissive shape with `_read_renames_overlay` in tools.py — drift risk.

---

### `_generation_counter(now=None)` at line 559

**Existing tests:** indirect.

**GOOD:**
- Returns int seconds.
- Honors `now` override for determinism in tests.

**EDGE:**
- `now=0` → returns 0 (`int(0.0)`).
- `now=-1` → returns -1; doesn't enforce non-negative.

**GAP (untested):**
- No test for the override.

**SLOP:**
- Plain alias for `int(time.time())` — function could be inlined.

---

### `_displayed_paths_pattern(bucket, witness_relpath)` at line 596

**Existing tests:** indirect via `v0_5_3_canonical_witness_test.py`.

**GOOD:**
- Bucket "app/rule/action_executor" + witness "app/models/rule/action_executor/auto_categorize.rb" → "app/models/action_executor".
- Non-Rails witness → bucket unchanged.
- Witness with <4 segments → bucket unchanged.

**EDGE:**
- witness_parts[1] not in load-bearing set → bucket unchanged.
- Load-bearing segment already in bucket → no rewrite.

**GAP (untested):**
- No test enumerating all 13 load-bearing second segments.
- No test for the case where witness[0] != "app".

**SLOP:**
- Hardcoded list of 13 segments — no extension point for app teams.

---

### `_rel_or_abs(path, repo_root)` at line 650

**GOOD:**
- Returns relative path when possible.
- Falls back to absolute string.

**GAP (untested):** trivial helper; no targeted test.

---

### `_stringify_distribution_key(value)` at line 658

**GOOD:**
- True → "true", False → "false", None → "null".

**EDGE:**
- Integers → str(int).
- Floats → str(float).

**GAP (untested):**
- No test for stable ordering of dict-keys post-stringification.

---

### `_build_sparse_warnings(sparse_clusters, repo_root)` at line 680

**Existing tests:** `v0_5_6_sparse_warnings_cap_test.py`, `v0_5_2_clustering_test.py`.

**GOOD:**
- Aggregates by paths_pattern; collapses many small clusters at same bucket.
- Cap at 50 groups; truncated marker emitted when exceeded.
- Adaptive threshold rendered per group.

**EDGE:**
- All clusters at same bucket → one group with cluster_count=N.
- Thresholds differ within a group → range rendered "X-Y".
- Empty sparse_clusters → empty warnings list.

**GAP (untested):**
- No test for the threshold-range branch (mixed thresholds within a group).
- No test that the truncated marker carries correct shown/total counts.
- No test for the unknown bucket "(unknown)" fallback.

**SLOP:**
- 50-group cap (`_SPARSE_WARNING_LIMIT`) is hardcoded.

---

### `_build_bimodal_warnings(bimodal_clusters, repo_root)` at line 776

**Existing tests:** `v0_5_2_clustering_test.py`.

**GOOD:**
- Each warning carries dimensions, distribution, sample_paths.

**EDGE:**
- Distribution with mixed types (bool + None) → keys stringified.

**GAP (untested):**
- No test for distribution rendering with non-primitive value types.

**SLOP:**
- Threshold percent in reason text is computed but not asserted in tests.

---

### `bootstrap_repo(repo_root, *, paths_glob=None, profile_dir_name=".chameleon")` at line 814

**Existing tests:** all bootstrap tests.

**GOOD:**
- Single-root: returns single-tier report.
- Monorepo: includes workspace_reports.
- Re-detects workspace from committed root profile.

**BAD:**
- Initial bootstrap fails → returns failure report without workspace loop.

**EDGE:**
- Workspace alias to root (ws_root == repo_root.resolve()) — skipped.
- Workspace at a path that fails to resolve → skipped.

**GAP (untested):**
- No test for the workspace-alias skip branch.
- No test for the OSError branch on `ws_path.resolve()` (line 871).
- No test for the `_amend_root_profile_with_workspaces` rollback when a workspace bootstrap fails mid-loop.

**SLOP:**
- Two-pass shape (single bootstrap then workspace loop) — implicit; tests don't verify ordering.

---

### `_amend_root_profile_with_workspaces(profile_dir, workspace_reports)` at line 909

**Existing tests:** `v0_5_3_monorepo_bootstrap_test.py`.

**GOOD:**
- Re-emits profile.json with workspaces array.
- Re-emits sibling artifacts inside same txn for generation consistency.

**BAD:**
- profile.json missing → returns silently.
- profile.json malformed → returns silently.
- Sibling artifact unreadable → returns silently (corrupt profile).

**EDGE:**
- idioms.md missing → empty content.
- summary.md missing → empty content.
- renames.json present → re-emitted.

**GAP (untested):**
- No test for the renames.json preservation branch.
- No test for the sibling-artifact unreadable branch.

**SLOP:**
- Silent returns make diagnosis hard.

---

### `_bootstrap_single(repo_root, *, paths_glob=None, profile_dir_name=".chameleon")` at line 991

**Existing tests:** every bootstrap test.

**GOOD:**
- Full pipeline: workspace detect → tool config → extractor select → discover → parse → cluster → canonicals → write.

**BAD:**
- No extractor → `failed_unsupported_language` with discovery_hints.
- Too many files → `failed_too_many_files`.
- No source files → `failed_unsupported_language` ("No source files found...").

**EDGE:**
- Sidecar bootstrap (no own JS/Ruby signals, but parent has) → inherits tool configs from parent (BUG-019).
- BUG-014: walks up only when both `own_js` and `own_ruby` are False.
- BUG-003: ad-hoc monorepo fallback adopts first workspace's tool configs.
- BUG-NEW-021: drift.db baseline populated with one row per clustered file.
- BUG-NEW-005: nested .chameleon/ profiles in workspace subdirs surfaced.
- Reciprocal language hint (TS won + Gemfile present + ≥50 Ruby files) → secondary_detected="ruby".

**GAP (untested):**
- No test for the parent-walk-cap (4 dirs).
- No test for the workspace-tool-config fallback when first workspace has a partial config.
- No test for the drift baseline population failing silently.
- No test for the nested_profile_warnings format.
- No test for the reciprocal Gemfile threshold (50).
- No test that the workspace-roots detection runs before extractor selection for ad-hoc monorepos.
- No test for the `paths_glob` user-override interaction with workspace-only globs.

**SLOP:**
- `inherited_signals_from` walks 4 levels — magic number.
- 50-file Ruby threshold for the reciprocal hint — arbitrary; not configurable.
- Many fail-paths produce slightly different envelope shapes (with workspace_roots/fanout_capped/discovery_hints sometimes populated).

---

### `_extract_active_idioms(idioms_md)` at line 1649

**Existing tests:** indirect via `_build_summary_md` tests.

**GOOD:**
- Returns the body of the `## active` section.
- Empty when section absent.

**EDGE:**
- Section body is the placeholder → empty.

**GAP (untested):**
- No targeted unit test.

---

### `_extract_idioms_section(idioms_md, marker)` at line 1660

**Existing tests:** indirect.

**GOOD:**
- Returns body of any `## ` section.
- Placeholders treated as empty.

**EDGE:**
- Marker absent → empty.
- Body contains only placeholder → empty.
- Body contains `## ` boundary mid-section → split at first occurrence.

**GAP (untested):**
- No test for the placeholder detection logic at line 1676-1682.

**SLOP:**
- Placeholder list `("_(none)_", "no idioms yet")` hardcoded; future template changes would silently break.

---

### `_count_terminal_rules(block, depth=0)` at line 1686

**Existing tests:** indirect via summary.md tests.

**GOOD:**
- Counts leaf rules in a nested config dict.
- Cap at depth 6.

**EDGE:**
- block=None → returns 0.
- block contains list → counts list length.
- block depth=7 → returns 0 (cap fires).

**GAP (untested):**
- No test for the depth cap firing.
- No test for non-dict input.

**SLOP:**
- "Rough count" — not a real rule-count metric; surfaces in summary.md.

---

### `_build_summary_md(archetypes_data, canonicals_data, profile_data, idioms_md, rules_data=None)` at line 1707

**Existing tests:** `comprehensive_test.py` (`profile.summary.md has Generated header` etc.), `v0_5_2_lint_idioms_test.py`.

**GOOD:**
- Headers (Generated, Engine, Language, Source, Generation, Schema version).
- Archetype list with cluster_size and paths.
- Rules section with auto-derived count.
- Active idioms inlined.

**EDGE:**
- language_hint present → "## Secondary language detected" block.
- No idioms → placeholder note.
- Deprecated idioms present → separate section.

**GAP (untested):**
- No test for the language_hint rendering inside summary.md.
- No test for the deprecated idioms rendering.
- No test for the v0.5.4 fallback rules rendering when rules_data is None.

**SLOP:**
- Section ordering hardcoded — drift risk if reorganized.

---

## mcp/chameleon_mcp/bootstrap/tool_config.py

### `read_tool_configs(repo_root)` at line 69

**Existing tests:** `tool_config_v03_test.py`, `comprehensive_test.py`, `v0_5_7_workspace_tool_configs_test.py`, `v0_5_7_tsconfig_workspace_alias_test.py`.

**GOOD:**
- All five config types read: prettier, tsconfig, eslint, editorconfig, rubocop.
- JS-only `.prettierrc.js` triggers `has_prettier_js_plugins`.
- `sources` map populated per tool.

**BAD:**
- Files unreadable → silently skipped.
- Malformed JSON in `.prettierrc` → tries next variant.

**EDGE:**
- Multiple `.prettierrc*` variants present — first valid one wins.
- ESLint YAML and JS sibling present — JSON/YAML wins.
- tsconfig with `extends` array — resolved via chain.

**GAP (untested):**
- No test for `.prettierrc.js` JS-plugin signal.
- No test for the case where `result.eslint` is loaded from JSON but a JS sibling also exists (line 161-167).
- No test for `.rubocop.yaml` (yaml extension variant).

**SLOP:**
- 5 config types, 16+ file variants — silently first-match wins; no warning when multiple ambiguous.

---

### `_strip_jsonc_comments(text)` at line 197

**Existing tests:** `v0_5_7_tsconfig_workspace_alias_test.py` (BUG-NEW-012).

**GOOD:**
- Strips `//` line comments.
- Strips `/* */` block comments.
- Preserves `//` inside strings (BUG-NEW-012-redo).
- Strips trailing commas.

**EDGE:**
- Escape `\"` inside string — escape pair preserved verbatim.
- Escape `\\` inside string.
- Empty input → empty output.
- Comment containing `*/` — block comment ends at first `*/`.

**GAP (untested):**
- No test for the `\\` escape handling.
- No test for nested-looking string boundaries `"foo \" bar"`.
- No test for the empty input case.
- No test for incomplete block comment (no closing `*/`) — current code sets `i=n`.

**SLOP:**
- Single-pass scanner is hand-written; subtle escape bugs likely.
- Trailing-comma cleanup runs even outside strings — no edge protection.

---

### `_resolve_tsconfig_chain(tsconfig_path, repo_root)` at line 265

**Existing tests:** `tool_config_v03_test.py`, `v0_5_7_tsconfig_workspace_alias_test.py`.

**GOOD:**
- Single-level extends → merged config + chain populated.
- Multi-level extends → chain walks outward.
- Cycle detected → warning emitted, chain breaks.
- 8-hop cap → warning emitted.

**BAD:**
- Root config fails to parse → returns (None, [], "tsconfig.json failed to parse").
- Extends target unresolvable → warning emitted, chain breaks.

**EDGE:**
- `extends` is an array (TS 5.0+ syntax) — last entry continues the chain, earlier merged underneath.
- `extends` is mixed types (string + dict) — non-string entries skipped.
- visited set prevents infinite loops on shared parents.

**GAP (untested):**
- No test for the TS 5.0 array-form extends.
- No test for the 8-hop cap firing.
- No test for the resolved-cycle warning text.

**SLOP:**
- 8-hop cap hardcoded.
- Cycle detection uses `resolve()` for the visited key — symlinks could create false negatives.

---

### `_resolve_extends_target(target, from_path, repo_root)` at line 390

**Existing tests:** `v0_5_7_tsconfig_workspace_alias_test.py`.

**GOOD:**
- Relative path `./base.json` → joined with from_path.parent.
- Bare specifier `@tsconfig/strictest` → walks ancestor node_modules.
- Absolute path used as-is.

**EDGE:**
- Target ends with `.json` → no `tsconfig.json` suffix appended.
- node_modules walk stops at repo_root.
- Workspace-package fallback for `@org/pkg-name/path.json`.

**GAP (untested):**
- No test for the OSError on `ancestor.resolve()` (line 449).
- No test for the workspace-package fallback when no `pnpm-workspace.yaml` exists but the repo is npm workspaces.
- No test for the absolute target case (line 418-419).

**SLOP:**
- Multi-candidate search across (relative-as-given, with-`.json`, with-`tsconfig.json` suffix) — many redundant stats.

---

### `_workspace_monorepo_root(start)` at line 473

**Existing tests:** `v0_5_7_tsconfig_workspace_alias_test.py`.

**GOOD:**
- Finds pnpm-workspace.yaml.
- Finds package.json with `workspaces` field.
- Returns None if not found within 8 levels.

**EDGE:**
- start is already the workspace root.
- package.json unreadable → skipped (silent).

**GAP (untested):**
- No test for the 8-level cap firing.
- No test for an npm-only workspaces config.

**SLOP:**
- Hardcoded 8-level cap.

---

### `_resolve_workspace_package_target(target, from_path, repo_root)` at line 502

**Existing tests:** `v0_5_7_tsconfig_workspace_alias_test.py`.

**GOOD:**
- `@org/pkg/path` → finds `packages/pkg/path` or `apps/pkg/path` etc.
- Defaults to `tsconfig.json` or `tsconfig.base.json`.

**EDGE:**
- Target doesn't have `@org/` prefix → returns [].
- pkg_name empty → returns [].
- rest is None → default candidates only.

**GAP (untested):**
- No test for org-less specifiers (which return []).
- No test for `tsconfig.base.json` fallback.

**SLOP:**
- Workspace parent list hardcoded (packages, apps, services, workspaces).

---

### `_load_tsconfig_file(path)` at line 559

**Existing tests:** indirect.

**GOOD:**
- Reads, JSONC-parses, returns dict.
- Returns None on OSError or JSON error.

**EDGE:**
- Top-level is a list (not dict) → returns None.
- File is empty → JSON parses to None → returns None.

**GAP (untested):**
- No direct test for the non-dict top-level case.

---

### `_merge_tsconfig_into(target, source)` at line 569

**Existing tests:** `v0_5_7_tsconfig_workspace_alias_test.py`.

**GOOD:**
- compilerOptions dict-merged.
- Non-compilerOptions wholesale-replaced.
- Empty source → no-op.

**EDGE:**
- Source has compilerOptions but target doesn't → adopts as dict copy.
- Both have compilerOptions → shallow merge with source winning.

**GAP (untested):**
- No test for nested objects within compilerOptions (e.g., `paths`).
- No test for `extends` field stripped (orchestrator-side check).

**SLOP:**
- Shallow merge of compilerOptions — `paths` map gets replaced wholesale; consumers expecting deep merge may be surprised.

---

### `_safe_rel(path, repo_root)` at line 595

**GOOD:**
- Returns relative path string when possible.
- Falls back to absolute string.

**GAP (untested):** trivial helper.

---

### `_parse_eslint_yaml(path)` at line 608

**Existing tests:** `tool_config_v03_test.py`.

**GOOD:**
- Parses .eslintrc.yml.
- Returns (None, warning) on YAML errors.

**BAD:**
- PyYAML unavailable → returns (None, warning).
- File unreadable → returns (None, warning).
- Top-level not a mapping → returns (None, warning).

**EDGE:**
- Empty YAML → loads to None → "did not parse to a mapping".

**GAP (untested):**
- No test for the PyYAML-unavailable branch.

---

### `_parse_rubocop_yaml(path)` at line 625

**Existing tests:** `v0_5_6_rubocop_extractor_test.py`, `v0_5_7_rubocop_extraction_test.py`.

**GOOD:**
- Parses .rubocop.yml.
- Caps at 200KB.

**BAD:**
- Same as YAML eslint paths.

**EDGE:**
- 50KB cap mentioned in docstring but actual cap is 200KB — docstring/code mismatch.

**GAP (untested):**
- No test exercising the 200KB cap.
- No test for the YAML/YAML extension variant (.yaml not .yml).

**SLOP:**
- Docstring says ~50KB; code says 200_000 (200KB) — discrepancy.

---

### `_parse_eslint_js_via_node(path)` at line 659

**Existing tests:** `v0_5_6_eslint_cjs_test.py`, `v0_5_6_eslint_flat_test.py`.

**GOOD:**
- ESM/flat-config → dynamic import via Node.
- CJS → require via Node.
- Flat config (array) → merged into a dict with `flat:True`.

**BAD:**
- Node not on PATH → returns (None, "node not on PATH").
- Node eval times out (4s) → returns (None, ...).
- Node returns non-zero → returns (None, first-line stderr).
- Node output not JSON → returns (None, ...).

**EDGE:**
- Flat-config returning array of blocks → merged.
- Module exports a function (not object) → drops via JSON.stringify replacer; returns empty.

**GAP (untested):**
- No test for the timeout firing.
- No test for the flat-config merge logic.
- No test for the JS-but-functional config (functions stripped).
- No test for the empty stdout case.

**SLOP:**
- 4-second timeout hardcoded.
- Stderr truncated to 120 chars — error context lost.
- Relies on `node` being on PATH and capable of dynamic import.

---

### `_parse_eslint_js(path)` at line 770

**Existing tests:** `tool_config_v03_test.py`, `v0_5_6_eslint_cjs_test.py`.

**GOOD:**
- Tries node-eval first; falls back to regex parser.
- Regex parser handles simple `module.exports = { ... }`.

**BAD:**
- Unbalanced braces → returns (None, ...).
- JS-ish object not JSON-coercible → returns (None, ...).
- Top-level export is not an object → returns (None, ...).

**EDGE:**
- File contains `module.exports` AND `export default` — first match wins.
- File contains a regex literal with `{` → may confuse the brace scanner.

**GAP (untested):**
- No test for the regex-fallback path (Node always available in CI).
- No test for the unbalanced-braces error path.

**SLOP:**
- Best-effort regex parser is brittle — well documented but not gracefully degraded.

---

### `_scan_balanced_braces(text, start)` at line 814

**GOOD:**
- Tracks string state (single/double/template).
- Skips line/block comments.

**EDGE:**
- String literal spans lines → continues correctly.
- Escape `\\` inside string → handled.
- Unclosed string → walks to end and returns None.

**GAP (untested):**
- No test for backtick string with `${` interpolation — current code doesn't track interpolation; would miscount braces.
- No test for the unclosed comment case.

**SLOP:**
- Template string interpolation NOT tracked — `${foo({a:1})}` confuses the depth counter.

---

### `_jsish_to_json(text)` at line 878

**GOOD:**
- Quotes bare identifier keys.
- Converts single-quoted strings to double-quoted.
- Strips trailing commas.

**BAD:**
- Spread `...foo` → not handled; falls through.
- Function values → not handled; JSON parse fails.

**EDGE:**
- Single-quoted string with embedded `"` → escaped.
- Bare key with hyphen → not matched by `_JS_KEY_RE` (`[A-Za-z_$][A-Za-z0-9_$]*` excludes hyphens).

**GAP (untested):**
- No test for the hyphen-key case.
- No test for the spread-syntax fail.

**SLOP:**
- Single-quoted string regex doesn't handle multi-line strings.
- Comment-strip regex `(^|[^:])//[^\n]*` is fragile.

---

### `_parse_editorconfig(path)` at line 913

**Existing tests:** `tool_config_v03_test.py`.

**GOOD:**
- Reads sections and key=value lines.
- Returns {section: {key: value}}.

**EDGE:**
- Comments (`#`, `;`) ignored.
- Empty lines ignored.
- No section → values go under "root".

**GAP (untested):**
- No test for the leading-`;` comment.
- No test for inline comments (current code doesn't handle them).
- No test for keys with `=` in their value.

**SLOP:**
- Minimal parser; no support for glob-pattern section matching (the standard's main feature).

---

## mcp/chameleon_mcp/profile/trust.py

### `plugin_data_dir()` at line 20

**Existing tests:** `comprehensive_test.py` (HMAC tests use this).

**GOOD:**
- `CHAMELEON_PLUGIN_DATA` overrides default.
- Default is `~/.local/share/chameleon`.

**BAD:**
- `CLAUDE_PLUGIN_DATA` set but `CHAMELEON_PLUGIN_DATA` unset → ignored (per security comment).

**EDGE:**
- Override with `~` → expanded.
- Override empty string → ignored (falsy).

**GAP (untested):**
- No test for the empty-string override case.
- No test confirming `CLAUDE_PLUGIN_DATA` is ignored.

**SLOP:**
- Intentionally ignores `CLAUDE_PLUGIN_DATA` — non-obvious; relies on docstring.

---

### `repo_data_dir(repo_id)` at line 41

**GOOD:**
- Returns `${PLUGIN_DATA}/<repo_id>/`, creating if missing.

**BAD:**
- Permission denied on mkdir → propagates.

**EDGE:**
- repo_id contains `/` — would create nested dirs; no validation.

**GAP (untested):**
- No test for repo_id with path-traversal chars.
- No test for the permission-denied case.

**SLOP:**
- Silently creates the dir — caller may not expect side effect.

---

### `TrustRecord` dataclass + `from_dict` / `to_dict` / `hash_for_root` at line 49

**Existing tests:** `trust_flow_test.py`, `v0_5_1_trust_test.py`, `comprehensive_test.py`, `material_change_test.py`.

**GOOD:**
- Round-trip through to_dict/from_dict preserves all fields.
- hash_for_root returns specific hash when present, else profile_sha256.

**BAD:**
- Malformed `repo_root_specific_hashes` (non-dict) → empty map.
- Entries with non-string keys/values → filtered out.
- from_dict with missing keys → empty defaults.

**EDGE:**
- repo_root_specific_hashes empty → to_dict omits the field (byte-compat with v0.5.0).
- hash_for_root with path that fails `Path.resolve()` → uses raw string.

**GAP (untested):**
- No test for the OSError on resolve() in hash_for_root.
- No test that to_dict + sort_keys produces a stable hash.

**SLOP:**
- Defensive filtering in from_dict — fail-quiet on corruption.

---

### `hash_profile(profile_dir)` at line 144

**Existing tests:** `material_change_test.py`, `comprehensive_test.py`, `trust_flow_test.py`.

**GOOD:**
- SHA-256 over framed artifacts.
- Includes 5 artifacts: archetypes.json, canonicals.json, idioms.md, profile.json, rules.json.
- Returns "" if profile.json missing.

**EDGE:**
- All optional artifacts (idioms.md, rules.json, …) missing → only profile.json + archetypes.json hashed.
- Artifact present but empty file → still framed and hashed.

**GAP (untested):**
- No test that the 5 artifacts in alphabetical order produce a stable hash.
- No test that adding a new file changes the hash.
- No test for the "profile.json missing" empty-string return.
- No test that the framing (`\x00<filename>\x00`) prevents collision.

**SLOP:**
- Five-artifact list hardcoded; a 6th artifact added in the future would silently not influence the hash.

---

### `trust_state_for(repo_id)` at line 188

**Existing tests:** `trust_flow_test.py`, `comprehensive_test.py`, `v0_5_1_trust_test.py`.

**GOOD:**
- Reads `.trust` JSON; returns TrustRecord.
- Missing file → None.
- Malformed JSON → None.

**EDGE:**
- File exists but empty → JSONDecodeError → None.
- File contains valid JSON but wrong shape → from_dict tolerates; returns record with empty fields.

**GAP (untested):**
- No test for an empty `.trust` file.
- No test for a `.trust` containing valid JSON-array (not dict).
- No test that subsequent reads of the same record are stable.

**SLOP:**
- Side-effect: calls `repo_data_dir` which mkdirs.

---

### `grant_trust(repo_id, profile_dir)` at line 199

**Existing tests:** `trust_flow_test.py`, `v0_5_1_trust_test.py`.

**GOOD:**
- First grant: writes profile_sha256 + repo_root + seeds repo_root_specific_hashes.
- Same-root re-grant: refreshes top-level fields.
- Different-root grant under same repo_id: preserves original top-level, extends map.

**EDGE:**
- repo_root resolve fails (OSError) → uses str representation.
- atomic write via tmp file + os.replace.

**GAP (untested):**
- No test for the OSError on resolve.
- No test for the different-root-with-empty-existing-repo_root path (treats as same root per line 252).
- No test for the atomic-write rollback (tmp file orphaned on crash).

**SLOP:**
- granted_at refreshes on same-root re-grant but persists on different-root grant — inconsistent semantics.
- Multiple TrustRecord constructions per call.

---

### `revoke_trust(repo_id)` at line 282

**Existing tests:** `trust_flow_test.py`.

**GOOD:**
- Existing record → removes file, returns True.
- No record → returns False.

**EDGE:**
- File exists but permission denied to remove → propagates.
- File symlinked → `unlink()` removes link, not target.

**GAP (untested):**
- No test for the permission-denied case.
- No test that revoke removes the entire repo_data_dir (it does not — only `.trust`).
- No test that revoke does NOT remove drift.db.

**SLOP:**
- Only removes one file; other state under repo_data_dir persists.

---

### `is_material_change(repo_id, current_profile_dir)` at line 291

**Existing tests:** `material_change_test.py`, `comprehensive_test.py`.

**GOOD:**
- No trust record → False (not "material change").
- Hash mismatch → True.
- Hash match → False.
- Per-root hash consulted first.

**EDGE:**
- record.repo_root_specific_hashes has an entry but the current profile_dir resolves differently → falls back to profile_sha256.

**GAP (untested):**
- No test that the per-root map takes precedence when both top-level and map entries exist.
- No test for a record with corrupted hash.

**SLOP:**
- Hash mismatch alone is "material"; v0.5+ docstring says future versions should refine — flagged tech debt.

---

### `_current_user()` at line 313

**GOOD:**
- Returns `getpass.getuser()` result.

**BAD:**
- getpass raises → falls back to $USER env var.
- $USER unset → returns "unknown".

**EDGE:**
- $USER is empty string → returns "" (would surface in audit trail).

**GAP (untested):**
- No test for the getpass exception path.
- No test for missing $USER.

**SLOP:**
- Empty string accepted; should probably default to "unknown".

---

## mcp/chameleon_mcp/profile/loader.py

### `_version_tuple(v)` at line 26

**Existing tests:** indirect via `find_repo_root_test.py`, `v0_5_5_resolver_test.py`.

**GOOD:**
- `"0.5.7"` → (0, 5, 7).
- `"1"` → (1,).
- `"0.4.0-beta"` → (0, 4, 0) (non-digits stripped).

**EDGE:**
- Empty string → (0,).
- None → str(None) = "None" → (0,).
- Garbage `"abc"` → (0,).

**GAP (untested):**
- No direct unit test for the parsing edge cases.
- No test for comparison ordering (`(0, 4, 9) < (0, 5, 0)`).

**SLOP:**
- Tolerant parsing — a typo like "v0.5.7" would parse to (0, 5, 7) silently dropping the 'v'.

---

### `ProfileLoadError` exception at line 37

**Trivial; no tests needed beyond raise/catch.**

---

### `LoadedProfile` dataclass at line 42

**Existing tests:** indirect via `load_profile_dir` tests.

**GOOD:**
- Holds profile, archetypes, canonicals, rules, idioms_text, generation, profile_dir, mtime_token, archetype_names.

**GAP (untested):**
- No test for the `archetype_names` ordering (currently sorted at construction).

---

### `find_repo_root(file_path)` at line 70

**Existing tests:** `find_repo_root_test.py`, `v0_5_7_repo_root_resolution_test.py`, `comprehensive_test.py`.

**GOOD:**
- File inside repo with `.chameleon/` → returns repo root (pass 1).
- File inside workspace with `package.json` → walks up to find `.chameleon` (pass 2).
- File in nested dir without any marker → returns None.

**BAD:**
- file_path doesn't exist — uses parent dir for the walk.
- file_path is None — `Path(None)` raises.
- file_path resolve fails (OSError) → returns None.

**EDGE:**
- 32-level cap on walk.
- Multiple markers at same level — `REPO_ROOT_MARKERS` order determines priority (`.chameleon` wins).
- Workspace package.json with .chameleon at root (BUG-NEW-002 monorepo case) → pass 2 returns root.
- Tests leaking stray `.chameleon/` in tmp paths — defensive: only override when closer marker.

**GAP (untested):**
- No test for the 32-level cap firing.
- No test for the OSError branch.
- No test that .git wins when no .chameleon exists upstream.

**SLOP:**
- Two-pass algorithm — documented but easy to misread.
- 32-level cap matches no specific filesystem limit.

---

### `load_profile_dir(profile_dir)` at line 154

**Existing tests:** `comprehensive_test.py`, `v0_2_regression_test.py`, `v0_5_5_resolver_test.py`, `v0_5_6_schema_version_test.py`, `v0_5_7_schema_version_detect_test.py`.

**GOOD:**
- Reads 4 JSON artifacts + idioms.md.
- Double-fstat check.
- Generation counter consistency.
- engine_min_version satisfied.

**BAD:**
- Missing COMMITTED sentinel → raises ProfileLoadError.
- Missing required artifact → raises.
- Malformed JSON → raises ValueError (NOT wrapped in ProfileLoadError).
- Generation mismatch → raises ProfileLoadError.
- engine version too old → raises ProfileLoadError.
- schema_version > MAX_SUPPORTED → raises ProfileLoadError.

**EDGE:**
- mtime changes between before/after reads → raises (mid-load mutation).
- Generation values non-int → raises.
- idioms.md missing → empty text (silently).

**GAP (untested):**
- No test for the malformed-JSON-not-wrapped behavior.
- No test for the mtime mid-load mutation (would need orchestrated race).
- No test for the engine_min_version coming from archetypes.json (the OR-fallback at line 212).
- No test that the mtime_token format is stable.

**SLOP:**
- Some failures wrap as ProfileLoadError; JSON errors do not — inconsistent.
- 4 required artifacts hardcoded.
- Cache invalidation via mtime_token is text-encoded; no integer tuple comparison helper exposed.

---

## mcp/chameleon_mcp/bootstrap/transaction.py

### `_acquire_rename_lock(lock_path, *, timeout_seconds=30.0)` at line 50

**Existing tests:** `drift_concurrent_writes_test.py` (partial), `comprehensive_test.py`.

**GOOD:**
- Acquires exclusive flock; returns open fd.
- Blocks-and-retries until acquired or deadline.

**BAD:**
- Lock parent dir can't be created → raises.
- Deadline exceeded → raises TimeoutError.

**EDGE:**
- Concurrent writer holds the lock → blocks then succeeds.

**GAP (untested):**
- No test for the timeout firing.
- No test for the jitter randomization.
- No test for the lock-released-on-fd-close contract.

**SLOP:**
- Random jitter `0.05 + random()*0.05` — fixed range; not configurable.

---

### `atomic_profile_commit(target_dir)` context manager at line 77

**Existing tests:** `comprehensive_test.py`, `smoke_test.py`, `v0_5_2_bootstrap_test.py`, `v0_5_3_canonical_witness_test.py`, `interview_test.py`, `partial_refresh_test.py`, `cold_start_init_test.py`.

**GOOD:**
- All writes go to txn_dir.
- On clean exit: COMMITTED sentinel written + atomic rename.
- On exception: txn_dir removed; target_dir untouched.
- User-/team-sibling files (`.skip`, `.gitignore`) preserved.

**BAD:**
- No artifacts written → raises RuntimeError.
- Rename fails on macOS (target not empty) → uses backup pattern.
- Backup restore on rename failure.

**EDGE:**
- Concurrent commit → rename lock serializes.
- Sibling file has same name as a protocol file — protocol wins (line 130-135).
- Sibling is a dir → copytree.
- Sibling unreadable → skipped silently.

**GAP (untested):**
- No test for the backup-restore branch (rename failure mid-swap).
- No test for the OSError on sibling copy.
- No test for the symlink-sibling preservation.
- No test that fsync of COMMITTED actually flushes.

**SLOP:**
- Multiple cleanup paths (txn_dir, backup_dir) — drift-prone.
- "no artifacts" check requires at least one file in txn_dir; user could write a single dummy file and break the contract.
- Renames lock acquired even when rename(target) is trivial — overhead on single-writer case.

---

### `is_committed(target_dir)` at line 183

**GOOD:**
- True iff `target_dir/COMMITTED` exists.

**EDGE:**
- target_dir is a file (not dir) → False.
- COMMITTED is a directory not a file → `.is_file()` False.

**GAP (untested):**
- No test for the file-not-dir case.

---

### `_txn_dir_pid(txn_dir)` at line 194

**GOOD:**
- Extracts pid from `<pid>-<uuid>-<epoch>` name.

**BAD:**
- Name doesn't start with digits → None.
- Name has no hyphen → tries int(full_name), may succeed or fail.

**EDGE:**
- Empty name → split returns [""]; int("") raises → None.

**GAP (untested):**
- No targeted unit test.

---

### `_pid_alive(pid)` at line 207

**GOOD:**
- Live pid → True.
- Non-existent pid → False.

**BAD:**
- pid is 0 or negative → os.kill(0, 0) means "all processes in group"; semantics differ.

**EDGE:**
- Permission denied → True (conservative).

**GAP (untested):**
- No test for the permission-denied branch.
- No test for the pid=0 case.

**SLOP:**
- Conservative "permission denied → alive" may keep orphan dirs around indefinitely on multi-user systems.

---

### `cleanup_orphan_tmp_dirs(target_parent, profile_dir_name=".chameleon")` at line 218

**Existing tests:** `comprehensive_test.py`.

**GOOD:**
- Removes orphan txn dirs (no COMMITTED, dead pid).
- Returns count cleaned.

**BAD:**
- tmp_root missing → returns 0.
- Permission denied on rmtree → ignored (`ignore_errors=True`).

**EDGE:**
- Live writer's txn dir → skipped.
- Legacy dir without PID prefix → cleaned unconditionally.

**GAP (untested):**
- No test that a live writer's dir is preserved.
- No test for the legacy-format dir.

**SLOP:**
- `ignore_errors=True` swallows real cleanup failures.

---

## mcp/chameleon_mcp/drift/observations.py

### `_drift_db_path(repo_id)` at line 37

**Trivial; no targeted tests needed.**

---

### `record_edit_observation(repo_id, rel_path, archetype, confidence_band, *, matched_canonical=False, observed_at=None)` at line 41

**Existing tests:** `refresh_drift_test.py`, `drift_concurrent_writes_test.py`, `stubs_implemented_test.py`.

**GOOD:**
- Appends to edit_observations.
- Upserts files row.
- Trims when count exceeds HARD_CAP.

**BAD:**
- Empty repo_id → returns silently.
- sqlite init fails → returns silently.
- sqlite operation fails → returns silently.

**EDGE:**
- confidence_band=None → confidence=0.0.
- Unknown confidence_band string → confidence=0.0.
- HARD_CAP exceeded → 90-day age cleanup then SOFT_CAP truncate.
- observed_at=None → defaults to current epoch.

**GAP (untested):**
- No test for the HARD_CAP trim path.
- No test for the SOFT_CAP truncate after 90-day didn't help.
- No test for the upsert-on-conflict behavior.
- No test for the matched_canonical flag.
- No test for concurrent writes hitting the cap simultaneously.

**SLOP:**
- HARD_CAP=50K, SOFT_CAP=10K — magic numbers; no doc rationale.
- Trim happens on every insert past HARD_CAP — not amortized.

---

### `record_bootstrap_baseline(repo_id, clustered_files)` at line 130

**Existing tests:** none directly; added in v0.5.7 (BUG-NEW-021).

**GOOD:**
- Bulk-upserts files rows for every clustered file.
- Returns count written.

**BAD:**
- Empty repo_id → returns 0.
- Empty list → returns 0.
- sqlite error → returns count-so-far.

**EDGE:**
- File with archetype=None (sparse-dropped) → confidence=0.0.
- ON CONFLICT updates existing row.

**GAP (untested):**
- No direct test for this function (NEW in v0.5.7).
- No test for the partial-success case (some rows fail).
- No test for the confidence-band mapping for sparse-dropped (band="low" → 0.3).

**SLOP:**
- Returns rowcount on partial failure — could be misleading.
- Per-row INSERT instead of bulk executemany — slow on big repos.

---

### `compute_drift_score(repo_id, *, window_days=14)` at line 193

**Existing tests:** `refresh_drift_test.py`, `material_change_test.py`.

**GOOD:**
- 1 - mean(confidence) over window_days.
- Returns None if no observations.
- Clamped to [0.0, 1.0].

**BAD:**
- db_path missing → returns None.
- sqlite init fails → returns None.
- Query fails → returns None.

**EDGE:**
- window_days=0 → cutoff = now; no rows match; returns None.
- avg_conf is None (empty result) → returns None.
- avg_conf > 1.0 → clamped to 1.0.

**GAP (untested):**
- No test for the negative window_days case.
- No test for the clamp at upper bound.
- No test that the score reflects the time window correctly.

**SLOP:**
- 14-day default — arbitrary.

---

## mcp/chameleon_mcp/hook_helper.py

### `_emit(output)` at line 29

**Trivial; tested implicitly by every hook test.**

---

### `_plugin_data_dir()` at line 35

**Existing tests:** `optouts_test.py`, indirect.

**GOOD:**
- Honors `CHAMELEON_PLUGIN_DATA`.
- Defaults to `~/.local/share/chameleon`.

**GAP (untested):** mirrors trust.plugin_data_dir; ensure both stay in sync.

---

### `_should_emit_untrusted_prompt(repo_id, session_id)` at line 50

**Existing tests:** `v0_5_6_preflight_trust_gate_test.py` (BUG-024).

**GOOD:**
- First call returns True and creates marker.
- Subsequent calls in same session return False.

**BAD:**
- Empty repo_id or session_id → returns True (always prompt).
- Permission denied on mkdir → returns True (fail-open).

**EDGE:**
- Marker file exists from prior session → returns False (intended per-session).
- File creation race — `touch(exist_ok=True)` handles.

**GAP (untested):**
- No test for the permission-denied branch.
- No test for the cross-session behavior.
- No test for the marker file path traversal (session_id with `../`).

**SLOP:**
- Marker filename includes `{session}` directly — no sanitization.
- "Per-session" is filesystem-based; session ids must be unique across processes.

---

### `_emit_session_context(content)` at line 76

**Existing tests:** `bootstrap_mechanism_test.py`, `comprehensive_test.py`.

**GOOD:**
- Cursor → `additional_context`.
- Claude Code → `hookSpecificOutput.additionalContext`.
- SDK/Copilot → `additionalContext`.

**EDGE:**
- Both CURSOR_PLUGIN_ROOT and CLAUDE_PLUGIN_ROOT set → Cursor wins (first check).
- COPILOT_CLI overrides Claude Code path.

**GAP (untested):**
- No test for the COPILOT_CLI branch.
- No test that the Cursor path doesn't emit `hookSpecificOutput`.

**SLOP:**
- Three-way platform detection via env vars — fragile.

---

### `session_start()` at line 99

**Existing tests:** `bootstrap_mechanism_test.py`, `comprehensive_test.py`, `smoke_test.py`.

**GOOD:**
- Reads SKILL.md, wraps in `<chameleon-context>`, emits.

**BAD:**
- CLAUDE_PLUGIN_ROOT unset → empty emit.
- SKILL.md missing → empty emit.

**EDGE:**
- Skill file contains binary bytes → decoded with `errors="replace"`.

**GAP (untested):**
- No test for missing SKILL.md.
- No test for the binary-bytes branch.

**SLOP:**
- Reads file synchronously; no caching.

---

### `preflight_and_advise()` at line 127

**Existing tests:** `pretooluse_hook_test.py`, `comprehensive_test.py`, `smoke_test.py`, `daemon_test.py`, `v0_5_6_preflight_trust_gate_test.py`.

**GOOD:**
- Reads tool_input.file_path; calls daemon; falls back to in-process.
- Suppression check (opt-out) short-circuits.
- Drift observation recorded before trust gate.
- Trust gate: untrusted → one-time prompt; stale → context with warning; trusted → full context.

**BAD:**
- Malformed stdin JSON → empty emit.
- Missing file_path → empty emit.
- daemon call raises → falls back.
- In-process call raises → empty emit (fail-open).

**EDGE:**
- notebook_path used when file_path absent.
- Repo not detected → suppression check skipped via exception.
- Stale trust → block contains "**Trust is stale**" prefix.
- Untrusted prompt: emits "<chameleon-context>" block with /chameleon-trust suggestion only once per session.

**GAP (untested):**
- No test for the daemon-returns-non-dict case.
- No test for the drift recording failure (silent).
- No test for the notebook_path fallback.
- No test that the prompt-block is suppressed on second call in same session.
- No test for the COPILOT_CLI / Cursor envelope variant in preflight (only session_start handles it).
- No test for the truncation at 6000 chars on excerpt content.

**SLOP:**
- The 6000-char limit (line 306) is documented as "~1500 tokens" — no test confirms.
- Trust-state branching is shallow (4 cases: untrusted, stale, trusted, anything else) — anything else falls through to the trusted block.
- Suppression check imports happen inside try; an ImportError silently falls through (not necessarily desirable).
- Block-string concatenation is fragile.

---

### `posttool_recorder()` at line 325

**Existing tests:** `comprehensive_test.py`, `smoke_test.py`, `hmac_key_edge_cases_test.py`.

**GOOD:**
- Reads command + return code; appends HMAC-signed log.
- repo_id computed from CLAUDE_CWD or cwd.

**BAD:**
- Malformed stdin → empty emit.
- Append fails → empty emit (silent).

**EDGE:**
- exit_code is None → -1.
- CLAUDE_CWD set → wins over os.getcwd().

**GAP (untested):**
- No test for the silent-failure path.
- No test for CLAUDE_CWD overriding cwd.

**SLOP:**
- repo_id here is sha256(cwd) — NOT the canonical `_compute_repo_id` (no git-remote consideration). Diverges from trust paths.

---

### `callout_detector()` at line 385

**Existing tests:** `comprehensive_test.py`, `smoke_test.py`.

**GOOD:**
- Detects frustration phrases via 7 regex patterns.
- Emits hint with /chameleon-disable, /chameleon-pause-15m, /chameleon-teach.

**BAD:**
- No user_prompt → empty emit.
- Malformed stdin → empty emit.

**EDGE:**
- Solo "stop" — NOT matched (BUG-NEW-014).
- Stop variants ("stop injecting", "stop using") — matched.
- Profanity ("damn", "fuck", "shit") — matched.

**GAP (untested):**
- No test that "stop alone" is a false negative (intentional).
- No test that 7 patterns are all exercised.
- No test for the "don't do that" variant.
- No test for the `prompt` field fallback (vs `user_prompt`).

**SLOP:**
- 7 patterns hardcoded; no extension point.
- Frustration hint emitted unconditionally on any match — even when chameleon is unrelated.

---

### `main(argv=None)` at line 427

**GOOD:**
- Dispatches on `argv[0]`.

**BAD:**
- Unknown command → stderr + return 1.
- No args → stderr + return 1.

**EDGE:**
- Multiple args beyond command → silently ignored.

**GAP (untested):**
- No targeted unit test.

---

## hooks/session-start (bash)

**Existing tests:** `comprehensive_test.py` (runs the script), `smoke_test.py`.

**GOOD:**
- Finds python via `.venv` first, then `python3`, then `python`.
- Calls helper with `session-start` command.

**BAD:**
- No python available → emit `{}` and exit 0.
- Helper fails → emit `{}` and log to `~/.local/share/chameleon/.hook_errors.log`.

**EDGE:**
- CLAUDE_PLUGIN_ROOT unset → derived from `${0%/*}/..`.
- Log dir creation fails → suppressed via `|| true`.

**GAP (untested):**
- No test for the python-fallback chain (only `.venv` path is exercised in tests).
- No test for the error log rotation behavior — there is no rotation, only append.
- No test for the unset CLAUDE_PLUGIN_ROOT derivation.

**SLOP:**
- "Rotating error log" is just an append; no actual rotation.
- 2-second timeout via `timeout 2` — could mask transient issues.

---

## hooks/preflight-and-advise (bash)

**Existing tests:** `comprehensive_test.py`, `pretooluse_hook_test.py`, `smoke_test.py`.

**GOOD:**
- 2-second hard timeout via `timeout 2`.
- Fail-open: emit `{}` on any failure.

**BAD:**
- Helper timeout → `{}` emitted.
- Helper exits non-zero → `{}` emitted, error logged.

**EDGE:**
- PYTHONPATH already set → preserved + prepended.

**GAP (untested):**
- No test that the 2-second timeout actually fires on a hung helper.
- No test for the PYTHONPATH preservation.

**SLOP:**
- 2-second timeout is hardcoded.
- Error log grows unbounded (no rotation).

---

## hooks/posttool-recorder (bash)

**Existing tests:** `comprehensive_test.py`.

**GOOD:**
- Same shape as preflight.
- 2-second timeout.

**GAP (untested):**
- Same gaps as preflight.

---

## hooks/callout-detector (bash)

**Existing tests:** `comprehensive_test.py`, `smoke_test.py`.

**Same shape as preflight.** Same gaps.

---

## hooks/run-hook.cmd (polyglot)

**Existing tests:** none directly.

**GOOD:**
- Unix: execs bash with hook script.
- Windows: cmd.exe finds Git Bash, falls back to PATH bash.

**BAD:**
- No bash found on Windows → exits 0 silently.
- Missing script name → exit 1.

**EDGE:**
- Multiple bash candidates on Windows — first found wins.
- Hook script doesn't exist — exec fails with non-zero.

**GAP (untested):**
- No Windows CI to exercise the batch portion.
- No test that the polyglot heredoc trick (`: << 'CMDBLOCK'`) is portable across bash versions.
- No test for the silent-exit-on-no-bash behavior.

**SLOP:**
- Polyglot script is hard to maintain.
- Silently exits 0 when bash is missing — users can't tell why hooks aren't firing.

---

## hooks/hooks.json (manifest)

**Existing tests:** `comprehensive_test.py` (loads it), `bootstrap_mechanism_test.py`.

**GOOD:**
- 4 hook events registered.
- Matchers reasonable.

**EDGE:**
- PreToolUse matcher `Edit|Write|NotebookEdit` — exact regex.
- PostToolUse matcher `Bash|Edit|Write|NotebookEdit` — broader.

**GAP (untested):**
- No test that the matchers don't accidentally include unintended tools.
- No test that `${CLAUDE_PLUGIN_ROOT}` is correctly expanded at runtime.

**SLOP:**
- async:false for all hooks; no test of the contract.

---

## Cross-cutting GAPS and SLOP themes

These appear across many modules — flagging them once here rather than repeating per function.

### GAP themes

- **Fail-open contracts are not asserted.** Every helper says "swallow errors, emit empty" but no integration test injects faults at each level (daemon down, sqlite locked, JSON malformed, network gone).
- **Boundary conditions for caps are untested.** _WORKSPACE_FANOUT_CAP=50, _SPARSE_WARNING_LIMIT=50, REPO_SIZE_GUARD=200_000, _EDIT_OBS_HARD_CAP=50_000, _MAX_EXTENDS_HOPS=8, _MAX_AGE_SECONDS=30 days — each has a comment explaining the magic number but no test that the boundary fires precisely.
- **Concurrency tests are weak.** `drift_concurrent_writes_test.py` is the only place; transaction.py's rename lock, idioms.md flock, and trust grant atomicity all benefit from stress testing.
- **Schema-version negotiation is shallow.** MAX_SUPPORTED_SCHEMA_VERSION=7; tests cover v8 rejection but not v3/v4 acceptance with field-level migration.
- **Sanitization defense-in-depth is implicit.** ANSI / zero-width / tag-boundary are scattered across `sanitization.py`, `_sanitize_user_input`, and `sanitize_for_chameleon_context`; no single test ensures every user-facing path runs all three.
- **Path resolution semantics around symlinks** (especially macOS `/private/var/`) are handled at multiple sites with `try/except OSError` — no integration test for broken symlinks at the leaf.
- **Error message stability** — many functions return failed envelopes with human-readable strings; no test pins these strings (so cosmetic changes in commit messages can silently break consumer skills).
- **Timezone correctness.** `get_drift_status` uses `time.mktime` (local TZ); `_iso_to_epoch` uses `calendar.timegm` (UTC). No test exercises the local-vs-UTC offset effect.

### SLOP themes

- **Hardcoded numeric thresholds** appear ~30 times across the focus files. None are configurable via env or profile.json overrides.
- **Duplicated logic** in tools.py vs orchestrator.py (`_compute_repo_id`, `_hash_cluster_key`, `_load_user_renames` / `_read_renames_overlay`, `_build_summary_md` / `_rewrite_summary_md`, `_extract_active_idioms`). Drift risk: a fix on one side may not land on the other.
- **Silent fallthroughs.** `index_db.upsert_repo` errors swallowed (`except Exception: pass`); `record_bootstrap_baseline` errors swallowed; `_amend_root_profile_with_workspaces` returns silently on any error. Real failures are invisible.
- **Schema is partially documented in docstrings, partially in dataclass fields, partially in the README.** Three sources of truth — none is canonical.
- **Status envelopes are inconsistent.** Some use `{"status": "failed", "error": "..."}`; others use `{"stub": True, ...}`; canonical-excerpt uses `{"status": "no_witness", "reason": ...}`. Consumers must duck-type.
- **No structured logging.** All "fail-open" branches just `pass`; the bash hooks have an error log but the Python side does not.
- **Magic strings** (`"profile_corrupted"`, `"already_bootstrapped"`, `"failed_unsupported_language"`, `"untrusted"`, `"stale"`, `"trusted"`, `"n/a"`) — no central enum; typos would surface only on consumer side.

---

End of audit.
