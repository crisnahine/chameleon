---
name: chameleon-dogfood
description: Use when the user explicitly invokes /chameleon-dogfood to run the comprehensive end-to-end dogfood test harness against the chameleon plugin
---

# /chameleon-dogfood

Run the comprehensive dogfood test harness at `tests/dogfood/`. The harness verifies chameleon's full lifecycle against real test repos.

## Defaults

By default the harness runs only `free` + `cheap` scenarios -- no real Claude calls, no money spent. To include real-Claude scenarios (~$0.20 each), pass `--include-real-claude`.

## Run

From the chameleon repo root:

```bash
cd /Users/crisn/Documents/Projects/chameleon
mcp/.venv/bin/python -m tests.dogfood.runner
```

Variations:

- `--list`: list scenarios that would run, no execution.
- `--phase 1.1` or `--phase 1.x,2.x`: filter by phase id (`1.x` = all 1.x).
- `--family init,trust`: filter by family.
- `--cost free,cheap` (default) or `--cost free,cheap,moderate`: filter by cost band.
- `--include-real-claude`: enable scenarios that need a real `claude -p` call.
- `--include-expensive`: enable expensive cost band (none today).
- `--results-dir DIR`: where to write the JSON + markdown summaries.
- `--max-budget-usd N`: abort if estimated cost would exceed (default 5.0).

## Output

- Stderr: `[RUN]` per scenario start, `[PASS/FAIL/SKIP/ERROR]` per scenario end, summary table at the end.
- Files: `tests/dogfood/results/dogfood_<timestamp>.{json,md}` (gitignored).

## Adding a scenario

Edit `tests/dogfood/scenarios/<family>.py` and append to `SCENARIOS = [...]`. Each entry:

```python
Scenario(
    id="3.5",
    name="my new scenario",
    family="trust",
    needs_claude=False,
    cost="cheap",
    requires=["repo:ts"],   # or ["env:MY_VAR"], or []
    run=_run_my_scenario,
    setup=None,
    teardown=None,
)
```

See `tests/dogfood/README.md` for the full pattern.

## Catalog

Scenario catalog lives in `tests/dogfood/`.
