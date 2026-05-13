# tests/dogfood

End-to-end test harness for chameleon. Runs the catalog at
`docs/superpowers/specs/2026-05-13-comprehensive-dogfood.md` against
real test repos pointed to via env vars.

## Run

By default, runs only `free` + `cheap` scenarios (no real Claude calls):

```bash
cd /Users/crisn/Documents/Projects/chameleon
python -m tests.dogfood.runner
```

Run a single phase / family:

```bash
python -m tests.dogfood.runner --phase 1.1
python -m tests.dogfood.runner --family trust
python -m tests.dogfood.runner --phase 1.x,2.x
```

Include real-Claude scenarios (~$0.20 each, see catalog for cost):

```bash
python -m tests.dogfood.runner --include-real-claude
```

List what would run without running:

```bash
python -m tests.dogfood.runner --list
```

## Output

- Stderr: per-scenario PASS/FAIL line + summary table
- Stdout: nothing (so you can pipe `2>&1 | tee`)
- Files: `tests/dogfood/results/dogfood_<timestamp>.{md,json}`

## Adding a scenario

Create or edit `tests/dogfood/scenarios/<family>.py` and append a `Scenario(...)` entry to its `SCENARIOS` list.

## Prerequisites

Set in `.env`:

```
CHAMELEON_TEST_TS_REPO=/abs/path/to/typescript/repo
CHAMELEON_TEST_RUBY_REPO=/abs/path/to/rails/repo
```

Without these, scenarios that need real repos skip gracefully.
