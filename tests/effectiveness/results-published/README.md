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

## Spawn environment: read this before comparing two runs

The harness that spawns every eval cell changed what a worker loads, and
nothing in the published numbers says which side of that change a run sat on.

`spawn_claude` now passes `--setting-sources project,local`, so a worker loads
no user-scope settings and no user-installed plugins. Chameleon itself is
unaffected: it loads through `--plugin-dir` and every one of its hooks still
fires. Before the change a worker also inherited whatever the operator had
installed at user scope, third-party hooks included, and one of those hooks
answering `permissionDecision: "defer"` on a `PreToolUse` is enough to end a
session with its `Edit` handed back un-executed while it still exits 0. Two
runs on opposite sides of that line are not the same experiment, however
identical their invocations look, and the numbers alone will not tell you.

Every run published in this file ran with user scope loaded, as did the
`baselines.json` seeded on 2026-06-12. Nothing here is on the new side of the
line, so no date test is needed to read what is already below.

For a run published later, tell the two sides apart by date. Find when the
change landed:

    git log -S'--setting-sources' -- tests/journey/harness/claude.py

A run whose `run_id` timestamp (`effectiveness_<YYYYMMDD>T<HHMMSS>Z`) predates
that commit ran with user scope loaded. The command answers only once the change
is committed; while it still sits in the working tree it prints nothing, which
means the same thing by another route - no run can be on the new side of a line
the history does not carry yet.

`baselines.json` names no environment either. Each (tier, category, arm) entry
carries a `run_id` and the file carries an `updated` date; that `run_id` is the
only handle, so resolve a baseline through it and apply the same date test.
Until the schema grows an explicit field, a re-seed under policy 2 is what
retires the ambiguity: seeding from a fresh run replaces every pre-change
`run_id` at once, which is the point of re-seeding rather than letting entries
age individually. Do not mix a pre-change baseline with post-change numbers.

## Runs

### effectiveness_20260803T034125Z (ci tier, 4.10.0 release re-seed)

12 tasks x (off, shadow) on claude-sonnet-5, 24 cells, 23 ok and 1 error, $19.87.
The error is a rails convention cell that hit its turn cap; the turn cap exits
non-zero, so the cell is dropped rather than scored, and `error_max_turns` in
`run.md` counts it.

This is the run `baselines.json` is seeded from, and it is the FIRST published
run whose workers ran without user-scope settings (`--setting-sources
project,local`, see "Spawn environment" above). Its numbers are therefore not
comparable cell-for-cell with the three runs below, which inherited whatever the
operator had installed. Every entry it seeded carries `setting_sources`; an entry
without that field predates the change.

No `--panel`, so there is no blind pairwise vote and no causal preference to
report. `metrics.json` carries an empty `causal_preference` rather than an
omitted one, so a reader can tell "not run" from "run and inconclusive".

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
Both invocations above are still the right command, but they no longer run the
same experiment: both runs predate `--setting-sources project,local`, so their
workers loaded the operator's user-scope settings and plugins and a re-run
today does not. See "Spawn environment" above. The cell-status rule also
widened afterwards, from a non-zero return code to any abnormal end state that
left no diff, so a re-run can drop cells these runs would have counted.


## Flat study artifacts

Besides per-run directories, this directory holds flat, self-describing study
publications (`dogfood-study-*.md`, `migration-ab-*.md`, `multiconv-ab-*.md`,
`pr-outcomes-*.md`, each with a `.metrics.json` sidecar where applicable) under
the same verbatim-whatever-the-verdict policy. Golden-set label provenance
lives in `../golden/LABELS_PROVENANCE.md`.
