# Journey Harness Design

Date: 2026-05-21
Status: draft v2, pending implementation plan
Reviewed: 3 expert reviews, 12 BLOCKs incorporated

## Context

The current `tests/` tree carries 95+ version-pegged `*_test.py` files (`v0_5_X_*`, `v0_6_1_fixes`, etc.) plus four parallel harnesses: `tests/dogfood/`, `tests/e2e/`, `tests/hook_evals/`, `tests/calibration/`. The unit tests are synthetic-fixture-driven and bug-shaped, written reactively per regression. They pass on CI but real Claude Code sessions still hit bugs. The synthetic floor encodes the author's mental model, not actual session behavior.

Decision: delete everything under `tests/` and rebuild as a single real-world journey harness. No synthetic unit tier. Every phase drives a real `claude -p` session against a real fixture repo. The bar is "if this passes, it works in a real Claude Code session."

This is the v2 design after 3 expert reviews (test architecture, implementation feasibility, operations) that flagged 19 BLOCKs, 27 FIXes, 25 NITs. The 12 BLOCKs flagged by 2+ reviewers are resolved below.

## Goals

1. Cover every chameleon surface end-to-end through real Claude sessions: **20 MCP tools** (server.py registers 20, not 18 as v1 spec claimed), 4 hook events with all matcher branches, 10 skills including `using-chameleon`, all v0.6.0 UX features (canonical_ref, auto_refresh, trust.auto_preserve_when, auto_rename), 30+ edge cases from prior version-pegged tests.
2. One sequenced flow from "no chameleon installed" to "chameleon fully exercised and uninstalled clean."
3. Failures attributed to a phase via a structured checkpoint file (not text-stream parsing). Later acts continue but may fail cascading from earlier breakage; the full pass/fail map across all 38 phases comes from one run.
4. Reproducible to the extent Claude allows. Real Claude is non-deterministic, so the harness's output is **probabilistic**, not deterministic. Mitigation: structured assertions on chameleon's filesystem state, not on Claude's prose; run before each release; if a phase fails inconsistently across 3 runs, suspect Claude variance not chameleon.
5. Total isolation from the developer's own chameleon usage. No writes to `~/.local/share/chameleon/`, `~/.claude/hooks/.exec_hmac.key`, or the global daemon socket outside the per-run ephemeral directory.
6. Cost ceiling ~$25 with hard mid-run abort. Runtime ~60 min. Replaces `tests/dogfood/`, `tests/e2e/`, `tests/hook_evals/`, `tests/calibration/`, all 95+ unit `*_test.py` files. Renames `/chameleon-dogfood` skill to `/chameleon-journey`.

## Non-goals

- No headless/cheap tier. If `claude` CLI isn't on PATH or fixtures are corrupted, the harness aborts.
- No `--from PHASE` flag. Acts run in order, full chain or nothing. Snapshots support post-mortem but not skip-ahead replay.
- No Cursor / Codex / Gemini CLI execution paths in v1. Manifests are installed so plugin recognition tests pass, but the journey drives Claude Code only.
- No value-attribution metrics testing. `architecture.md` references value_attrib.db but it is not implemented as a testable surface.
- No system-clock manipulation. Time-driven phases (drift cooldown, log GC, stale errors filter) test by directly setting filesystem mtimes via `os.utime`, not by changing the wall clock or mocking `time.time()` inside chameleon. This means we test "chameleon respects mtime thresholds," not "chameleon behaves correctly across actual elapsed time."
- No true concurrency assertions on the daemon. The v0.5 daemon is single-threaded (per `daemon.py:47`); we verify queue latency for serial processing, not parallel execution.
- No multi-user / concurrent harness runs. Two `runner.py` invocations against the same machine produce undefined behavior; the runner attempts to detect this via a lockfile and aborts.
- No coverage of the `clear` and `compact` SessionStart matcher branches. Claude Code does not allow synthetic triggering of `/clear` or `/compact` from inside an existing session. Phase 4 verifies the `startup` branch only (via the act's own `claude -p` spawn). Manual verification on the `clear` and `compact` branches is deferred to v2 or to release-process sanity testing.
- No CI integration in v1. The harness is human-triggered pre-release. CI hooks deferred to v2.

## Test isolation strategy

Every run gets a per-run directory under `tests/journey/results/journey_<timestamp>/`. All chameleon state goes there. Nothing touches the developer's home dir except read-only operations.

Environment overrides set by Act 0 before any subsequent act spawns:

```
CHAMELEON_PLUGIN_DATA=<run_dir>/chameleon_data       # overrides ~/.local/share/chameleon
CHAMELEON_HMAC_KEY_PATH=<run_dir>/exec_hmac.key      # overrides ~/.claude/hooks/.exec_hmac.key
TMPDIR=<run_dir>/tmp                                  # exec log lands here
CHAMELEON_JOURNEY_CHECKPOINT=<run_dir>/checkpoints/<act_id>.jsonl   # per-act phase attribution
CHAMELEON_HOOK_ERROR_LOG=<run_dir>/hook_errors.log   # hook fail-open log
```

Fixtures: committed seed repos under `tests/journey/fixtures/<name>/` are read-only. Act 0 copies them to `<run_dir>/working/<name>/` for per-run mutation. All subsequent acts work on the working copy. The committed seeds are NEVER touched.

Each seed fixture is a real git repo (not a bare repo) with `origin` configured to a sibling local path also under `<run_dir>/working/origin_<name>/`. Git operations against `origin/main` work offline without external network.

Fixtures to create (committed under `tests/journey/fixtures/`):

- `ts_basic/`: small TS repo with `src/`, `tsconfig.json`, `package.json`, `.eslintrc.js`, ~30 files across components/hooks/utils
- `rails_basic/`: Rails repo with `Gemfile`, `app/controllers`, `app/models`, `app/services`, `spec/`, `.rubocop.yml`, ~30 files
- `ts_monorepo/`: root + 2 workspace dirs (`packages/api/`, `packages/web/`), each with own `package.json`
- `ts_with_rails_sidecar/`: Rails skeleton + `client/` TS subdir, used to verify language_hint hybrid detection

Daemon isolation: the daemon socket path is constructed as `${CHAMELEON_PLUGIN_DATA}/.daemon.sock`. Since `CHAMELEON_PLUGIN_DATA` is per-run, the daemon is per-run. No conflict with the developer's running daemon.

## Failure attribution mechanism

Phase results are attributed via a structured checkpoint file, not text-stream parsing of Claude's output.

Per-act setup:

1. Runner creates `<run_dir>/checkpoints/<act_id>.jsonl` (empty).
2. Runner sets `CHAMELEON_JOURNEY_CHECKPOINT=<that path>` in the env passed to `claude -p`.
3. Each act's prompt includes a preamble instructing Claude to emit one JSON line per phase boundary via Bash. Checkpoint JSON schema (one event per line, JSONL):

```jsonc
{
  "phase": <int, 0-37>,             // phase ID from the inventory
  "status": "started" | "completed" | "failed",
  "ts": "<ISO 8601 UTC>",           // date -u +%FT%TZ
  "notes": "<optional 1-line string>"
}
```

   ```
   PHASE_START="$(date -u +%FT%TZ)"
   echo "{\"phase\": 12, \"status\": \"started\", \"ts\": \"$PHASE_START\"}" >> "$CHAMELEON_JOURNEY_CHECKPOINT"
   # ... run phase 12 steps ...
   echo "{\"phase\": 12, \"status\": \"completed\", \"ts\": \"$(date -u +%FT%TZ)\"}" >> "$CHAMELEON_JOURNEY_CHECKPOINT"
   ```

4. After the Claude session ends, the runner reads the checkpoint file line by line. Each line is parsed defensively: a malformed JSON line is logged as a warning and skipped, the runner never aborts on parse error. The per-act `checkpoint_parse_errors: int` counter in `run.json` records the count of skipped lines so operators can distinguish "phase was never started" from "checkpoint corruption."  Then the runner matches `started` + `completed` events against the act's expected phase list. Phases that started but didn't complete are marked FAIL with reason "phase incomplete." Phases never started are marked SKIP with reason "phase not attempted (likely upstream failure)." If `checkpoint_parse_errors > 0` for an act, SKIP-attributed phases include the note "(may be checkpoint corruption, check transcripts)."

5. Runner-side assertions (`expect.*`) verify chameleon's actual state. Claude's natural-language output is never parsed for assertion logic; it is preserved in the transcript for debugging only.

This approach survives Claude's text drift, code-fence rendering, and prompt rephrasing. If Claude forgets to echo a checkpoint, that phase is marked SKIP with an attribution-loss diagnostic, but the runner continues.

## Cost control

Per-act cost ceilings in the act table are estimates based on prompt token count and expected tool-call volume. Real Claude is non-deterministic and can exceed estimates.

Mitigation: mid-run abort. After each act completes, the runner computes:

```
projected_total = cost_so_far + sum(remaining_act_ceilings)
if projected_total > --max-budget-usd:
    abort with "budget would be exceeded: $cost_so_far spent + $remaining_estimate remaining > $max_budget"
    remaining phases marked SKIP with reason "budget exhausted"
```

`--max-budget-usd` defaults to $35 (a 40% safety margin over the $25 nominal). Operators can lower it for cautious runs (e.g., `--max-budget-usd 20` for a tight run that aborts early if Acts 0-4 burned more than budgeted).

Pre-flight check (existing): if the sum of all act ceilings exceeds `--max-budget-usd`, abort before any spawn.

Post-run report includes `cost_actual_vs_estimate` per act for tuning future estimates.

## Architecture

```
tests/
  journey/
    runner.py                   single entry point
    harness/
      context.py                JourneyContext: shared state, env setup, time helpers
      claude.py                 spawn_claude() wrapper: claude -p + stream-json parse + cost tracking
      bash.py                   spawn_bash(): subprocess wrapper for filesystem setup (used by runner directly, not by Claude prompt)
      checkpoints.py            checkpoint file parsing + phase attribution
      expect.py                 assertion helpers (file_exists, json_field, hook_fired, etc.)
      snapshots.py              capture state per phase
      fixtures.py               copy committed seeds to per-run working dir, set up git remotes
    acts/
      00_preflight.py
      01_install_mcp_doctor.py
      02_init_flow.py
      03_hot_path_drift.py
      04_v060_ux_bundle.py
      05_teach_status_doctor.py
      06_suppression_callout.py
      07_rails_parity.py
      08_hooks_security_sanitization.py
      09_schema_atomicity_concurrency.py
      10_daemon_observability_resilience.py
      11_uninstall_cleanup.py
    fixtures/                   committed seed repos (read-only at runtime)
      ts_basic/
      rails_basic/
      ts_monorepo/
      ts_with_rails_sidecar/
    results/
      journey_<timestamp>/
        run.json                machine-readable per-act + per-phase result
        run.md                  human-readable summary
        chameleon_data/         ephemeral CHAMELEON_PLUGIN_DATA
        exec_hmac.key           ephemeral HMAC key
        tmp/                    ephemeral TMPDIR
        working/                per-run mutable fixture copies + origin_<name>/ siblings
        checkpoints/<act>.jsonl per-act phase attribution
        transcripts/<act>.txt   Claude's full stdout per act
        snapshots/<act>/<phase>/  captured state per checkpoint
        hook_errors.log         hook fail-open log
```

Each act is a Python module exposing one function: `def run(ctx: JourneyContext) -> ActResult`. The function:

1. Sets up filesystem state via `ctx.spawn_bash(...)` for runner-side setup (planting configs, simulating state, time-mtime nudging). This is harness scaffolding, not part of the test surface.
2. Spawns one or more `claude -p` sessions via `ctx.spawn_claude(prompt, max_turns=N, allowed_tools=[...])`. Claude drives chameleon via natural Claude Code interactions (slash commands, file edits, Bash tool calls that exercise chameleon).
3. After Claude sessions end, runner-side `expect.*` assertions verify chameleon's actual state on disk + drift.db + via direct MCP calls when state introspection requires it.
4. Captures snapshots before assertions run.

The mixing rule: `ctx.spawn_claude()` drives the test surface (this is where chameleon is exercised). `ctx.spawn_bash()` and `ctx.call_mcp()` are harness instrumentation (setup + assertion). They MUST NOT be used to bypass the test surface (e.g., you cannot use `ctx.call_mcp("bootstrap_repo")` instead of having Claude run `/chameleon-init`, because that bypasses the user-facing flow). Direct MCP calls are allowed only for state introspection (e.g., `ctx.call_mcp("list_profiles")` to check what got registered) and for setup steps not user-facing (planting a v99 schema profile for migration testing).

Acts run in order. Failure in one act does not abort the chain; the runner records the failure and continues. Cascading failures in later acts are expected and acceptable.

## Time-driven phase mechanics

Phases that test time-driven behavior (7-day drift cooldown, 30-day exec log GC, 72h stale errors filter, 168h auto_refresh max_age) use direct mtime manipulation rather than waiting wall-clock time or mocking `time.time()` inside chameleon.

Pattern:

1. Trigger the time-gated behavior to write its marker file (e.g., drift banner cooldown writes `.drift_banner.last`).
2. Verify the marker exists.
3. Use `ctx.fast_forward_marker(path, age_seconds)` which calls `os.utime(path, (now - age_seconds, now - age_seconds))` to make the marker appear old.
4. Trigger the behavior again. Verify it fires (cooldown expired).

This means we test "chameleon respects mtime thresholds correctly," not "chameleon's clock arithmetic is correct." The latter is left to unit-level Python `time` module trust.

## The 12 acts

| Act | Phases | Cost ceiling | Runtime ceiling |
|---|---|---|---|
| 0. Pre-flight wipe + setup | 0 | $0.30 | 2 min |
| 1. Install + MCP boot + Doctor + using-chameleon verify | 1, 2, 3, 4 | $1.20 | 4 min |
| 2. Init flow (TS, both auto_rename modes + force=True) | 5, 6, 7, 15 | $3.00 | 7 min |
| 3. Hot path advisory + drift (Edit + Write + NotebookEdit) | 8, 9, 10, 11 | $3.00 | 7 min |
| 4. v0.6.0 UX bundle | 12, 13, 14 | $3.50 | 9 min |
| 5. Teach + Status + Doctor | 16, 17, 18 | $2.50 | 6 min |
| 6. Suppression + callout-detector | 19, 20, 23 | $2.00 | 5 min |
| 7. Rails parity | 21 | $3.00 | 7 min |
| 8. Hooks + security + sanitization | 22, 24, 25, 26 | $2.00 | 5 min |
| 9. Schema + atomicity + concurrency + monorepo | 27, 28, 29, 30, 31, 32 | $2.50 | 6 min |
| 10. Daemon + observability + resilience | 33, 34, 35, 36 | $2.00 | 5 min |
| 11. Uninstall + cleanup | 37 | $0.50 | 2 min |
| **Total** | **38 phases** | **~$25 nominal, $35 budget** | **~65 min** |

Each act's prompt skeleton appears below. The actual prompts in `acts/*.py` are longer and include the checkpoint preamble.

### Act 0: Pre-flight wipe + setup

This is mostly runner-side scaffolding, not Claude-driven.

Runner steps (no Claude):
1. Acquire `<run_dir>/.lock` (exclusive). If another runner is active, abort with "concurrent run detected."
2. Create `<run_dir>/{chameleon_data,tmp,working,checkpoints,transcripts,snapshots}/`.
3. Copy committed fixtures from `tests/journey/fixtures/<name>/` to `<run_dir>/working/<name>/`. For each, set up the loopback origin:

   ```bash
   cd <run_dir>/working/<name>
   # --initial-branch=main works on git >= 2.28; runner preflight verifies version
   git init --initial-branch=main -q
   git add -A && git commit -q -m "seed"
   git clone --bare . ../origin_<name>.git
   git remote add origin ../origin_<name>.git
   git fetch -q origin
   git branch --set-upstream-to=origin/main main
   ```

   This makes `git show origin/main:.chameleon/<artifact>` work offline against the bare loopback under `<run_dir>/working/origin_<name>.git/`. The runner's preflight check aborts with a clear error if `git --version` reports < 2.28 (since `--initial-branch` is unavailable). Committed seed fixtures under `tests/journey/fixtures/<name>/` are source-code-only (no `.git/` directory). Act 0 initializes them.
4. Set env vars: `CHAMELEON_PLUGIN_DATA=<run_dir>/chameleon_data`, `CHAMELEON_HMAC_KEY_PATH=<run_dir>/exec_hmac.key`, `TMPDIR=<run_dir>/tmp`, `CHAMELEON_HOOK_ERROR_LOG=<run_dir>/hook_errors.log`.
5. Verify the developer's actual `~/.local/share/chameleon/` and `~/.claude/hooks/.exec_hmac.key` are NOT touched. Emit `[checkpoint phase 0] setup complete`.

Phase 0 assertions: `expect.env_var_set("CHAMELEON_PLUGIN_DATA", under=run_dir)`; `expect.path_absent(<run_dir>/chameleon_data, must_be_empty_or_absent=True)`; `expect.no_chameleon_state_in_home()` (sanity guard).

### Act 1: Install + MCP boot + Doctor + using-chameleon verify

Claude prompt skeleton:
> Verify the chameleon plugin install. Use Bash to list `.claude-plugin/`, `.cursor-plugin/`, `.codex-plugin/`, `gemini-extension.json`, `hooks/hooks.json` and parse each as JSON. Verify required fields per manifest. Boot the MCP server: shell out to `mcp/.venv/bin/python -m chameleon_mcp.server` and send an `initialize` + `tools/list` JSON-RPC request via stdin/stdout. Verify all 20 tools are present with expected names. Run `chameleon-mcp::doctor`, report all 9 subsystems. Inspect `mcp/typescript-checksums.json` and verify entries match files under `mcp/node_modules/typescript/`. Finally, verify the SessionStart hook injected the `using-chameleon` skill body into your current context: run `printenv` and confirm `CLAUDE_PLUGIN_ROOT` is set; describe what skill content you can see. Emit checkpoint markers between sections.

Phase 1: 6 integration files parseable, `bump-version.sh --check` clean. Phase 2: tool list returns 20 (not 18 as v1 claimed). Phase 3: doctor returns ok across 9 subsystems. Phase 4: TS Compiler vendored SHA-256 verify; `using-chameleon` skill body is part of Claude's session context (verified via runner inspecting the stream-json initial system message for chameleon-context markers).

### Act 2: Init flow (TS, both auto_rename modes + force=True)

Claude prompt skeleton:
> Bootstrap two TS fixtures.
>
> First fixture: working/ts_basic. Plant `.chameleon/config.json` with `{"auto_rename": false}`. Run `/chameleon-init`. Step through the ≤3-prompt rename interview, accepting defaults. After bootstrap, verify COMMITTED sentinel + 4 profile artifacts + idioms.md + summary.md. Run `/chameleon-trust`, type the repo name. Verify trust granted. Then attempt to re-bootstrap: call `chameleon-mcp::bootstrap_repo` with `path=<fixture>` and no force flag. Expect status `already_bootstrapped`. Retry with `force=True`. Expect successful overwrite. Verify trust state flipped to stale due to profile SHA change.
>
> Second fixture: working/ts_monorepo (root with 2 workspaces). Plant `.chameleon/config.json` with `{"auto_rename": true}`. Run `/chameleon-init`. Verify NO interview surfaces. Verify only fallback names (cluster-*, class-*, numeric disambiguators) were auto-renamed; user-provided names preserved. Read `.archetype_renames.json` ledger, verify it lists the auto-renames with FIFO order. Inspect ledger entry cap: it should be capped at 256 (no concern for small fixture, but verify schema).
>
> Emit checkpoints between sections.

Phase 5: interactive interview ≤3 prompts. Phase 6: auto_rename skips interview, fallback names rekeyed. Phase 7: trust granted, `force=True` overwrite works, trust correctly flips stale on SHA change. Phase 15: archetype_renames.json ledger structure + FIFO cap.

### Act 3: Hot path advisory + drift (Edit + Write + NotebookEdit)

Claude prompt skeleton:
> In trusted `working/ts_basic`, perform three operations using THREE DIFFERENT tools across THREE archetypes:
>
> 1. EDIT an existing util file (e.g., `src/utils/format_date.ts`): change one line.
> 2. WRITE a new component file (e.g., `src/components/NewWidget.tsx`): create from scratch.
> 3. Edit a Jupyter notebook cell if one exists; otherwise WRITE a third file via the Write tool in the test/ archetype to satisfy the NotebookEdit-or-fallback matcher coverage.
>
> For each operation, before the tool call lands, the PreToolUse hook should inject a `<chameleon-context>` advisory. After your session ends, the runner will parse the stream-json hook events and verify: each PreToolUse fired, the advisory contained archetype + sub_buckets + match_quality (one of ast / exact / fallback / none) + canonical witness, total advisory stays under 1500 tokens. Hook-model dedup: the second edit in the same archetype within this session should skip injection.
>
> After the 3 operations, trigger drift by copying 50 files into `src/utils/` with intentionally unconventional naming (e.g., `src/utils/UNCONVENTIONAL-FILE-{1..50}.ts`). Start a fresh sub-session via `claude -p` (Bash tool) for the same fixture. Verify the SessionStart drift banner appears in the new session's context. Run `/chameleon-refresh`. Verify profile updates, trust preserved (structural-equality path: no rename, no idiom changes, only cluster size shifts).
>
> Emit checkpoints.

Phase 8: MCP read sweep transitively through Claude's tool calls; `match_quality` envelope populated. Phase 9: excerpt LRU hit on 2nd edit; 7-tuple cache key (repo_id, archetype, file_path, mtime_ns, size, inode, sha256) validated via post-read re-fstat. Phase 10: PreToolUse fires on Edit + Write + NotebookEdit (or Write fallback if no notebook present); advisory shape complete; hook-model dedup fires. Phase 11: drift banner surfaces in second session, manual refresh recovery.

### Act 4: v0.6.0 UX bundle

Claude prompt skeleton:
> Three independent v0.6.0 features against `working/ts_basic` (trusted from Act 2).
>
> 1. auto_refresh: plant `.chameleon/config.json` `{"auto_refresh": {"enabled": true, "drift_threshold": 0.2, "max_age_hours": 168}}`. Trigger drift past threshold by copying 30 unconventional files into `src/services/`. Fire a PreToolUse event by editing any tracked file. Runner verifies: `_maybe_auto_refresh` spawned a detached subprocess (Popen with `start_new_session=True`), `auto_refresh.log` was written under `<run_dir>/chameleon_data/<repo_id>/auto_refresh.log` (mode 0o600, ≤64KB cap, truncate-on-spawn), `.auto_refresh_cooldown` was touched AFTER the Popen returned (cooldown mtime > Popen call mtime, measured by runner). Fire another edit immediately; runner verifies cooldown blocks re-trigger (no new auto_refresh.log entry).
>
> 2. canonical_ref: ensure `working/ts_basic` has a `working/origin_ts_basic` loopback origin (set up by Act 0). Update config to `{"canonical_ref": "origin/main"}`. Modify working tree `.chameleon/canonicals.json` to differ from `origin/main`. Call `chameleon-mcp::get_pattern_context` (direct introspection by runner is OK here since this verifies a read path). Runner verifies: content came from `git show origin/main:.chameleon/canonicals.json` (cached under `<run_dir>/chameleon_data/<repo_id>/canonical/<ref-sha>/`), NOT from working tree. Trust check uses WORKING-tree hash, not canonical cache (v0.6.1 regression: bump working tree, verify trust state changes). Materialized canonicals.json passed through injection + secret scanners (plant a benign secret-looking string in the canonical version; verify scanner caught it). Bump `origin/main` HEAD via Bash (`cd origin_ts_basic && git commit ...`). Runner verifies old ref-sha cache dir gone, new ref-sha dir present after next get_pattern_context. Test unresolvable ref: set `canonical_ref: origin/nonexistent`. Verify fallback to working tree with stderr diagnostic logged.
>
> 3. trust.auto_preserve_when: update config to `{"trust": {"auto_preserve_when": "pulled_from_remote"}}`. In origin_ts_basic, make a commit to .chameleon/profile.json as user "teammate@example.com". Push to origin (loopback). In ts_basic, pull. Verify `git log -1 --format=%ae -- .chameleon/profile.json` returns teammate's email, not local user. Call `refresh_repo`. Runner verifies envelope has `trust_preserved: true, trust_preserve_reason: "pulled_from_remote"`. Then simulate same-author change: in ts_basic, edit profile.json as local user, commit. Verify trust drops (re-prompt required). Verify 2-second timeout on git log call: runner-side instrumentation plants a `git` shim that sleeps 5s before responding; verify refresh_repo returns within 3 seconds with timeout fallback (assumes not-pulled).
>
> Emit checkpoints throughout.

Phase 12: auto_refresh subprocess discipline, cooldown ordering. Phase 13: canonical_ref full lifecycle, v0.6.1 fixes verified (trust uses working-tree hash, scanner pass on materialized prose, gc_stale_caches, unresolvable fallback). Phase 14: trust.auto_preserve_when both paths + git timeout.

### Act 5: Teach + Status + Doctor

Claude prompt skeleton:
> Use `/chameleon-teach` to add a structured idiom: `slug: no-direct-axios`, `rationale: We wrap HTTP in src/lib/api.ts`, `example: import { api } from '@/lib/api'`, `counterexample: import axios from 'axios'`, `archetype: util`, `status: active`. Verify the slug passes the regex `\A[a-z][a-z0-9-]{0,63}\Z`. Test boundary: also try a 64-char slug (valid) and a 65-char slug (invalid, expect error). Verify the rationale + example + counterexample stays under 50KB per-idiom. Add 4 more idioms to push cumulative idioms.md past 100KB but under 200KB. Verify the 200KB cumulative cap by attempting a 6th idiom that would exceed it; expect error.
>
> Run `/chameleon-status`. Verify the output surfaces: profile summary, trust state with grantor + timestamp, drift score + recommended action, language hint, version coherence, and the v0.6.0 config block (canonical_ref + auto_refresh.enabled/threshold/max_age + trust.auto_preserve_when + auto_rename).
>
> Corrupt `.chameleon/canonicals.json` (truncate to 5 bytes via Bash). Run `/chameleon-doctor`. Verify it reports the corruption in the per-repo subsystem with `status: error`. Verify the stale errors filter (72h) honors `CHAMELEON_HOOK_ERROR_LOG`: write a known-old error entry (mtime via os.utime through Bash `touch -t`) and a fresh entry, verify only the fresh one appears in the doctor envelope.
>
> Emit checkpoints.

Phase 16: structured teach, slug regex + boundaries, 50KB per + 200KB cumulative caps, language-scoped frontmatter. Phase 17: status surface complete. Phase 18: doctor 9-subsystem report + 72h stale filter + env override.

### Act 6: Suppression + callout-detector

Claude prompt skeleton:
> Run `/chameleon-pause-15m` in `working/ts_basic`. Edit any file. Runner verifies no PreToolUse advisory injected (suppression honored). Use `ctx.fast_forward_marker(.pause_until, age_seconds=16*60)` to push the pause past expiry. Edit again. Runner verifies advisory fires again (pause expired).
>
> Run `/chameleon-disable`. Runner verifies HMAC-signed `.session_disabled.<session_id_hash>` marker written. Call `chameleon-mcp::disable_session` again for the same session_id (no force). Expect idempotent ok or refusal per the documented contract (runner reads server.py's actual contract). Call disable_session with an unknown session_id (no prior activity) without force=True; expect refusal. Retry with force=True; expect success. Verify disable_session requires the repo to be trusted; call it on an untrusted second fixture (`working/rails_basic`, not yet bootstrapped), expect error `{"status": "failed", "error": ".*not trusted.*"}`.
>
> Plant a forged `.session_disabled.<sid>` marker with wrong HMAC (use Bash to overwrite the marker with a known-bad signature). Edit a file. Runner verifies suppression NOT honored (downgrade attack defense).
>
> Test 4-level precedence by setup-and-edit cycles:
>   - Plant `.chameleon/.skip` (no other markers): edit, verify suppressed.
>   - Remove .skip, set `CHAMELEON_DISABLE=1`: edit, verify suppressed.
>   - Unset env var, plant valid session_disabled marker: edit, verify suppressed.
>   - Remove session_disabled, plant valid .pause_until (15m future): edit, verify suppressed.
>   - Remove all: edit, verify advisory fires.
>
> Send 7 user prompts (one per turn) containing frustration markers: `ugh stop`, `I hate this`, `damn it`, `this isn't right`, `don't do that`, `chameleon is slow`, `stop injecting`. Runner verifies callout-detector fires on each via UserPromptSubmit additionalContext containing `/chameleon-disable` / `/chameleon-pause-15m` / `/chameleon-teach` hints.
>
> Emit checkpoints.

Phase 19: pause + disable + 4-level precedence; pause expiry via mtime fast-forward. Phase 20: callout-detector 7 regex patterns. Phase 23: HMAC tampering rejected, force= flag, unknown session refusal, trust gate.

### Act 7: Rails parity

Claude prompt skeleton:
> Switch to `working/rails_basic` (Gemfile, app/controllers, app/models, app/services, spec/). Run `/chameleon-init`. Runner verifies Prism extractor ran (look for prism_dump.rb invocation in subprocess logs), `.rubocop.yml` rules extracted into `rules.json` under `rubocop` key, Rails priors applied (controller / model / service / spec archetype names, 15-entry table). Run `/chameleon-trust`. Make 3 edits across 3 Rails archetypes (one controller, one service, one spec). Verify advisory injection on each. Verify `looks_like_test_path_bias` correctly clusters `spec/**` files separately. Run `/chameleon-refresh`. Run `/chameleon-teach` with a Rails idiom (e.g., `no-direct-active-record-in-controllers`). Verify language-scoped to ruby via `Language:` frontmatter.
>
> Then test Rails+JS hybrid: switch to `working/ts_with_rails_sidecar` (has both Gemfile and package.json). Run `/chameleon-init`. Verify `language_hint` surfaces in SessionStart primer (v0.5.1 fix). Verify primary language detected matches the dominant tree.
>
> Emit checkpoints.

Phase 21: Rails parity, Prism extractor, .rubocop.yml extraction, Rails priors, hybrid language_hint.

### Act 8: Hooks + security + sanitization

Claude prompt skeleton:
> In trusted `working/ts_basic`, run a Bash command (`ls -la`). Runner verifies the PostToolUse exec recorder wrote an HMAC-signed log line to `${TMPDIR}/.chameleon_exec_log/<repo_id>/<sha256(session_id)[:16]>.jsonl`. Runner reads the line: HMAC matches (sha256 of canonical-JSON payload with key from `CHAMELEON_HMAC_KEY_PATH`). Key file mode is 0o600. Owner check passes. Runner-side test: use `ctx.fast_forward_marker()` to age a previous log file's mtime past 30 days. Call the exec_log GC function via `ctx.call_mcp` (or trigger naturally if there's an MCP tool for it). Verify the aged file is purged.
>
> PostToolUse on Edit, Write, and NotebookEdit: have Claude perform one Edit, one Write, and (one NotebookEdit if a notebook exists, else a third Write). Runner verifies each fires PostToolUse and exec log gets new entries.
>
> Plant a symlink at `working/ts_basic/src/utils/symlinked.ts` pointing outside the repo (use Bash `ln -s`). Make an Edit on `symlinked.ts`. Runner verifies discovery + extractor + safe_open_fd refused (O_NOFOLLOW), advisory fell back to a degraded banner (or no advisory at all). Verify O_NOFOLLOW worked: `safe_open_fd` returned `ELOOP` or equivalent.
>
> Plant adversarial `.chameleon/canonicals.json` with: bidi character U+202E, zero-width joiner U+200D, NFD-decomposed `<`, ANSI CSI escape `\x1b[31m`, C0 control byte `\x07`, dangerous tokens `</chameleon-context>`, `<system-reminder>`, `<|im_start|>`. Call `chameleon-mcp::get_pattern_context` (runner introspection). Verify the sanitization sweep replaced dangerous tokens with `[chameleon-sanitized: <token>]`, stripped bidi + zero-width + C0, NFC-normalized. The advisory header still emitted cleanly.
>
> Test 5MB boundary: plant a `.chameleon/canonicals.json` of exactly 4.99MB, 5.00MB, and 5.01MB. Runner verifies the 4.99MB version is accepted, 5.00MB and 5.01MB are rejected with sentinel framing in hash_profile.
>
> Emit checkpoints.

Phase 22: PostToolUse exec recorder HMAC scheme + GC + all matcher branches (Bash, Edit, Write, NotebookEdit). Phase 24: input sanitization sweep. Phase 25: symlink refusal via O_NOFOLLOW. Phase 26: adversarial profile + 5MB boundary.

### Act 9: Schema + atomicity + concurrency + monorepo

Claude prompt skeleton:
> Schema migration (use runner-side `ctx.spawn_bash` to plant fixtures, then `ctx.call_mcp` for introspection):
>
> 1. Plant a v0.3 profile.json in `working/ts_basic_v03_copy/` (separate copy). Call `detect_repo`. Verify migration runs OR refusal with clear error envelope (whichever the contract is).
> 2. Plant a v99 profile. Call `detect_repo`. Verify outright refusal with `unsupported_schema_version` envelope.
> 3. Plant a v0.5.8 profile (missing `clustering_algorithm_version`). Call `get_archetype`. Verify pre-v0.5.9 detection works.
>
> Atomic-txn recovery (deterministic alternative to mid-write SIGKILL):
>
> 1. Plant `.chameleon/.tmp/abc123/` containing a partial profile WITHOUT COMMITTED sentinel, and a sentinel pidfile with PID `99999` (guaranteed dead).
> 2. Call `bootstrap_repo`. Verify orphan-txn detection: the .tmp/abc123/ dir is cleaned (PID dead, no COMMITTED), bootstrap proceeds, no partial profile leaks.
> 3. Plant a second `.chameleon/.tmp/def456/` with a sentinel pidfile holding the CURRENT runner PID. Call `bootstrap_repo`. Verify NO cleanup (PID alive: presumed in-progress).
>
> Concurrent refresh (Claude-driven, then runner-verified):
>
> Have Claude open two Bash subshells via the Bash tool and run two parallel `mcp/.venv/bin/python -c "from chameleon_mcp.tools import refresh_repo; print(refresh_repo('<path>'))"` calls via `&`. Runner verifies via the captured outputs: one call returns success, the other returns `{"status": "failed", "error": "another /chameleon-refresh is in progress (PID <pid>); retry shortly"}` (envelope shape per `tools.py:2451`). The PID embedded in the error message matches the first call's PID.
>
> Glob brace expansion:
>
> Plant `discovery.paths_glob: "src/{components,hooks}/**/*.{ts,tsx}"` in profile.json. Call `refresh_repo`. Verify `_expand_brace_groups` recursive expansion enumerated 4 patterns. Test the 512-pattern cap: plant a pathological glob with 600 brace combinations, verify cap enforcement.
>
> Git merge driver:
>
> Create two divergent branches of profile.json in `working/origin_ts_basic` via Bash. Run the merge driver: `scripts/chameleon-merge-driver.sh` shells to `chameleon-mcp::merge_profiles`. Runner verifies the 3-way merge output is a clean union, no conflict markers.
>
> Monorepo aggregation:
>
> In `working/ts_monorepo`, run `/chameleon-init`. Runner verifies per-workspace bootstrap fired (each of `packages/api/` and `packages/web/` has its own .chameleon/), `index.db` PK is (repo_id, repo_root), no collision. Per-workspace counts: read `archetypes.json` from each workspace, get cluster_size per archetype. Read root profile.json's `workspaces` field; verify the per-archetype `cluster_size_total` equals the sum across workspaces for matching archetype names. Document the formula in profile.summary.md.
>
> Emit checkpoints.

Phase 27: schema migration. Phase 28: atomic-txn recovery (deterministic, no SIGKILL race). Phase 29: concurrent refresh + flock. Phase 30: brace expansion + cap. Phase 31: merge driver. Phase 32: monorepo aggregation with explicit sum formula.

### Act 10: Daemon + observability + resilience

Claude prompt skeleton:
> Daemon (runner-side, no Claude needed for assertions but Claude can trigger via Bash):
>
> Set `CHAMELEON_DAEMON_IDLE_TIMEOUT=600` (high) for the test duration. Start the chameleon daemon via Bash (`python -m chameleon_mcp.daemon &`). Runner verifies socket at `<run_dir>/chameleon_data/.daemon.sock` mode 0o600, pidfile exists. Call `chameleon-mcp::daemon_status`, verify alive + pid + uptime_s + last_request_at + socket_path. Make 3 serial calls via the socket using length-prefix framing (4-byte big-endian header + UTF-8 JSON, 1MB cap). Runner measures total time: should be ~3× single-call latency (single-threaded daemon, calls queue per `daemon.py:47`). Verify listen backlog 128 by attempting 50 connections in a tight loop and verifying no `ECONNREFUSED`. Then set `CHAMELEON_DAEMON_IDLE_TIMEOUT=2`, wait 4 seconds, verify daemon exits cleanly + pidfile removed. Explicitly kill any stragglers.
>
> Inspect `metrics.jsonl` from prior acts. Runner verifies per-call entries have: ts + hook + repo_id + elapsed_ms + advisory_emitted + suppression_reason + fail_open + trust_state + archetype + confidence.
>
> Log rotation: write 10MB of fake errors to `<run_dir>/hook_errors.log`. Trigger a hook event. Runner verifies rotation to `.hook_errors.log.1`, max 5 backups retained. Runner-side: use `os.utime` to age all 5 backups past 72h, trigger again, verify aged backups are pruned by the doctor stale filter (per Act 5 also). Verify `auto_refresh.log` truncates on each subprocess spawn (write 1MB junk, trigger auto_refresh, verify file is now small).
>
> Hook fail-open via Python fallback chain:
>
> Temporarily mask Python interpreters via PATH manipulation: `export PATH=/usr/bin:/bin` (no python). Trigger a SessionStart hook by spawning a new claude session. Runner verifies the bash fallback chain exhausted (count actual branches in `hooks/preflight-and-advise`: the spec previously claimed 8, actual is 6 per `hooks/preflight-and-advise:8-29`; reconcile in implementation), `{}` emitted, edit proceeded. Verify `hook_errors.log` captured the failure with timestamp + python version attempted.
>
> Windows polyglot: the spec does not currently test this. Document as out-of-scope for the v1 journey harness; Windows users get manual verification.
>
> Emit checkpoints.

Phase 33: daemon lifecycle + serial queue (not parallel) + idle shutdown. Phase 34: hook fail-open + Python fallback chain (actual branch count from preflight-and-advise). Phase 35: metrics emission. Phase 36: log rotation (10MB + 5 backups + 72h filter + auto_refresh.log truncate).

### Act 11: Uninstall + cleanup

Claude prompt skeleton:
> Uninstall chameleon from the ephemeral install. Use Bash to remove all 6 integration files (`.claude-plugin/`, `.cursor-plugin/`, `.codex-plugin/`, `gemini-extension.json`, `hooks/hooks.json`; the `.claude-plugin/` dir contains both plugin.json and marketplace.json). Wipe `<run_dir>/chameleon_data/` (per-run, NOT `~/.local/share/chameleon`). Verify daemon process is dead (from Act 10 cleanup). Attempt to call `chameleon-mcp::list_profiles`: should fail (MCP server unreachable post-uninstall). Verify no chameleon-related process is still running (`ps aux | grep chameleon | grep -v grep` returns nothing). Verify the developer's `~/.local/share/chameleon/` and `~/.claude/hooks/.exec_hmac.key` are STILL PRESENT (untouched by the harness). Emit final checkpoint phase 37.

Phase 37: clean uninstall; isolation verified (developer's home dir untouched).

## The 38 phases (coverage inventory)

| # | Phase | Surface area | Specific assertions |
|---|---|---|---|
| 0 | Pre-flight wipe + isolation setup | env vars, per-run dirs, fixture copies, loopback git origins | CHAMELEON_PLUGIN_DATA + HMAC_KEY_PATH + TMPDIR set, home dir untouched, fixtures copied to working dir |
| 1 | Plugin install | 6 integration files | all parseable, bump-version sync clean |
| 2 | MCP boot + tool list | server.py FastMCP | **20 tools** (corrected from 18) exact match |
| 3 | Doctor baseline | doctor() envelope | 9 subsystems status ok |
| 4 | Bootstrap resource limits + using-chameleon verify | TS Compiler subprocess + AST + vendor + skill injection | 5s CPU / 512MB / 1MB / 50k nodes / inode dedup / SHA-256 verify / using-chameleon body in SessionStart context |
| 5 | Cold-start init interactive | `/chameleon-init` legacy interview | ≤3 prompts, COMMITTED, all 4 json + idioms + summary |
| 6 | Cold-start init auto_rename | propose+apply_archetype_renames | fallback names rekeyed, user names preserved, no interview |
| 7 | Trust security | trust gate + per-(repo_id, repo_root) + SHA-256 + HMAC disable + force=True + downgrade defense + bootstrap force=True | all paths covered including force=True overwrite |
| 8 | MCP read sweep | every read-only tool of the 20 | match_quality envelope populated correctly |
| 9 | Excerpt LRU + TOCTOU | _excerpt_cache | 7-tuple key (repo_id, archetype, file_path, mtime_ns, size, inode, sha256), O_NOFOLLOW + O_CLOEXEC, post-read re-fstat, CONTEXT_TRANSFORM_VERSION bust |
| 10 | PreToolUse advisory + hook-model dedup | preflight-and-advise hook on Edit AND Write AND NotebookEdit | all 3 matcher branches fire; archetype + sub_buckets + match_quality + ≤1500 tok + dedup |
| 11 | Drift + manual refresh | drift.db + drift banner + refresh_repo | score threshold via fast_forward_marker, observation count, cooldown |
| 12 | auto_refresh subprocess | _maybe_auto_refresh | detached Popen, log mode 0o600, ≤64KB cap, cooldown AFTER Popen (mtime check) |
| 13 | canonical_ref lifecycle | canonical_loader + git show against loopback origin | materialize, ref-sha cache, scanner pass, trust uses WORKING-tree hash, gc_stale_caches, fallback to working tree on unresolvable ref |
| 14 | trust.auto_preserve_when | refresh_repo trust path | structural equality + git author check via loopback origin + 2s timeout (git shim) |
| 15 | auto_rename + ledger | archetype_renames.json | FIFO 256-cap, in hashed artifacts |
| 16 | Teach idiom | teach_profile + teach_profile_structured | slug regex (incl 64 + 65 char boundaries), 50KB per, 200KB cumulative, Language: scope |
| 17 | Status | /chameleon-status output | profile + trust + drift + lang_hint + version_coherence + v0.6.0 config |
| 18 | Doctor in session | doctor() with stale errors | 9 subsystems + 72h filter via fast_forward_marker + CHAMELEON_HOOK_ERROR_LOG override |
| 19 | Pause + Disable + precedence | pause_session + disable_session + .skip + CHAMELEON_DISABLE | 4-level precedence enforced; pause expiry via fast_forward_marker |
| 20 | Callout-detector | UserPromptSubmit | 7 frustration regex patterns enumerated: ugh / I hate / damn / this isn't right / don't do that / chameleon is X / stop injecting |
| 21 | Rails parity | Prism + .rubocop.yml + Rails priors (15-entry table) + language_hint hybrid | end-to-end on Rails fixture |
| 22 | PostToolUse exec recorder | posttool-recorder + exec_log on Bash + Edit + Write + NotebookEdit | HMAC scheme, key gen 0o600, 30-day GC via fast_forward_marker, owner check, all 4 matcher branches |
| 23 | HMAC tampering + disable_session security | optouts.py marker verification + tools.py disable_session | forged sig rejected, force= flag, unknown session refusal, trust gate refusal |
| 24 | Input sanitization | sanitization.py | bidi + zero-width + C0 + ANSI + NFC + dangerous tokens + sentinel framing |
| 25 | Symlink refusal | discover_files + safe_open_fd + extractors | O_NOFOLLOW enforced (ELOOP returned), fail-soft to degraded banner |
| 26 | Adversarial profile + 5MB boundary | secret_scanner + poisoning_scanner + safe_open size cap | oversized strings (4.99MB accepted / 5.00MB + 5.01MB rejected) + injection rejected |
| 27 | Schema migration | profile/schema.py + migrations/ | v0.3 / v0.4 / v0.5 / v0.6 read, v99 refuse, clustering_algorithm_version |
| 28 | Atomic-txn recovery (deterministic) | bootstrap/transaction.py orphan-txn cleanup | dead-PID sentinel → cleanup; alive-PID sentinel → no cleanup; no SIGKILL race |
| 29 | Concurrent refresh | locks.py + drift/sqlite_config.py | flock + WAL + busy_timeout 30000 + retry-jitter; `{"status":"failed", "error":".*PID <n>.*"}` envelope returned (shape per tools.py:2451), holding PID embedded in error |
| 30 | Glob brace expansion | _expand_brace_groups | recursive 512-cap, basename + dir braces |
| 31 | Git merge driver | scripts/chameleon-merge-driver.sh + merge_profiles | 3-way clean union on all 4 profile artifacts |
| 32 | Monorepo aggregation | index.db (repo_id, repo_root) PK + per-workspace bootstrap + sum formula | workspace cluster_size for archetype A sums to root profile.json workspaces[*].archetypes[A].cluster_size_total |
| 33 | Daemon | daemon.py serial queue + idle | socket 0o600, length framing 1MB, listen 128 (50-conn flood test), idle 600s default + 2s test, double-fork, pidfile, stale cleanup, daemon_status fields, 3-call latency ~3x single-call |
| 34 | Hook fail-open | bash fallback chain (actual branch count from preflight-and-advise:8-29) + malformed JSON + broken interpreter | exhaustion → {} emitted, error logged |
| 35 | Metrics emission | metrics.py | metrics.jsonl per-call fields complete |
| 36 | Log rotation | log_rotation.py | .hook_errors 10MB + 5 backups + 72h via fast_forward_marker + auto_refresh.log truncate-on-spawn |
| 37 | Uninstall + cleanup + isolation verify | (no module, harness-driven) | list_profiles empty after MCP teardown, all per-run artifacts gone, home dir UNTOUCHED |

## JourneyContext API

```python
@dataclass
class JourneyContext:
    plugin_root: Path                       # chameleon repo root
    run_dir: Path                           # tests/journey/results/journey_<ts>/
    plugin_data_dir: Path                   # <run_dir>/chameleon_data (CHAMELEON_PLUGIN_DATA)
    hmac_key_path: Path                     # <run_dir>/exec_hmac.key (CHAMELEON_HMAC_KEY_PATH)
    tmpdir: Path                            # <run_dir>/tmp (TMPDIR)
    fixtures: dict[str, Path]               # {"ts_basic": <run_dir>/working/ts_basic, ...}
    origins: dict[str, Path]                # {"ts_basic": <run_dir>/working/origin_ts_basic, ...}
    env: dict[str, str]                     # accumulated env for spawn_claude
    cost_so_far_usd: float
    act_results: list[ActResult]
    current_checkpoint_file: Path | None    # set per-act

    def spawn_claude(
        self,
        prompt: str,
        cwd: Path,
        max_turns: int = 25,
        allowed_tools: list[str] | None = None,    # default: all chameleon MCP tools + Read,Edit,Write,Bash
        timeout_s: int = 600,
    ) -> ClaudeSession:
        """Spawn `claude -p` subprocess with --output-format stream-json --verbose --include-hook-events.
        Sets env vars (PLUGIN_DATA, HMAC_KEY_PATH, TMPDIR, CHAMELEON_JOURNEY_CHECKPOINT) before spawn.
        Parses stream-json for cost, hook events, tool calls."""

    def spawn_bash(self, command: str, cwd: Path | None = None, timeout_s: int = 30) -> BashResult:
        """Runner-side bash for setup steps. Distinct from Claude's own Bash tool usage inside a session."""

    def call_mcp(self, tool_name: str, **args) -> dict:
        """Direct MCP tool call via stdio. RUNNER INSTRUMENTATION ONLY.
        Use for state introspection (verify what got registered), NOT for replacing user-facing flows.
        Bypassing /chameleon-init with bootstrap_repo() defeats the test purpose."""

    def snapshot(self, act_id: str, phase_id: int) -> Path:
        """Capture <run_dir>/chameleon_data + working/<fixture>/.chameleon snapshot under
        <run_dir>/snapshots/<act_id>/<phase_id>/. Called BEFORE assertions run."""

    def fast_forward_marker(self, path: Path, age_seconds: int) -> None:
        """os.utime(path, (now - age_seconds, now - age_seconds)) to simulate aged file.
        Used for time-driven phase testing without changing system clock."""

    def setup_git_shim(self, delay_seconds: float) -> ShimHandle:
        """Plant a fake `git` executable on PATH that sleeps before delegating to real git.
        Used to test trust.auto_preserve_when 2-second timeout (Phase 14).
        Returns a handle whose .restore() cleans up PATH on exit.
        ShimHandle supports context-manager protocol: `with ctx.setup_git_shim(5.0): ...`
        ensures PATH is restored even if the act raises mid-test. Bare `.restore()` is
        provided for explicit cleanup but the context-manager form is preferred."""

    def now(self) -> float:
        """time.time() wrapper. Reserved for future deterministic time mocking; currently passthrough."""

    def fixture(self, name: str) -> Path:
        """Returns <run_dir>/working/<name>/. Per-run mutable copy of the committed seed."""

    def origin(self, name: str) -> Path:
        """Returns <run_dir>/working/origin_<name>/. Loopback origin for git ops."""

    def projected_remaining_cost(self) -> float:
        """Sum of estimated ceilings for unstarted acts. Used by mid-run abort."""
```

`ClaudeSession` exposes:
- `.cost_usd` (from stream-json result envelope)
- `.hook_events` (parsed from stream-json `type:"system", subtype:"hook_response"` per the existing pattern in `tests/dogfood/scenarios/injection.py:104-119`)
- `.tool_calls` (full list of MCP and built-in tool invocations)
- `.transcript` (full stdout, preserved at `<run_dir>/transcripts/<act_id>.txt`)
- `.checkpoints` (parsed from `<run_dir>/checkpoints/<act_id>.jsonl`, NOT from text-stream)

`expect.*` helpers:

```python
expect.path_exists(phase: int, path: Path)
expect.path_absent(phase: int, path: Path)
expect.json_field(phase: int, path: Path, key: str, expected: Any)
expect.json_field_in(phase: int, path: Path, key: str, allowed: list)
expect.hook_fired(phase: int, session: ClaudeSession, event: str, count: int, matcher: str | None = None)
expect.advisory_contains(phase: int, session: ClaudeSession, archetype: str, match_quality: str)
expect.cost_under(phase: int, session: ClaudeSession, max_usd: float)
expect.mcp_envelope(phase: int, result: dict, status: str, required_keys: list)
expect.file_size_between(phase: int, path: Path, min_bytes: int, max_bytes: int)
expect.file_mode(phase: int, path: Path, mode: int)
expect.env_var_set(phase: int, name: str, under: Path)    # verify env var points under a base dir
expect.no_chameleon_state_in_home(phase: int)             # isolation guard
```

Every assertion takes `phase: int` so failures attribute correctly. Assertion failure raises `PhaseAssertionError` which the runner catches, records, and continues with the next phase.

## Runner CLI

```bash
mcp/.venv/bin/python -m tests.journey.runner                # full run, all 12 acts
mcp/.venv/bin/python -m tests.journey.runner --list         # list acts + phases, exit 0
mcp/.venv/bin/python -m tests.journey.runner --max-budget-usd 35  # mid-run abort if budget would be exceeded (default 35)
mcp/.venv/bin/python -m tests.journey.runner --dry-run      # validate setup (fixtures present, claude on PATH, etc.) without spawning Claude
```

No `--no-claude` flag. No `--cheap` flag. No `--only-act N` in v1 (reproducibility concerns flagged in review).

If `claude` CLI not on PATH or seed fixtures missing or another runner is active (lockfile contention), runner aborts with a clear error.

`--max-budget-usd` semantics:
- Pre-flight: if `sum(act_ceilings) > N`, abort before any spawn.
- Mid-run (after each act): if `cost_so_far + sum(remaining_act_ceilings) > N`, abort before next act, mark remaining phases SKIP.

## Output format

Per-act stderr stream:
```
[ACT 4] v0.6.0 UX bundle - starting (estimate $3.50)
[ACT 4] spawning claude session (max_turns=25, allowed_tools=[Read,Edit,Write,Bash,mcp__chameleon-mcp__*])
[checkpoint phase 12] auto_refresh subprocess discipline - PASS (1.2s, cooldown ordering verified)
[checkpoint phase 13] canonical_ref lifecycle - FAIL: trust used canonical cache hash, expected working-tree
[checkpoint phase 14] trust.auto_preserve_when - PASS (1.4s)
[ACT 4] complete (cost: $2.87 / $3.50 budgeted, duration: 7.4min, 2 PASS / 1 FAIL)
[BUDGET] projected remaining: cost_so_far $14.20 + remaining_estimates $10.50 = $24.70 (under $35 budget, continuing)
```

Final markdown table at `<run_dir>/run.md`:
```
| act | phase | name | status | duration | cost | notes |
|-----|-------|------|--------|----------|------|-------|
| 4 | 12 | auto_refresh subprocess discipline | PASS | 1.2s | $0.85 | cooldown ordering verified |
| 4 | 13 | canonical_ref lifecycle | FAIL | 0.8s | $1.20 | trust used canonical cache (v0.6.1 regression returned?) |
| 4 | 14 | trust.auto_preserve_when | PASS | 1.4s | $0.82 | git author check fired, 2s timeout honored |
```

Snapshots persist at `<run_dir>/snapshots/<act_id>/<phase_id>/`. The whole `<run_dir>` is the post-mortem artifact: snapshots + transcripts + checkpoints + hook_errors + run.json + run.md. Self-contained for sharing with a teammate.

`<run_dir>` is gitignored. Manually cleaned by the developer or auto-pruned by a future `runner.py --gc-old` flag (deferred to v2).

## Cost model

Per-run cost ceiling: ~$25 nominal, $35 hard budget. Runtime: ~65 min. Pre-release run, not per-commit. Expected cadence: before tagging a release, on any change to `hooks/` / `mcp/chameleon_mcp/` / `skills/`, weekly during active dogfood.

The estimate assumes prompt caching across acts (each act reuses chameleon-context, skill bodies). If cache miss rate is high (e.g., spec changes between runs), actual cost can reach $30. The $35 budget provides headroom.

Cost tracking per act is reported in `run.json` for tuning future estimates.

## Migration plan

Delete-and-rebuild, single PR:

1. Delete `tests/dogfood/`, `tests/e2e/`, `tests/hook_evals/`, `tests/calibration/`, all `tests/*_test.py` (95+ files), `tests/run_all_orders.py`.
2. Delete `tests/fixtures/` (the existing fixtures are not suitable; journey-specific fixtures replace them).
3. Build `tests/journey/` per this spec.
4. Create 4 committed seed fixtures under `tests/journey/fixtures/`.
5. Rename skill `chameleon-dogfood` to `chameleon-journey`. Update skill body to point at `tests/journey/runner.py`.
6. Update CLAUDE.md test commands section.
7. Update `.gitignore` to include `tests/journey/results/`.
8. Verify `bump-version.sh` has no version touchpoints in `tests/` (likely clean).
9. Audit `mcp/chameleon_mcp/` for ALL callsites that construct plugin-data paths. Required grep: `grep -rn "Path.home()\|.local/share/chameleon\|.claude/hooks" mcp/chameleon_mcp/`. Every match must either (a) route through `plugin_paths.plugin_data_dir()` (which honors `CHAMELEON_PLUGIN_DATA`), or (b) honor the appropriate env override (`CHAMELEON_HMAC_KEY_PATH`, `CHAMELEON_HOOK_ERROR_LOG`, `TMPDIR`). Round-3 audit confirmed compliance at: `daemon.py` socket path, `canonical_loader.py:117-119`, `metrics.py:21-26`, `exec_log.py:23-35`, `index_db.py:30`, `profile/trust.py:25-43`, `tools.py:5421` hook errors fallback, `hook_helper.py:85-88`. Re-run the grep after the spec lands to catch any new callsites added between now and merge.

No backwards-compat shim. The old test files are gone; no `pytest tests/` invocation survives.

## Open questions / out of scope

- Snapshot-based skip-ahead replay. Deferred to v2.
- `--only-act N` flag. Reproducibility broken by fixture mutation; punt to v2 with proper fixture-restore.
- Per-act parallelism. State dependencies prevent it without full isolation (would re-introduce synthetic mocking).
- Cursor / Codex / Gemini CLI execution paths. v1 drives Claude Code only.
- Value attribution metrics. Not implemented in chameleon yet.
- Calibration corpus. Tuning is orthogonal; defer to a separate tool if needed.
- Real Claude availability for CI. v1 is human-triggered locally. CI integration in v2 requires a paid API account or a Claude Code CI runner.
- System-clock manipulation for time-driven phases. We test mtime threshold respect via `fast_forward_marker`, not clock-skew behavior.
- True concurrent daemon calls (multi-threaded). v0.5 daemon is single-threaded; we verify serial queue latency. Multi-threading is a v2 chameleon feature.
- Multi-user / concurrent harness runs. Lockfile contention triggers an abort; we punt graceful handoff to v2.
- Windows polyglot hook wrapper (`run-hook.cmd` batch branches). Manual verification on Windows machines; not in the Linux/macOS-driven harness.
- Claude-non-determinism mitigation beyond "run 2-3 times before release." No statistical-bounds majority-vote in v1.
