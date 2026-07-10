# Judge golden set

Hand-labeled preference judgments used to calibrate the effectiveness eval's
LLM judge panel. `stats.py` gates citability on Cohen's kappa >= 0.6 between
the panel and a human-labeled golden set; until `labels.jsonl` exists and the
gate passes, every panel preference number is uncitable by the project's own
standard.

## Files

- `pairs.jsonl` - generated. One blinded pair per line: `pair_id`, `task_id`,
  `side_a`, `side_b` (the two arms' final diffs, side order randomized per
  pair, capped at 20k chars like the panel prompt). Nothing in it says which
  side is which arm or what the panel voted.
- `panel_verdicts.jsonl` - generated sidecar. The panel's verdict per pair,
  mapped to the blinded A/B. The labeler must not open this file.
- `labels.jsonl.example` - generated template: one `{"pair_id": ..., "winner": ""}`
  line per pair.
- `labels.jsonl` - written BY A HUMAN, never by this tooling.

## Labeling protocol

1. Copy `labels.jsonl.example` to `labels.jsonl`.
2. For each line in `pairs.jsonl`, read both diffs and decide which change a
   senior reviewer would flag less: convention fit, correctness risk, reuse of
   existing helpers, test discipline (the same question the panel was asked).
3. Fill `winner` with `"A"`, `"B"`, or `"tie"`. Tie is a real option; don't
   force a pick.
4. Label blind: do not open `panel_verdicts.jsonl` (or the run dirs' panel
   transcripts) until `labels.jsonl` is complete.

Hard rule: labels must come from a human. If a model fills `labels.jsonl`,
kappa measures judge-vs-judge agreement instead of judge-vs-human, which voids
the gate and every number downstream of it. The tooling only ever writes the
`.example` template.

## Commands

Generate the labeling sheet (re-running with the same runs/n/seed is
byte-identical; it refuses to overwrite an existing `labels.jsonl` sheet's
pairing without `--force`):

```bash
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.golden_label sample \
  --runs tests/effectiveness/results/effectiveness_20260615T175635Z \
         tests/effectiveness/results/effectiveness_20260616T003421Z \
  --n 40 --seed 7 --out tests/effectiveness/golden/pairs.jsonl
```

Compute the gate once labels exist (read-only; partial labels are fine, it
reports coverage and computes on the labeled subset):

```bash
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.golden_label kappa \
  --pairs tests/effectiveness/golden/pairs.jsonl \
  --labels tests/effectiveness/golden/labels.jsonl \
  --panel tests/effectiveness/golden/panel_verdicts.jsonl
```

Output: `kappa=X.XXX n=NN citable=yes/no (gate 0.6, per stats.py)`.

Note: the sampler can only draw from pairs the panel actually judged (the
panel runs on `--panel` or on deterministic-scorer disagreement), so a sample
may come back smaller than `--n`. The kappa line prints the real n; more
panel-judged runs grow the pool.
