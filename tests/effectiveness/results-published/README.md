# Published effectiveness runs

Tracked, releasable artifacts from headline effectiveness runs, so that any
third party can check chameleon's A/B claims without access to the machine
that ran them. The working results directory (`../results/`) is gitignored
and holds the full bulk (transcripts, per-cell diffs, worktrees); this
directory holds only what a verification needs:

- `<run_id>/run.md`: the runner's rendered scoreboard, copied VERBATIM from
  `../results/<run_id>/run.md`, including the verdict whatever it is. An
  honest "not established" is published exactly like a win.
- `<run_id>/metrics.json`: a compact machine-readable summary extracted from
  the run's `run.json`: per-arm `cost_usd_mean` / `wall_seconds_mean` / cell
  counts, the paired cluster-bootstrap preference with its 95% CI, `n_tasks`,
  and the verdict. The CI is deterministic (fixed bootstrap seed), so
  re-running `report.paired_preference_cis` over the run's panel rows
  reproduces these numbers bit-for-bit.

Transcripts, diffs, and worktrees are deliberately NOT published: they carry
fixture-code bulk and session content that adds size without adding
verifiability.

## Publication policy

1. Every headline run (any run whose numbers appear in the README, CHANGELOG,
   release notes, or docs) gets its `run.md` + `metrics.json` published here
   verbatim, WHATEVER the verdict. No cherry-picking: a null or negative
   result is published under the same policy as a positive one.
2. `tests/effectiveness/baselines.json` is re-seeded from a fresh run at each
   release (copy the release run's aggregate values per (tier, category, arm)
   plus the run_id), never left to age.
3. The release workflow attaches every `run.md` / `metrics.json` under this
   directory to the GitHub release as `<run_id>-run.md` /
   `<run_id>-metrics.json` assets.

## Runs

### effectiveness_20260615T175635Z (dup tier, causal round 1)

46 duplication tasks x (off, shadow) on sonnet, blind 3-vote judge panel,
paired cluster-bootstrap CI resampled by task. Verdict: preference 0.833,
95% CI [0.500, 1.000], n_tasks 6: not established (lower bound not > 0.5).

Reproduce:

    PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.effectiveness.runner \
      --tier dup --arms off,shadow --panel --max-budget-usd 120

### effectiveness_20260616T003421Z (dup tier, causal round 2, replication)

Same invocation as round 1, fresh sessions. Verdict: preference 0.571,
95% CI [0.143, 0.857], n_tasks 7: not established.

Reproduce:

    PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.effectiveness.runner \
      --tier dup --arms off,shadow --panel --max-budget-usd 120

Reproduction notes: spawns real `claude -p` sessions (each run cost ~$60);
requirements are listed in `../README.md`. Judge-panel winners are LLM votes,
so a re-run reproduces the harness mechanics and the statistical machinery,
not the exact vote sequence; expect the same shape, not identical tables.


## Flat study artifacts

Besides per-run directories, this directory holds flat, self-describing study
publications (`dogfood-study-*.md`, `migration-ab-*.md`, `multiconv-ab-*.md`,
`pr-outcomes-*.md`, each with a `.metrics.json` sidecar where applicable) under
the same verbatim-whatever-the-verdict policy. Golden-set label provenance
lives in `../golden/LABELS_PROVENANCE.md`.
