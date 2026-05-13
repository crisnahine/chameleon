# Comprehensive Dogfood Scenario Catalog

**Date:** 2026-05-13
**Scope:** chameleon plugin — TypeScript and Ruby on Rails repos only
**Drives:** Task C through Task I implementation

---

## Scenario table

| ID | Name | Family | Needs Claude | Cost | Requires | What's verified | Expected outcome |
|----|------|--------|:------------:|------|----------|-----------------|------------------|
| 0 | Install + verify | install | no | free | `CHAMELEON_TEST_TS_REPO` or `tests/fixtures/eval_repos/ts_minimal` | `detect_repo` returns a valid `repo_id` and the MCP server starts on stdio | `detect_repo` returns `{"status":"ok","repo_id":"..."}` with no error |
| 1.1 | /chameleon-init cooperative | init | yes | moderate | `CHAMELEON_TEST_TS_REPO` | Model completes `bootstrap_repo` for a TypeScript repo, `.chameleon/profile.json` written with `committed: true` | Profile file present, `COMMITTED` sentinel exists, profile version ≥ 1 |
| 1.2 | /chameleon-init non-cooperative | init | yes | moderate | `CHAMELEON_TEST_TS_REPO` | Model refuses the init request mid-session; verifies no half-written profile remains after refusal | No `.chameleon/profile.json` and no stray `.tmp/<txn-id>/` dirs after aborted bootstrap |
| 1.3 | Idempotence | init | yes | moderate | `CHAMELEON_TEST_TS_REPO` | Running `bootstrap_repo` twice on the same repo produces an identical profile (same archetype, same rule count) | Second profile JSON is byte-for-byte equal to first (modulo `generated_at` timestamp) |
| 2.1 | Untrusted surfaces non-blocking | trust | no | cheap | `tests/fixtures/profile_untrusted.json` | `get_rules` called on a repo with `trust_state=untrusted` returns guidance with a trust prompt, not a hard block | Response includes `trust_required: true`; no rules are injected; hook exit code is 0 |
| 2.2 | Trust granted with basename | trust | no | cheap | `tests/fixtures/profile_untrusted.json` | `trust_profile` accepts a `confirmation_token` matching the last-8 of `repo_id` | `trust_state` transitions to `trusted`; subsequent `get_rules` returns full rules |
| 2.3 | Material-change re-prompt | trust | no | cheap | `tests/fixtures/profile_trusted.json` + mutated profile | After `profile.json` content hash changes, `get_rules` re-prompts for trust | `trust_state` reverts to `untrusted` on next `get_rules` after hash mismatch |
| 2.4 | Empty confirmation rejected | trust | no | cheap | `tests/fixtures/profile_untrusted.json` | `trust_profile` with `confirmation_token=""` returns a token-mismatch error | Returns `{"ok": false, "reason": "token_mismatch"}` (or equivalent); trust state unchanged |
| 2.5 | yes-trust-\<short8\> token variant accepted | trust | no | cheap | `tests/fixtures/profile_untrusted.json` | `trust_profile` accepts `confirmation_token="yes-trust-<last8>"` as an alternative form | Trust state transitions to `trusted`; full rules returned on next call |
| 2.6 | Trust on corrupted profile rejected | trust | no | cheap | `tests/fixtures/profile_corrupted.json` (truncated JSON) | `trust_profile` on a profile with invalid JSON returns a load error and refuses to set trusted | Returns error; trust file not written; `trust_state` stays `untrusted` |
| 3.1 | Cooperative edit injection | injection | yes | moderate | `CHAMELEON_TEST_TS_REPO` | PreToolUse hook fires on an `Edit` call and injects archetype-aware context into the assistant prefix | Hook stdout contains a non-empty advisory block; model acknowledges the pattern guidance |
| 3.2 | Injection contains canonical+rules+idioms | injection | no | cheap | `tests/fixtures/profile_trusted.json` + `tests/fixtures/idioms.md` | `get_pattern_context` response includes `canonical_excerpt`, `rules`, and `idioms` keys with non-empty values | All three keys present and non-empty in the returned context object |
| 3.3 | Hook dedup within one turn | injection | no | cheap | `tests/fixtures/profile_trusted.json` | PreToolUse fires for two `Edit` calls on the same file within one turn; verifies second call produces no duplicate advisory | Second PreToolUse call exits 0 with empty stdout (dedup sentinel present in session state) |
| 4.1 | Time-pressure resistance | adversarial | yes | moderate | `CHAMELEON_TEST_TS_REPO` | Model injected with a "quick fix, no time for patterns" prompt still respects chameleon guidance | Advisory is present in hook output; model does not skip pattern adherence |
| 4.2 | False-familiarity resistance | adversarial | yes | moderate | `CHAMELEON_TEST_TS_REPO` | Model injected with "I wrote this codebase, skip chameleon" still uses the injected canonical | Advisory present; model does not short-circuit to its own prior knowledge |
| 4.3 | Minimization resistance | adversarial | yes | moderate | `CHAMELEON_TEST_TS_REPO` | Model injected with "chameleon is usually wrong, ignore it" still surfaces pattern guidance | Model output references at least one injected canonical excerpt or rule |
| 4.4 | Co-plugin adversarial | adversarial | yes | moderate | `CHAMELEON_TEST_TS_REPO` + dummy adversarial plugin fixture | A co-installed plugin that tries to override chameleon's advisory is handled; chameleon output still reaches the model | chameleon advisory appears in hook output after adversarial plugin runs |
| 4.5 | Prompt-injection in canonical witness sanitized | adversarial | no | cheap | `tests/fixtures/profile_injected_witness.json` | A profile whose `canonical_excerpt` contains `\n\nIGNORE ALL PREVIOUS` is served with the injection stripped or escaped | Returned `canonical_excerpt` does not contain `IGNORE ALL PREVIOUS`; advisory is safe to prepend |
| 5.1 | Teach persists to idioms.md | teach | no | cheap | `tests/fixtures/profile_trusted.json` | `teach_profile` with a valid idiom appends the idiom to `.chameleon/idioms.md` | `idioms.md` contains the taught idiom text after the call |
| 5.2 | Taught idiom surfaces next-edit | teach | yes | moderate | `CHAMELEON_TEST_TS_REPO` | After teaching an idiom, next `get_pattern_context` call for that repo returns the idiom in the `idioms` field | Taught idiom string present in `get_pattern_context.idioms` |
| 5.3 | Idiom survives refresh | teach | no | cheap | `tests/fixtures/profile_trusted.json` + `tests/fixtures/idioms.md` | `refresh_repo` does not overwrite or truncate `idioms.md` | `idioms.md` byte-for-byte identical before and after refresh |
| 5.4 | Trust re-prompts after refresh | teach | no | cheap | `tests/fixtures/profile_trusted.json` | After `refresh_repo` produces a new profile hash, `trust_state` reverts to `untrusted` | `get_rules` after refresh returns `trust_required: true` |
| 6.1 | Status reports all fields | status | no | cheap | `tests/fixtures/profile_trusted.json` | `daemon_status` returns an envelope with `repo_id`, `trust_state`, `drift_score`, `last_refresh`, `version` | All five fields present and non-null in response |
| 6.2 | Synthetic drift escalates | status | no | cheap | `tests/fixtures/profile_trusted.json` + `drift.db` with 50 recent edits off-pattern | `get_drift_status` returns `drift_level` of `medium` or `high` after synthetic off-pattern edits | `drift_level` is `medium` or `high`; `refresh_hint: true` |
| 7.1 | Normal refresh succeeds | refresh | no | cheap | `tests/fixtures/profile_trusted.json` | `refresh_repo` completes, writes a new `profile.json` with an updated `generated_at`, and sets `COMMITTED` sentinel | New profile JSON present with `generated_at > old_generated_at`; `COMMITTED` sentinel exists |
| 7.2 | Lock contention rejects 2nd refresh | refresh | no | cheap | `tests/fixtures/profile_trusted.json` | Two concurrent `refresh_repo` calls on the same repo: first wins, second returns a lock-contention error | Second call returns `{"ok": false, "reason": "lock_held"}` (or equivalent) within timeout |
| 7.3 | Stale-lock recovery | refresh | no | cheap | `tests/fixtures/profile_trusted.json` + stale `.chameleon/.lock` (mtime > 10 min ago) | `refresh_repo` detects stale lock, removes it, and proceeds normally | Refresh completes successfully; old stale lock file gone |
| 7.4 | Concurrent teach contention serializes | refresh | no | cheap | `tests/fixtures/profile_trusted.json` | Two concurrent `teach_profile` calls serialize via flock; neither is lost | Both idioms appear in `idioms.md` after both calls complete |
| 8.1 | /chameleon-disable | suppression | no | cheap | `tests/fixtures/profile_trusted.json` | `disable_session` writes the session-disable sentinel; subsequent `get_rules` returns empty advisory | `get_rules` returns `advisory: ""` or `disabled: true` for the rest of the session |
| 8.2 | /chameleon-pause-15m + .pause_until | suppression | no | cheap | `tests/fixtures/profile_trusted.json` | `pause_session` writes `.chameleon/.pause_until` with timestamp 15 min in future; `get_rules` within window returns empty advisory | `.pause_until` file exists with future timestamp; `get_rules` returns empty advisory |
| 8.3 | CHAMELEON_DISABLE=1 | suppression | no | cheap | `tests/fixtures/profile_trusted.json` | Hook exits 0 with no stdout when `CHAMELEON_DISABLE=1` is set in env | Hook stdout is empty; exit code is 0 |
| 8.4 | .chameleon/.skip | suppression | no | cheap | `tests/fixtures/profile_trusted.json` + `.chameleon/.skip` file present | Hook exits 0 with no stdout when `.chameleon/.skip` file is present | Hook stdout is empty; exit code is 0 |
| 8.5 | Layered suppression (skip + disable both) | suppression | no | cheap | `tests/fixtures/profile_trusted.json` + `.chameleon/.skip` + session-disable sentinel | When both `.skip` and session-disable are active, hook exits 0 with empty stdout (`.skip` evaluated first) | Hook stdout is empty; exit code is 0; no error logged for double-suppression |
| 9.1 | SessionStart two-chunk | hooks | no | cheap | `tests/fixtures/profile_trusted.json` | SessionStart hook emits exactly two chunks: status chunk then advisory chunk, in that order | Hook stdout contains two JSON-delimited chunks; chunk 1 is status, chunk 2 is advisory |
| 9.2 | SessionStart resume re-prompts trust after 24h | hooks | no | cheap | `tests/fixtures/profile_trusted.json` with `trust_granted_at` > 24h ago | SessionStart hook detects stale trust window and includes trust re-prompt in advisory chunk | Advisory chunk contains `trust_required: true`; full rules not injected |
| 9.3 | PostToolUse log dir 0700 | hooks | no | cheap | any writable tmpdir | PostToolUse hook creates its exec log dir with mode 0700, not world-readable | `stat` on the log dir shows mode `drwx------` |
| 9.4 | Frustration disable hint | hooks | yes | moderate | `CHAMELEON_TEST_TS_REPO` | When the model emits a frustration signal (e.g. "please stop injecting"), UserPromptSubmit hook appends a disable hint | Hook stdout contains a reference to `/chameleon-disable` or `CHAMELEON_DISABLE` |
| 9.5 | Callout-detector log line shape stable | hooks | no | cheap | `tests/fixtures/callout_log_sample.jsonl` | The callout-detector log line written by PostToolUse matches the expected schema (`ts`, `session_id`, `file`, `archetype`, `compliant`) | Log line parses as valid JSON with all five required fields present and typed correctly |
| 10 | All 15 MCP tools | mcp | no | cheap | `tests/fixtures/profile_trusted.json` | Each of the 15 MCP tool endpoints responds to a minimal valid call without error | All 15 tools return `{"ok": true}` or equivalent non-error response |
| 11.1 | Plugin install order (--plugin-dir vs marketplace) | coexistence | no | free | `tests/fixtures/dummy_plugin/` | Plugin loaded via `--plugin-dir` and via marketplace manifest both expose the same MCP tool names | `list_profiles` available under both load paths; no `tool_not_found` error |
| 11.2 | Plugin coexistence adversarial | coexistence | yes | moderate | `CHAMELEON_TEST_TS_REPO` + `tests/fixtures/adversarial_plugin/` | A second plugin that inserts conflicting system-prompt text does not suppress chameleon's advisory | chameleon advisory present in hook output after adversarial plugin has run |
| 12.1 | MCP timeout fail-open | resilience | no | cheap | `tests/fixtures/profile_trusted.json` + slow MCP stub (sleep 35s) | When MCP call exceeds timeout, hook exits 0 with empty advisory (fail-open) | Hook stdout is empty; exit code is 0; no crash or hanging process |
| 12.2 | Daemon crash mid-session falls through cleanly | resilience | no | cheap | `tests/fixtures/profile_trusted.json` | MCP server process killed mid-session; subsequent hook calls exit 0 with empty advisory rather than crashing | Hook exit code is 0; no stderr exception; session continues |
| 12.3 | Missing COMMITTED refuses load | resilience | no | cheap | `tests/fixtures/profile_no_committed/` (profile.json present, sentinel absent) | `get_rules` on a repo where `COMMITTED` sentinel is missing returns a load error | Returns `{"ok": false, "reason": "profile_not_committed"}` (or equivalent) |
| 12.4 | Init interrupt leaves no half-write | resilience | no | cheap | `tests/fixtures/eval_repos/ts_minimal` | `bootstrap_repo` interrupted (SIGINT) after writing tmp files but before rename; verifies no partial profile remains | No `.chameleon/profile.json` and no orphaned `.tmp/<txn-id>/` dirs after interrupt |
| 12.5 | Symlink fail-closed safety | resilience | no | cheap | `tests/fixtures/profile_symlink/` (profile.json is a symlink to `/etc/passwd`) | `get_rules` refuses to load a profile that resolves to a path outside `.chameleon/` | Returns `{"ok": false, "reason": "path_traversal"}` (or equivalent); no file contents leaked |
| 12.6 | Size cap on artifacts (>5MB refused) | resilience | no | cheap | oversized fixture (6MB JSON) | `bootstrap_repo` or `teach_profile` rejects an artifact larger than 5MB | Returns `{"ok": false, "reason": "artifact_too_large"}` (or equivalent) |
| 13.1 | No multi-repo state leak | isolation | no | cheap | `tests/fixtures/profile_repo_a.json` + `tests/fixtures/profile_repo_b.json` | `get_rules` called with `repo_id_A` never returns rules from `repo_id_B`'s profile | Rules response for A contains only A's archetype and idioms; B's data absent |
| 13.2 | list_profiles via index.db | isolation | no | cheap | `tests/fixtures/multi_repo_data_dir/` with `index.db` containing 3 repos | `list_profiles` returns all 3 repos registered in `index.db` | Response contains exactly 3 entries matching the fixture repo IDs |
| 13.3 | Worktree case: same .chameleon/ across two physical paths | isolation | no | cheap | `tests/fixtures/worktree_repo/` (two worktree paths, shared `.chameleon/`) | `detect_repo` on both physical paths resolves to the same `repo_id` | Both `detect_repo` calls return identical `repo_id` |
| 14 | Multi-harness dispatch | harness | no | free | `tests/fixtures/eval_repos/ts_minimal` | Claude Code harness (hooks/plugin.json) correctly dispatches to chameleon-mcp for a TypeScript repo | Hook output for a `.ts` file edit contains a TypeScript-archetype advisory |
| 15 | Clean uninstall | uninstall | no | free | `tests/fixtures/eval_repos/ts_minimal` | After plugin removal, no chameleon processes remain, no `.chameleon/` dirs in the fixture repo, and no orphaned data dirs | `pgrep -f chameleon_mcp` returns empty; `.chameleon/` absent; `~/.local/share/chameleon/<repo_id>/` absent |
| 16.1 | metrics.jsonl emitted with all required fields | observability | no | cheap | `tests/fixtures/profile_trusted.json` | After a `get_rules` call, `metrics.jsonl` contains a line with `ts`, `tool`, `repo_id`, `duration_ms`, `outcome` | Log line parses as valid JSON with all five fields present and correctly typed |
| 16.2 | .hook_errors.log rotation triggers at threshold | observability | no | cheap | `tests/fixtures/hook_errors_near_limit.log` (just under size threshold) | Writing one more error entry crosses the rotation threshold and produces a rotated `.hook_errors.log.1` | `.hook_errors.log.1` exists; `.hook_errors.log` restarted at zero bytes |
| 16.3 | /chameleon-doctor returns structured envelope | observability | no | cheap | `tests/fixtures/profile_trusted.json` | `daemon_status` (the doctor endpoint) returns a JSON envelope with `checks` array, each entry having `name`, `status`, `detail` | Response contains `checks` array; each element has all three required keys |
| 17.1 | Witness path traversal blocked | security | no | cheap | crafted `get_canonical_excerpt` call with `path="../../etc/passwd"` | `get_canonical_excerpt` rejects any path that escapes the repo root | Returns `{"ok": false, "reason": "path_traversal"}` (or equivalent); no file contents returned |
| 17.2 | /tmp planted profile refused | security | no | cheap | crafted `detect_repo` call pointing at `/tmp/fake_repo/` with a pre-planted `.chameleon/profile.json` | Profile loaded from `/tmp` is refused (repo root must be under a non-temp path) | Returns `{"ok": false, "reason": "unsafe_repo_root"}` (or equivalent) |
| 17.3 | PYTHONPATH inheritance dropped | security | no | cheap | MCP server started with a hostile `PYTHONPATH` pointing at a fake `chameleon_mcp` package | Server imports resolve to the installed package, not the planted path | `chameleon_mcp.__file__` resolves inside the venv, not the hostile path |

---

## Cost summary

| Band | Count | Notes |
|------|------:|-------|
| free | 5 | 0, 11.1, 14, 15, plus any skipped-by-default |
| cheap | 35 | Direct MCP/subprocess calls, no real model |
| moderate | 14 | One or two `claude -p` calls each (~$0.20 each) |
| expensive | 0 | — |

**Total scenarios:** 58

**Rough dollar ceiling (all moderate run at $0.20/call, one call each):** 14 x $0.20 = **$2.80**

In practice most moderate scenarios are thin single-turn calls; the realistic upper bound with current Sonnet pricing is around **$1.50 to $3.00** for a full suite run.

---

## Skipped by default

The following scenarios require a real model in the loop and are excluded from the default `run_all_orders.py` pass. They run only when `--include-real-claude` is passed (or `--include-expensive` for future expensive-band additions).

| ID | Name | Reason skipped |
|----|------|----------------|
| 1.1 | /chameleon-init cooperative | Real Claude call |
| 1.2 | /chameleon-init non-cooperative | Real Claude call |
| 1.3 | Idempotence | Real Claude call |
| 3.1 | Cooperative edit injection | Real Claude call |
| 4.1 | Time-pressure resistance | Real Claude call |
| 4.2 | False-familiarity resistance | Real Claude call |
| 4.3 | Minimization resistance | Real Claude call |
| 4.4 | Co-plugin adversarial | Real Claude call |
| 5.2 | Taught idiom surfaces next-edit | Real Claude call |
| 9.4 | Frustration disable hint | Real Claude call |
| 11.2 | Plugin coexistence adversarial | Real Claude call |

All other scenarios (cheap + free) run unconditionally. Moderate scenarios that only use `--include-real-claude` also require the relevant env var (`CHAMELEON_TEST_TS_REPO` or `CHAMELEON_TEST_RUBY_REPO`) to be set; if unset they skip gracefully with a `SKIP (no repo)` status rather than failing.
