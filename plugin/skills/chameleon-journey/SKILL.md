---
name: chameleon-journey
argument-hint: "[--list | --dry-run | --max-budget-usd N]"
description: Use when the user explicitly invokes /chameleon-journey to run the comprehensive real-world journey harness against the chameleon plugin
---

# /chameleon-journey

Run the journey harness at `tests/journey/`. The harness verifies chameleon's full lifecycle by spawning real `claude -p` subprocesses against committed seed fixtures.

**Requires a development clone of the chameleon repository.** The installed plugin ships only the runtime surface (`hooks/`, `skills/`, `mcp/`, ...); it does not include `tests/`. Every command below runs from the root of the CLONE, not from the installed plugin directory.

## Defaults

Full run: 21 acts, ~$38 cost ceiling, ~65 min runtime, $40 hard budget cap (default `--max-budget-usd`).

## Run

From the chameleon repo (clone) root:

```bash
PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.journey.runner
```

Variations:

- `--list`: show acts + phase coverage, exit 0.
- `--dry-run`: run preflight only (claude on PATH, git >= 2.28, fixtures present, plugin/mcp/.venv), exit before any Claude spawn.
- `--max-budget-usd N`: pre-flight + mid-run abort if projected cost exceeds N (default 40).
- `--results-dir DIR`: override per-run output dir (default `tests/journey/results/`).

## Output

- stderr: per-act `[ACT N] ...` markers + cost + duration.
- `tests/journey/results/journey_<ts>/run.json` + `run.md`: per-act + per-phase results.
- `tests/journey/results/journey_<ts>/checkpoints/<act>.jsonl`: per-act checkpoint data for post-mortem.

## Notes

The full run requires:
- `claude` CLI on PATH with API access.
- git >= 2.28 (for `git init --initial-branch=main`).
- `plugin/mcp/.venv/bin/python` (run `cd plugin/mcp && uv sync` if missing).

The harness writes ALL state to a per-run dir; the developer's own `~/.local/share/chameleon/` is never touched.
