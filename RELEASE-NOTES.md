# Chameleon Release Notes

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
