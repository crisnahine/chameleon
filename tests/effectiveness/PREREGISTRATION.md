# Pre-registration: v3 causal effectiveness campaign

Committed BEFORE the campaign runs. The run is scored against what this file
says, not the other way around: no bar in here moves after data exists, and
the run.md publishes to `results-published/` whatever the verdict says.

## Hypotheses

- **H1 (primary, causal):** worker sessions with chameleon in shadow mode
  produce output judged pairwise-better than the no-plugin arm, with the
  cluster-bootstrap 95% CI lower bound on judge preference **> 0.5**
  (the bar coded in `stats.py` — "causal claim requires lo > 0.5").
- **H2 (comparative, the "most effective" claim):** shadow also beats the
  **static** arm (same derived knowledge delivered as a one-shot CLAUDE.md),
  isolating per-edit contextual delivery from content. Same bar as H1.
- **H3 (concordance):** deterministic scorers (convention adherence,
  duplication, cross-file integrity) do not contradict the judged direction.
- **H4 (cost honesty):** any H1/H2 win is reported net of cost — the
  lift-per-dollar and lift-per-wall-minute rows and the per-arm turns_mean /
  error_max_turns counts publish alongside the preference, and
  `error_max_turns` must be 0 across arms for the run to count as clean.

## Design

- **Arms:** `off`, `static`, `shadow` (enforce excluded: it changes task
  completion semantics, not just guidance quality).
- **Task set:** every task `tests.effectiveness.runner --list` reports at the
  campaign commit, across tiers ci + full + dup and all three languages
  (TypeScript, Ruby/Rails, Python) — >= 30 distinct tasks, cluster-bootstrap
  by task. The campaign commit SHA is recorded in the run.md.
- **Worker models:** two independent full passes, one `--model sonnet`, one
  `--model opus` (whole-run model, never mixed per-arm — a per-arm model
  would confound arm with model). H1/H2 must hold on the pooled bootstrap;
  per-model splits publish as secondary rows.
- **Repeats:** 2 per (task, arm) where budget allows, 1 minimum; the
  bootstrap clusters by task, so distinct tasks — not repeats — carry the n.
- **Judge:** the blind pairwise panel (`--panel`), order-randomized. Its
  citability is gated by Cohen's kappa >= 0.6 against the human-labeled
  golden set (`tests/effectiveness/golden/`). **Kappa state at
  pre-registration time: labels pending (human labeling sheet committed,
  13 pairs).** If kappa is absent or < 0.6 when the campaign runs, every
  preference number publishes with the explicit "judge uncalibrated —
  uncitable" caveat, regardless of how favorable it is.
- **Deterministic scorers:** unchanged from the committed scorers/ set at the
  campaign commit.

## What counts as success / failure

- **Established:** H1 and H2 lower bounds both > 0.5 on the pooled run, H3
  not contradicting, H4 clean. Only then may any "most effective" language
  cite this campaign.
- **Directional only:** H1 holds but H2 does not — the honest public claim
  becomes "per-edit delivery adds no measured value over static conventions";
  the next iteration targets injection value, not marketing.
- **Not established:** H1 lower bound <= 0.5 — published as-is, verdict
  verbatim, same as the two prior runs.

## Budget and publication

- Estimated spend: $60–150 across both model passes (tier-full is $25–45
  per pass historically, plus panel and repeats).
- Publish per pass: `run.md` + `metrics.json` to
  `results-published/<run_id>/`, release-asset attached; `baselines.json`
  re-seeded from the sonnet pass.
- Raw transcripts/diffs stay local (fixture-code bulk), reproducible from
  the committed fixtures + this design + the recorded seeds.
