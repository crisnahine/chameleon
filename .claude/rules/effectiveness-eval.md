---
paths:
  - "tests/effectiveness/**"
---

# Effectiveness eval (A/B: does chameleon improve agent output?)

Spawns real `claude -p` sessions — local only, never CI. Tier ci (~$3-5) runs
8 tasks on committed fixtures; tier full (~$25-45) needs the
`CHAMELEON_TEST_*_REPO` env vars and asks before spending.

```bash
# List tasks / preflight without spawning
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner --list
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner --dry-run

# Tier-ci A/B (off vs shadow), budget-capped
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner \
  --tier ci --arms off,shadow --max-budget-usd 8

# Feature-level toggle experiment (paired arm from shadow)
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner \
  --tier ci --arms off,shadow --toggle judge_crossfile_facts

# Unit tests for the eval itself (these DO run in CI)
PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/effectiveness/tests/ -v
```

Results land in `tests/effectiveness/results/effectiveness_<ts>/` (gitignored):
`run.json`, `run.md` (scoreboard + baseline deltas + 20% regression banner),
`transcripts/`, `diffs/`, `worktrees/`. `baselines.json` is committed and
updated manually at release time only. See `tests/effectiveness/README.md`.

