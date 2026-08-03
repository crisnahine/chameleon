---
paths:
  - "tests/effectiveness/**"
---

# Effectiveness eval (A/B: does chameleon improve agent output?)

Spawns real `claude -p` sessions — local only, never CI. Tier ci (~$20, measured over 24 cells on claude-sonnet-5) runs
12 tasks on committed fixtures; tier full (~$25-45) needs the
`CHAMELEON_TEST_*_REPO` env vars and asks before spending.

```bash
# List tasks / preflight without spawning
PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.effectiveness.runner --list
PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.effectiveness.runner --dry-run

# Tier-ci A/B (off vs shadow), budget-capped
PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.effectiveness.runner \
  --tier ci --arms off,shadow --max-budget-usd 25

# Feature-level toggle experiment (paired arm from shadow)
PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.effectiveness.runner \
  --tier ci --arms off,shadow --toggle judge_crossfile_facts

# Unit tests for the eval itself (these DO run in CI)
PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/effectiveness/tests/ -v
```

Results land in `tests/effectiveness/results/effectiveness_<ts>/` (gitignored):
`run.json`, `run.md` (scoreboard + baseline deltas + 20% regression banner),
`transcripts/`, `diffs/`, `worktrees/`. `baselines.json` is committed and
updated manually at release time only. See `tests/effectiveness/README.md`.

## Cell status: what counts as a measured cell

A cell is `ok`, `error` or `skipped`, and only `ok` cells reach the numbers.
`report.aggregate` filters on `status == "ok"`, the panel only judges diffs
collected from `ok` cells, and `run.json`'s top-level `errors` counts the
`error` ones (a `skipped` cell is in neither number). A run with no `ok` cell
at all exits 1.

The `error` class is wider than a non-zero return code. A print-mode session
that stopped mid-task still exits 0 and still reports `subtype: "success"`, so
the runner asks `abnormal_termination(session, work_complete=bool(changed))`
instead of reading the return code alone. Two end states it catches:

- **A deferred tool is always fatal.** The CLI handed an `Edit` or `Write` back
  un-executed, so the worktree is not the state the session asked for and no
  diff over it means anything. This is what `--setting-sources project,local`
  exists to prevent (a user-scope plugin's `PreToolUse` hook answering
  `permissionDecision: "defer"`), and the check is deliberately not gated behind
  the end state, because the two facts arrive in different result frames
  whenever a background-task wakeup ran.
- **Any other unclean end state, `max_turns` above all, errors the cell UNLESS
  it already produced a diff.** That escape is a methodology decision, not a
  convenience: dropping every turn-capped cell that DID write would shrink the
  measured population, and turn-cap rates differ per arm, so the loss would not
  fall evenly across the arms being compared. A cell that changed files and then
  ran out of turns is scored on what it wrote.

The per-cell patch is written BEFORE that decision, so an error cell keeps
`diffs/<cell_id>.patch` and its worktree for forensics. On the full tier an
`ok` cell's worktree is pruned and an error cell's is kept, with the run-end
summary naming the repo that needs a manual prune; tier-ci clones off a
committed fixture are exempt from the prune either way, since retention is
their forensic record. A patch under `diffs/` is therefore evidence of what a
session wrote, not evidence the cell was counted. Cross-check `run.json` before
reading anything into one.

Every cell's `session` block in `run.json` records `terminal_reason` and
`stop_reason` alongside cost and return code, so a reader can tell a clean
finish from a turn cap without reopening the transcript. Every cell also spawns
with `--setting-sources project,local`: no user-scope settings or plugins are
loaded, chameleon itself still loads via `--plugin-dir` and every one of its
hooks still fires. Override with `CHAMELEON_JOURNEY_SETTING_SOURCES` (the name
says journey, the reach is both harnesses).

