# Chameleon Release Notes

## 0.1.0 — 2026-05-11

Initial release. Internal Empire Flippers use only.

### Highlights

- **Two-language support**: TypeScript (EF client) and Ruby on Rails (EF api).
- **15 MCP tools**: detect_repo, get_archetype, get_pattern_context, get_canonical_excerpt, get_rules, lint_file, get_drift_status, refresh_repo, bootstrap_repo, list_profiles, merge_profiles, teach_profile, trust_profile, disable_session, pause_session.
- **8 skills**: using-chameleon (auto-fires on SessionStart) plus 7 user-invoked slash commands (`/chameleon-init`, `/chameleon-refresh`, `/chameleon-status`, `/chameleon-teach`, `/chameleon-trust`, `/chameleon-disable`, `/chameleon-pause-15m`).
- **4 hooks**: SessionStart, PreToolUse (Edit/Write/NotebookEdit), PostToolUse (Bash), UserPromptSubmit.
- **Atomic profile commit**: `.chameleon/.tmp/<txn-id>/COMMITTED` sentinel + flock-serialized rename pattern, safe under concurrent bootstrap calls.
- **Trust + material-change flow**: trust_state values are `untrusted` / `trusted` / `stale` / `n/a`; preflight surfaces re-trust hint when stale.
- **Opt-out hierarchy**: `.chameleon/.skip` (per-repo) → `CHAMELEON_DISABLE=1` (per-user) → `/chameleon-disable` (per-session) → `/chameleon-pause-15m` (timed). All four wired and verified.
- **Drift tracking**: each Edit/Write hook records a confidence observation in `~/.local/share/chameleon/<repo_id>/drift.db`; `get_drift_status` returns `observed_drift_score` + recommended_action.
- **Git merge driver**: `scripts/chameleon-merge-driver.sh` integrates with `.gitattributes` for clean 3-way merges of `.chameleon/*.json`.
- **Security**: tag-boundary sanitization (closes 9 known evasion tokens including zero-width and NFC variants), poisoning scanner with security-context awareness, secret scanner (detect-secrets + fallback regex), HMAC-signed exec log with concurrent-safe key generation.
- **Performance**: EF api 4800 files → 149 archetypes in ~2.8s; EF client 2357 files → 7 archetypes in ~3.0s.

### Test coverage

Verified with 391+ tests across 12 test files including bash + Python:

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
| `claude_code_acceptance_test.py` | 26 — both EF stacks via real Claude Code |
| `all_commands_acceptance_test.py` | 42 — all 7 slash commands + 13 MCP tools × 2 stacks |
| `windows_dispatcher_test.py` | 12 — parity with superpowers' production-tested wrapper |

`tests/run_all_orders.py` runs the 5 core suites in 4 randomized orders to verify order-independence.

### Known gaps

- **Multi-hour stability**: not exercised. drift.db growth over weeks unverified.
- **50k-file repo at the cap**: ceiling exists in code, not exercised at scale.
- **Concurrent Claude Code sessions on the same repo**: paths exist, not stress-tested.
- **Marketplace publishing**: only verified via `--plugin-dir`; never published to a marketplace.
- **Daemon model**: subprocess-per-call hooks; daemon model deferred (Phase 4-end).

### Borrowed from superpowers

The Windows dispatcher (`hooks/run-hook.cmd`), bash test helpers (`tests/test-helpers.sh`), version automation (`scripts/bump-version.sh` + `.version-bump.json`), Windows docs (`docs/windows/polyglot-hooks.md`), and the `.cursor-plugin` / `.codex-plugin` / `gemini-extension.json` manifest shapes are adapted from [superpowers](https://github.com/obra/superpowers).
