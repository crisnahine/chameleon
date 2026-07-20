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

## Confirm the spend first

A full run bills real API usage and takes about an hour, so it is not something to start on the
user's behalf from a bare `/chameleon-journey`. Before the first Claude-spawning command:

1. Run `--dry-run` (free) to confirm the preflight passes, and `--list` if the user wants to see
   what would execute.
2. State the projected cost, the runtime, and the budget cap that will apply.
3. **Ask the user to confirm, and wait for an affirmative answer.** Typing the slash command is a
   request to use the harness, not standing approval to spend ~$38.

Skip the confirmation only when the user's own message already authorized the spend (an explicit
"yes, run the full journey" or a `--max-budget-usd` they chose themselves). A run gated to zero
spend (`--dry-run`, `--list`) needs no confirmation.

## Run

Only after the user has confirmed. From the chameleon repo (clone) root:

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
