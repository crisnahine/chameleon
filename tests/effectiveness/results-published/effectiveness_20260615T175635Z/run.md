# Effectiveness run effectiveness_20260615T175635Z

tier: dup | arms: off, shadow | model: sonnet | toggle: none
cells: 92 ok: 87 errors: 5 skipped: 0 | total cost: $59.92

## Aggregates

| category | arm | cells | conv viol | broken exp | stale callers | verify rate | dup rate | $ mean | wall s |
|---|---|---|---|---|---|---|---|---|---|
| duplication | off | 44 | 0.3182 | - | - | 0.1364 | 0.8409 | 0.5598 | 195.64 |
| duplication | shadow | 43 | 0.3023 | - | - | 0.3256 | 0.7209 | 0.6406 | 235.61 |

## Judge panel

| task | pair | winner | valid votes | $ |
|---|---|---|---|---|
| t3-ts-dup-get-full-name | off vs shadow | shadow | 3 | 0.451861 |
| t3-ts-dup-titleize | off vs shadow | off | 3 | 0.458006 |
| t3-ts-dup-is-blank | off vs shadow | shadow | 3 | 0.496966 |
| t3-ts-dup-suffixed-number | off vs shadow | shadow | 3 | 0.496066 |
| t3-ts-dup-use-previous | off vs shadow | shadow | 3 | 0.319131 |
| t3-rb-dup-rate-limit | off vs shadow | shadow | 3 | 0.47899 |

### Causal preference (paired cluster-bootstrap 95% CI)

Preference for the treatment arm over control; resampled by TASK. A causal win requires the CI lower bound > 0.5.

| control | treatment | preference | 95% CI | n_tasks | verdict |
|---|---|---|---|---|---|
| off | shadow | 0.833 | [0.500, 1.000] | 6 | not established |

_No baseline entries for this tier yet (baselines.json is empty
until the first release-time update)._

## Errors and skips (excluded from aggregates, never dropped)

- t3-rb-dup-calculate-net-profit | shadow | repeat 1 | error: session returncode -1
- t3-rb-dup-validate-uuid | off | repeat 1 | error: session returncode 1
- t3-rb-dup-validate-uuid | shadow | repeat 1 | error: session returncode 1
- t3-rb-dup-pagination-calculate | off | repeat 1 | error: session returncode 1
- t3-rb-dup-filters-service | shadow | repeat 1 | error: session returncode 1
