# Real-world dogfood effectiveness study — 2026-07-11

Pre-registered in `docs/effectiveness-study.md` before any analysis. Published
verbatim per the results-published policy: a null or reversed result is
published exactly like a win. Machine-readable numbers in the sibling
`dogfood-study-2026-07-11.metrics.json`.

## What was measured

Chameleon has run in daily use on two Empire Flippers production repos since
its install on **2026-06-01**: `ef-api` (Rails) and `ef-client` (TypeScript).
The pre-adoption "before" arm already exists in git history. Three arms, all
deterministic, no LLM spend, measured against `origin/production` (fetched
2026-07-10) over 2026-01 through 2026-07:

- **D1** — interrupted time series: new-violation rate (chameleon's own
  `lint_file`) on the source files each first-parent commit changed, before
  vs after adoption. Unit = commit; two-sample cluster bootstrap on the rate
  difference.
- **H2** — review-comment rate per merged PR (real Bitbucket comment counts),
  before vs after adoption. Unit = PR.
- **D2** — governed vs ungoverned files, WITHIN the post-adoption window
  (isolates the temporal confound): files chameleon actually governed (from
  session attestations) vs files it did not, same repo, same lint. Unit =
  file.

## Results

| Arm | Repo | Pre | Post | Diff (95% CI) | Verdict |
|-----|------|-----|------|---------------|---------|
| D1 viol/100 files | ef-api | 112.9 | 100.5 | +12.5 [-13.9, +37.9] | **NULL** |
| D1 viol/100 files | ef-client | 209.5 | 224.8 | -15.3 [-76.4, +57.6] | **NULL** |
| H2 comments/PR | ef-api | 0.49 | 3.36 | -2.88 [-3.84, -1.99] | REVERSED* |
| H2 comments/PR | ef-client | 0.62 | 2.48 | -1.86 [-3.32, -0.57] | REVERSED* |
| D2 viol/file (ungov−gov) | ef-api | 0.33 | 1.13 | -0.80 [-1.48, -0.27] | REVERSED† |
| D2 viol/file | ef-client | — | — | underpowered (n=3) | NO DATA |

Diff sign convention: positive = improvement (post/governed lower). Bootstrap
seed fixed (12345), 10k resamples.

## Verdict: effectiveness NOT established, and no credible harm shown either

Every arm is null or dominated by a confound the design cannot remove.

- **D1 is null on both repos.** The bootstrap CI straddles zero. Structural
  lint conformance — the one dimension `lint_file` measures — did not move at
  adoption. This is expected: chameleon's mechanism is idiom conformance and
  turn-end cross-file review, not structural lint.

- **\*H2 "reversed" is an org-process artifact, not chameleon.** Comments/PR
  stepped up 4–7× across *every* PR starting exactly at June, on both repos.
  Chameleon is used by one developer on a fraction of PRs; it cannot raise the
  comment count on PRs it never touched. The step reflects an EF review-process
  change (more reviewers / mandatory review) that coincided with the install
  month. This is the pre-registered "adoption is not exogenous" limitation
  realized in the data. H2 says nothing about chameleon.

- **†D2 "reversed" is a file-selection/size artifact, not harm.** Governed
  files carry more violations/file (1.13 vs 0.33), but they are 3× larger:
  median 196 LOC (governed) vs 66 LOC (ungoverned). The developer used
  chameleon on the big, central files (`app/models/listing.rb`,
  `app/models/user.rb`, `app/services/api/v1/*`); violation count scales with
  size, and the per-file metric does not normalize for it. This is the
  pre-registered selection-bias limitation. D2 removes the temporal confound
  but not the selection confound, so it cannot estimate a clean effect either.

## What this adds to the record

Two fully independent measurement approaches now converge on the same
conclusion: **chameleon's effect on output quality is not demonstrable with any
instrument available.**

1. Eight session-scale A/B experiments (see `docs/gap-log.md`, 2026-07-11):
   strong context-reading models infer conventions from siblings on small
   fixtures, so the on/off arms tie.
2. This real-world retrospective: the free, deterministic proxies (structural
   lint, review-comment counts) either don't move (D1), are swamped by a
   concurrent org change (H2), or are confounded by file selection (D2).

The blocker is structural, not a coding defect. What chameleon actually
changes — idiom conformance and cross-file staleness prevention on multi-turn
edits — is not captured by any free repo-wide proxy, and the one developer /
shared-fixture usage pattern gives no representative governed population.

**Functional correctness is proven** (zero-bug real-session runs across
TS/Python/Ruby/monorepo). **A positive effectiveness number is not**, and this
study does not manufacture one.

## What would move this

A design that captures the actual mechanism and a representative population:
multi-developer adoption (so the governed arm isn't one person's file
choices), a task-matched governed/ungoverned pairing (same feature area, same
size band, to kill the selection/size confound), and an idiom-conformance or
cross-file-correctness outcome (what chameleon changes) rather than structural
lint. Until then the honest claim is "correct and non-regressive," not
"measurably better."

## Reproduce

```bash
base="$HOME/Documents/Projects/Testing Apps"
CHAMELEON_TEST_TS_REPO="$base/ef-client" CHAMELEON_TEST_RUBY_REPO="$base/ef-api" \
  PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_retrospective.py > d1.json
BITBUCKET_USER=… BITBUCKET_TOKEN=… \
CHAMELEON_TEST_TS_REPO="$base/ef-client" CHAMELEON_TEST_RUBY_REPO="$base/ef-api" \
  PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_review_comments.py > h2.json
CHAMELEON_TEST_TS_REPO="$base/ef-client" CHAMELEON_TEST_RUBY_REPO="$base/ef-api" \
  PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_d2.py > d2.json
PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_analyze.py d1.json h2.json
```
