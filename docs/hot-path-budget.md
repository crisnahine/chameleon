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
  `hooks/preflight-and-advise` → `hook_helper.preflight_and_advise()`
  (`hook_helper.py:1987`), which calls `get_pattern_context` via the daemon
  (`hook_helper.py:2095`) or the in-process fallback (`hook_helper.py:2124-2126`).
  This fires automatically on every qualifying edit. The user cannot opt out per
  call; it is bound by the shell `timeout` ceiling below.
- **Optional MCP tool (not the budget).** `get_pattern_context` is also registered
  as an MCP tool (`server.py:55` → `tools.get_pattern_context`). This is a
  discretionary surface Claude may call during a task. Item 15's caveat is exactly
  this: verify the **hook** invocation specifically, because that is the enforced,
  budget-bound path — the tool surface is not.

## Ceilings (cited from code, not invented)

These are the real, already-enforced caps. No percentile-latency SLO (p50/p95 in ms)
is codified on top of them today; if one is ever wanted it must be chosen
deliberately and added here, not back-derived from a benchmark run.

| Ceiling | Value | Where | Behavior on breach |
|---|---|---|---|
| Fast-hook hard timeout | **3 s** | `hooks/preflight-and-advise:62` (`timeout 3`); same for `session-start`, `posttool-verify`, `posttool-recorder`, `callout-detector` | Process killed; hook **fails open** (edit proceeds without chameleon) |
| Stop/SubagentStop backstop | **55 s** | `hooks/stop-backstop:72` (`timeout 55`); wraps the ~45 s turn-end correctness judge | Killed; fails open |
| Statusline render | **< 100 ms** | `bin/chameleon-statusline.sh:6-7` (bounded stdin read; ~12-profile cap) | Truncates/degrades to stay under budget |

Notes:
- The 3 s cap is a *hard ceiling and a safety net*, not a target. The fast hooks
  degrade to uncapped only when neither `timeout` nor `gtimeout` is on PATH
  (Windows / minimal environments); in-process code still self-limits
  (git 2 s, sqlite `busy_timeout`).
- Fail-open is the invariant: a slow or broken hot path never blocks the edit and
  never corrupts the session. That is what makes "budget, not tool" safe.

## Measurement method

`tests/bench_hot_path.py` reports cold / warm **p50 and p99** for
`get_pattern_context` and its sub-steps (`find_repo_root`, `_compute_repo_id`,
`_effective_profile_dir`, `load_profile_dir`, archetype resolve). It auto-discovers
a profiled repo from the local bed and a no-profile baseline (the chameleon repo
itself). It **measures only — it enforces no ceiling**; the ceilings above are the
contract, the benchmark is the instrument.

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
