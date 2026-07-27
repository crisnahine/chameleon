# Real-world effectiveness study: D1 + D2, diff-scoped re-run (2026-07-27)

Pre-registered in `docs/effectiveness-study.md`. That registration commits to
publishing "verbatim ... whatever they say". This run does not support the
study's hypotheses, and two of the three powered readings run against them.

## Why this is a re-run

The earlier D1/D2 code linted each changed file's whole blob and counted every
row. That measures the file's accumulated violation load, not the commit's
contribution -- a file carrying 40 pre-existing violations scored 40 every time
anyone touched it. The registration asks for "violations introduced per 100
changed source files", which is a different quantity.

Both scripts now lint each file twice (at the commit and at its baseline) and
subtract by multiset match, so only net-new rows count; rows the per-edit path
suppresses before the model reads them are excluded. See `tests/study_scope.py`.

The correction is large. Pre-existing load was **5.2x** the introduced count on
ef-client (235 carried vs 56 introduced) and **5.7x** on ef-api (2,562 vs 540).
Roughly 84% of what the old numbers counted was violations the commit inherited.

## D1 -- interrupted time series (adoption 2026-06-01)

| repo | pre | post | diff | 95% CI | n pre/post | verdict |
|---|---|---|---|---|---|---|
| ef-client (TS) | 5.15 | 11.43 | -6.27 | [-12.38, -0.49] | 71 / 52 | **REVERSED** |
| ef-api (Rails) | 21.27 | 19.66 | +1.60 | [-5.42, 8.66] | 171 / 173 | NULL |

Rates are introduced violations per 100 changed source files; unit of
resampling is the commit. H1 predicted a lower post-adoption rate. ef-client
moved the other way with a CI excluding zero; ef-api moved the predicted
direction but its CI straddles zero.

## D2 -- governed vs ungoverned files, same window

Positive diff = governed files carry fewer introduced violations.

| repo | governed | ungoverned | diff | 95% CI | n gov/ungov | verdict |
|---|---|---|---|---|---|---|
| ef-client (TS) | 0.444 | 0.120 | -0.325 | [-1.024, 0.141] | 9 / 242 | underpowered |
| ef-api (Rails) | 0.439 | 0.180 | -0.259 | [-0.513, -0.043] | 171 / 891 | **REVERSED** |

On ef-api, the only adequately powered D2 arm, chameleon-governed files
introduced about 2.4x the violations per file of ungoverned ones, with the
whole CI below zero.

## Verdict against the fixed bar

The registration grants "chameleon measurably improves output in real-world
use" only when the time-series and contemporaneous arms are concordantly
supported. **Not supported.** No arm is supported in the predicted direction;
two are reversed with CIs excluding zero.

## What was checked before reporting the reversal

Two mechanisms could have manufactured the D2 direction. Neither did.

**New-file asymmetry.** A file absent at the window base has an empty baseline,
so all its violations count as introduced. If governed files were
disproportionately new, subtraction would favor the ungoverned arm. Measured on
ef-api: governed 35.1% new (61/174), ungoverned 55.3% (502/908). The asymmetry
runs the *other* way, so it works against the observed result rather than
producing it.

**Arm composition.** Both arms are dominated by the same directory (`services`:
123/174 governed, 561/908 ungoverned) and have similar zero-violation shares
(85% governed, 88% ungoverned). The gap lives in the tail, not in one arm being
made of trivial files.

## What this does and does not license

It does not license "chameleon makes code worse". D2's registration already
declares SELECTION BIAS as an uncontrolled limitation: governed files are the
ones a developer chose to do substantive AI-assisted work on, not a random
sample. Substantive work plausibly introduces more violations than the routine
edits filling the ungoverned arm, and nothing here separates those.

It does license two conclusions. The published effectiveness claim is not
currently supported by this repo's own real-world instrument, and any prior
number derived from the whole-blob count was measuring accumulated load rather
than introduced violations. A design that controls selection -- matching on
change size, or randomizing governance per file -- is what would make the D2
question answerable; the current design cannot answer it in either direction.

## Reproduce

```
CHAMELEON_TEST_TS_REPO=<ef-client> CHAMELEON_TEST_RUBY_REPO=<ef-api> \
  PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_retrospective.py
CHAMELEON_TEST_TS_REPO=<ef-client> CHAMELEON_TEST_RUBY_REPO=<ef-api> \
  PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_d2.py
```
