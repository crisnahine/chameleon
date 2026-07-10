# Real-PR outcome aggregate — 2026-07-10

Deterministic replay of merged PRs on the two dogfood repos through the
auto-pass router (`tests/measure_pr_review_outcomes.py`,
`tests/measure_pr_tier_distribution.py`), correlated with the real
non-deleted Bitbucket review-comment count each PR drew. Zero spend, zero
agent spawns, read-only API access. Refresh this file at every release.

## Headline (pooled across both repos)

| Metric | Value |
|---|---|
| Merged PRs scored (comment-fetched) | 381 (183 api + 198 client) |
| Routed review-clean | 137 PRs (69 api + 68 client) |
| Precision: routed-clean PRs that drew 0 comments | **120/137 = 88%** (api 90%, Wilson95 LB 0.81; client 85%, Wilson95 LB 0.75) |
| Recall: easy+medium PRs routed clean | **119/123 = 97%** (api 65/68, client 54/55) |
| Human-routed PRs that drew 0 comments anyway | api 75%, client 73% (the conservative residual) |

## Per-repo, per-tier

empire-flippers/api (Rails), 183 PRs:

| tier | PRs | routed-clean | 0-comment | mean comments |
|---|--:|--:|--:|--:|
| easy | 38 | 97% | 89% | 0.1 |
| medium | 30 | 93% | 90% | 0.1 |
| hard | 43 | 9% | 93% | 0.2 |
| complex | 72 | 0% | 64% | 1.0 |

empire-flippers/client (TypeScript), 198 PRs (post-refresh, see incident below):

| tier | PRs | routed-clean | 0-comment | mean comments |
|---|--:|--:|--:|--:|
| easy | 4 | 100% | 100% | 0.0 |
| medium | 51 | 98% | 84% | 0.4 |
| hard | 28 | 50% | 82% | 0.9 |
| complex | 115 | 0% | 72% | 0.7 |

False-passes (routed clean but drew >= 1 comment): 7 on api, 10 on client;
all were easy/medium/hard PRs with 1-10 comments, none security-surfaced.
Raw rows are in the measurement output; the largest (client PR#3987, 10
comments) is a hard-tier 4-file change the router should arguably not have
cleared — candidate calibration input.

## Measurement incident worth keeping

The first client run scored 194/200 PRs "complex" with 0% routed clean.
Cause: the checkout's committed profile predated the reverse-index schema
bump (artifact schema_version 1, engine expects 2), so `load_reverse_index`
correctly refused it and every TS file read "blast radius unknown" ->
complex. The plugin surfaced this as designed (`doctor` ->
`profile_artifacts: unreadable by this engine (stale schema)`;
`query_symbol_importers` -> `reason: index-unavailable`); `/chameleon-refresh`
repaired it and the numbers above are from the refreshed profile. Lesson for
consumers of these measurements: run `doctor` before trusting a replay.

## Caveats (unchanged from the measurement scripts, verbatim in spirit)

- Denominator = comment-fetched PRs only (fetch-failed and >100-comment PRs
  excluded; this window had zero fetch failures).
- Comment count = non-deleted Bitbucket PR comments. It undercounts review
  activity that leaves no comment (approve/request-changes states, Slack
  review threads).
- LOOK-AHEAD: the profile is derived from a ref AFTER these PRs merged, so
  precision is an upper bound until a temporal holdout (profile pinned
  before the PR window) is run.
- The router's verdict is advisory tiering, not an enforcement record: these
  PRs were all human-reviewed in reality.

## Reproduction

```bash
CHAMELEON_TEST_TS_REPO=/abs/path/ef-client CHAMELEON_TEST_RUBY_REPO=/abs/path/ef-api \
  PYTHONPATH=. mcp/.venv/bin/python tests/measure_pr_review_outcomes.py 200
CHAMELEON_TEST_TS_REPO=/abs/path/ef-client CHAMELEON_TEST_RUBY_REPO=/abs/path/ef-api \
  PYTHONPATH=. mcp/.venv/bin/python tests/measure_pr_tier_distribution.py 200
```

Requires `BITBUCKET_USER`/`BITBUCKET_TOKEN` (read scope) and committed,
current-schema `.chameleon/` profiles in both repos.
