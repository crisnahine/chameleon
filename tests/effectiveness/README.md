# Effectiveness eval

A/B measurement of whether chameleon improves agent output. Mechanics:
per-(task, arm, repeat) git worktrees of committed fixtures, identical
prompts, deterministic scorers, advisory baselines. CI runs only
`tests/effectiveness/tests/`; everything below spawns real `claude -p`
sessions and costs money.

## Smoke run (local, ~$2-3, run before relying on a release's numbers)

One task per category, off vs shadow:

    PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner \
      --tier ci \
      --tasks t1-ts-convention-component,t1-ts-crossfile-rename,t1-rails-duplication-email,t1-ts-verification-clamp \
      --arms off,shadow --repeats 1 --max-budget-usd 5

Expected shape (check `results/effectiveness_<ts>/`):

- exit code 0; `run.json` has 8 cells (4 tasks x 2 arms), `"errors"` ideally
  0 (a timeout cell is acceptable iff listed under errors with a reason);
- every ok cell's `scores.<name>` is either a metrics dict or
  `{"unscored": "<reason>"}`, never empty, never missing;
- the verification cells: `test_cmd_in_transcript` present for BOTH arms;
  `test_run_seen` true only ever on the shadow arm (the off arm has no exec
  log by construction);
- the crossfile cell: `callers_updated + callers_stale == callers_total`
  (the total counts every recorded formatMoney call site in the worktree's
  calls_index, both the import sources and the test sites, so it is larger
  than the 4 import-source files and can differ by a site or two between
  cells);
- `run.md` renders the aggregate table, an errors section (possibly empty),
  and "No baseline entries" until baselines.json is populated;
- `diffs/*.patch` exists for every ok cell; `worktrees/` holds the final trees.

If any scorer key is absent or empty on an ok cell, that is a runner bug;
fix it before trusting any numbers.

## Full tier (ask before spending; ~$25-45)

    CHAMELEON_TEST_TS_REPO=...  CHAMELEON_TEST_RUBY_REPO=... \
    PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner \
      --tier full --arms off,shadow --max-budget-usd 45

Missing env repos skip that language's tasks with a reason (never an error).

## Toggle experiments

    PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner \
      --tier ci --arms off,shadow --toggle judge_crossfile_facts

adds the paired arm `shadow~judge_crossfile_facts=false`.

## Baselines

`baselines.json` is committed and updated MANUALLY at release time: copy the
release run's aggregate values for (tier, category, arm) plus the run_id.
The runner only reads it; a worsening beyond 20% prints an advisory
regression banner in run.md and never blocks anything.

## Requirements

claude CLI on PATH; git >= 2.28; Node >= 22.6 (the TS fixture's `npm test`
uses --experimental-strip-types); ruby 3.x (plain, no gems) for the Rails
fixture; mcp/.venv built. Tier full additionally needs the env repos
bootstrapped (`/chameleon-init`) and committed.
