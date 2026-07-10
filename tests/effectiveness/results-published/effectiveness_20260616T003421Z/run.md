# Effectiveness run effectiveness_20260616T003421Z

tier: dup | arms: off, shadow | model: sonnet | toggle: none
cells: 92 ok: 84 errors: 8 skipped: 0 | total cost: $59.91

## Aggregates

| category | arm | cells | conv viol | broken exp | stale callers | verify rate | dup rate | $ mean | wall s |
|---|---|---|---|---|---|---|---|---|---|
| duplication | off | 44 | 0.2273 | - | - | 0.1818 | 0.8409 | 0.5646 | 202.5686 |
| duplication | shadow | 40 | 0.55 | - | - | 0.35 | 0.65 | 0.6566 | 248.798 |

## Judge panel

| task | pair | winner | valid votes | $ |
|---|---|---|---|---|
| t3-ts-dup-transform-money-value | off vs shadow | off | 3 | 0.315311 |
| t3-ts-dup-get-item-with-expiry | off vs shadow | shadow | 3 | 0.476168 |
| t3-ts-dup-is-blank | off vs shadow | shadow | 3 | 0.323843 |
| t3-ts-dup-remove-http-prefix | off vs shadow | off | 3 | 0.459059 |
| t3-ts-dup-process-humps | off vs shadow | shadow | 3 | 0.456197 |
| t3-ts-dup-has-filters-raw | off vs shadow | shadow | 3 | 0.321528 |
| t3-ts-dup-use-previous | off vs shadow | off | 3 | 0.181386 |

### Causal preference (paired cluster-bootstrap 95% CI)

Preference for the treatment arm over control; resampled by TASK. A causal win requires the CI lower bound > 0.5.

| control | treatment | preference | 95% CI | n_tasks | verdict |
|---|---|---|---|---|---|
| off | shadow | 0.571 | [0.143, 0.857] | 7 | not established |

_No baseline entries for this tier yet (baselines.json is empty
until the first release-time update)._

## Errors and skips (excluded from aggregates, never dropped)

- t3-ts-dup-use-document-title | shadow | repeat 1 | error: session returncode 1
- t3-rb-dup-format-name | shadow | repeat 1 | error: session returncode -1
- t3-rb-dup-calculate-net-profit | shadow | repeat 1 | error: session returncode -1
- t3-rb-dup-safe-parse-datetime | off | repeat 1 | error: session returncode 1
- t3-rb-dup-mask-email | shadow | repeat 1 | error: session returncode -1
- t3-rb-dup-markdown-to-html | off | repeat 1 | error: session returncode 1
- t3-rb-dup-validate-uuid | shadow | repeat 1 | error: session returncode 1
- t3-rb-dup-attachment-serializer | shadow | repeat 1 | error: session returncode 1
