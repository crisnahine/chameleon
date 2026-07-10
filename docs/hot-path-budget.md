# Hot-path budget

> The hot path is `get_pattern_context`, reached through the PreToolUse
> `preflight-and-advise` hook. Subsystem #15 of the correctness goal requires the
> hot path to be a **budget, not a tool**: a number it must stay under on every
> required cell, an enforced automatic invocation (not something Claude chooses to
> call), and a path that never blocks the user. This doc states the ceilings, cites
> where they live in code, and records the measurement method and the real numbers.

## The two surfaces of `get_pattern_context` (and which one is budgeted)

`get_pattern_context` exists twice, and only one is the enforced hot path:

- **Enforced hook path (budgeted).** PreToolUse `Edit|Write|NotebookEdit` →
  `hooks/preflight-and-advise` → `preflight_and_advise()` in
  `mcp/chameleon_mcp/hook_helper.py`, which calls `get_pattern_context` via the
  daemon first, then the in-process fallback (both inside that function). The
  daemon is a latency layer, not a correctness layer: a negative daemon answer
  (`no_repo` / `no_profile` / `profile_corrupted` /
  `profile_unsupported_schema_version`) is re-verified in-process rather than
  trusted — the version+fingerprint-keyed daemon socket is shared across sessions
  with its env frozen at spawn, so its negatives can be environment-stale. That
  means the budgeted path sometimes pays both the daemon call and the in-process
  compute within the same 3 s cap. This fires automatically on every qualifying
  edit. The user cannot opt out per call; it is bound by the shell `timeout`
  ceiling below.
- **Optional MCP tool (not the budget).** `get_pattern_context` is also registered
  as an MCP tool (the `@mcp.tool()` wrapper in `mcp/chameleon_mcp/server.py`,
  delegating to `tools.get_pattern_context`). This is a discretionary surface
  Claude may call during a task. Item 15's caveat is exactly this: verify the
  **hook** invocation specifically, because that is the enforced, budget-bound
  path — the tool surface is not.

## Ceilings (cited from code, not invented)

These are the real, already-enforced caps. No percentile-latency SLO (p50/p95 in ms)
is codified on top of them today; if one is ever wanted it must be chosen
deliberately and added here, not back-derived from a benchmark run.

| Ceiling | Value | Where | Behavior on breach |
|---|---|---|---|
| Claude Code hook timeout (outer) | **45 s** fast hooks / **60 s** Stop+SubagentStop | `hooks/hooks.json` (`"timeout": 45` on the five fast hooks, `"timeout": 60` on Stop/SubagentStop) | Claude Code kills the hook; fails open |
| Fast-hook hard timeout | **3 s** | `hooks/preflight-and-advise:75` (`timeout 3`); same for `session-start`, `posttool-verify`, `posttool-recorder`, `callout-detector` | Process killed; hook **fails open** (edit proceeds without chameleon) |
| Daemon per-call deadline | **1.5 s** | `DEFAULT_TIMEOUT_S` in `mcp/chameleon_mcp/daemon_client.py` | `call()` returns `None`; the hook falls back in-process, still inside the 3 s cap — a wedged daemon cannot eat the budget |
| Interpreter-resolver uv probe | **5 s** fast hooks / **30 s** SessionStart+doctor | `_cham_uv_ge_311` in `hooks/_resolve-python.sh`; the five per-edit/per-turn hooks set `CHAMELEON_RESOLVE_FAST=1` | Probe killed (`timeout(1)`, or the background poll loop where no timeout binary exists — the probe is bounded in fast mode even on Git Bash / coreutils-less macOS); ladder falls through; hook fails open. Warm sessions skip the ladder entirely: SessionStart's generous resolve writes `interp.cache` and each later resolve is a builtins-only cache hit |
| Stop/SubagentStop backstop | **55 s** | `hooks/stop-backstop:85` (`timeout 55`); wraps the ~45 s turn-end correctness judge | Killed; fails open |
| Statusline render | **< 100 ms** | `bin/chameleon-statusline.sh` — a design budget, not a runtime check. What the script bounds: the stdin read (256 KB, `head -c` at line 8) and the process count (one single-pass `jq` render, constant regardless of profile count) | No latency measurement or truncation; on any render failure it degrades to a `.chameleon` walk-up or a silent `exit 0` |

Notes:
- Each fast hook is capped twice: the hooks.json `timeout: 45` (outer) and the
  shell `timeout 3` (inner). The inner cap is the binding one; the outer exists so
  a broken shell wrapper still cannot hang the session. Stop/SubagentStop follow
  the same two-layer shape with more headroom: the shell's 55 s is the binding cap
  and the hooks.json `timeout: 60` is the outer safety net (per the comment in
  `hooks/stop-backstop` — a shorter inner cap would SIGKILL the judge mid-review).
- The 3 s cap is a *hard ceiling and a safety net*, not a target. The fast hooks
  degrade to uncapped when no usable `timeout`/`gtimeout` exists: missing from
  PATH (minimal environments), or Git Bash on Windows, where the wrapper skips
  the cap unconditionally (the MINGW/MSYS/CYGWIN `uname` branch — Windows'
  `timeout.exe` takes no command, so PATH is never consulted there);
  in-process code still self-limits (git 2 s, sqlite `busy_timeout`).
- The interpreter resolver does NOT share that degrade-to-uncapped shape on the
  per-edit path: under `CHAMELEON_RESOLVE_FAST=1` its uv probe is bounded even
  with no timeout binary (background poll loop, `kill -0` every 0.2 s, hard kill
  at ~5 s). Only the generous SessionStart/doctor probe keeps the old uncapped
  fallback, and it runs off the per-edit budget. `CHAMELEON_INTERP_CACHE=0`
  disables the cache; see `.claude/rules/environment-variables.md` for both flags.
- Fail-open is the invariant: a slow or broken hot path never blocks the edit and
  never corrupts the session. That is what makes "budget, not tool" safe.

## Measurement method

`tests/bench_hot_path.py` reports cold / warm **p50 and p99** (100 iterations) for
`get_pattern_context` (profiled and no-profile) and its sub-steps
(`find_repo_root`, `_compute_repo_id`, `_effective_profile_dir`,
`load_profile_dir`); archetype resolution has no standalone row — it is measured
only inside the collapsed `get_pattern_context` call. It also runs a ×30
multi-cold series (caches cleared each run) and times the turn-end duplication
gate's deterministic phase (catalog load + gather; the judge spawn is not timed).
It auto-discovers a profiled repo from the local bed (`CHAMELEON_TEST_TS_REPO` /
`CHAMELEON_TEST_RUBY_REPO`, then Testing Apps candidates) and uses the chameleon
repo itself as the no-profile baseline — one caveat: this repo now carries a
local gitignored `.chameleon/` profile, so on a machine that has it the
"(no profile)" rows are really a second profiled measurement. It **measures
only — it enforces no ceiling**; the ceilings above are the contract, the
benchmark is the instrument.

```bash
PYTHONPATH=. mcp/.venv/bin/python tests/bench_hot_path.py
```

The heaviest Tier-1 cell (per `docs/verification-matrix.md`) is the size check on
`gitlabhq` (large Rails); that is where the budget breaks first, so real-session
overhead must be measured there, not only on a toy repo.

## Measured reality (scaffolding, not a ceiling)

Run on a real profiled TypeScript repo (`excalidraw`, target
`excalidraw-app/CustomStats.tsx`), 100 iterations:

| Component | Cold p50 | Cold p99 | Warm p50 | Warm p99 |
|---|--:|--:|--:|--:|
| `get_pattern_context` (profiled) | 24.84 ms | 24.84 ms | 0.73 ms | 0.92 ms |
| `get_pattern_context` (no profile) | 30.79 ms | 30.79 ms | 7.54 ms | 11.23 ms |
| `get_pattern_context` profiled, multi-cold ×30 | 10.94 ms | 21.82 ms | — | — (max 22.80 ms) |

Reading: the enforced hot path runs roughly **100×–4000× under the 3 s ceiling**
(cold p99 ~25 ms, warm sub-millisecond). The turn-end duplication gate
(deterministic phase, ~232 ms p50) is a separate Stop-path cost bounded by the 55 s
backstop, not the per-edit hot path.

### Heaviest Tier-1 cell (gitlabhq, large Rails)

Measured on `gitlabhq` (target `app/uploaders/uploader_helper.rb`), 10 cold + 50 warm:

| | p50 | p99 |
|---|--:|--:|
| cold | 88.2 ms | 124.5 ms |
| warm | 30.7 ms | 31.7 ms |

Even on the largest repo in the bed, cold p99 is **124.5 ms — 24× under the 3 s
ceiling**. This is the cell where the budget breaks first, and it has wide headroom.
Still scaffolding (zero done-credit); subsystem #15 closes only on a human's
real-session observation here.

These numbers are developer scaffolding. Per the goal, they earn zero "done" credit:
subsystem #15 closes only when a human observes real-session overhead within budget
on every required cell (especially the heaviest, `gitlabhq`) and records it in
`docs/verification-matrix.md`.
