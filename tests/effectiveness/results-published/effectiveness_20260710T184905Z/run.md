# Effectiveness run effectiveness_20260710T184905Z

tier: dup | arms: off, shadow | model: sonnet | toggle: none
cells: 92 ok: 61 errors: 31 skipped: 0 | total cost: $157.63

## Aggregates

| category | arm | cells | conv viol | broken exp | stale callers | verify rate | dup rate | $ mean | wall s |
|---|---|---|---|---|---|---|---|---|---|
| duplication | off | 32 | 0.5 | - | - | 0.875 | 0.875 | 1.3214 | 213.3919 |
| duplication | shadow | 29 | 0.4138 | - | - | 0.7931 | 0.8621 | 1.3272 | 228.2176 |

## Per-arm turn overhead (advisory, never blocking)

turns_mean charges the arm's real turn overhead over ok cells; error_max_turns counts cells that died at the turn cap (a truncated cell measures nothing, so the count is the signal).

| arm | ok cells | turns_mean | error_max_turns | $ mean | wall s mean |
|---|---|---|---|---|---|
| off | 32 | 31.625 | 12 | 1.3214 | 213.3919 |
| shadow | 29 | 31.5517 | 17 | 1.3272 | 228.2176 |

## Judge panel

| task | pair | winner | valid votes | $ |
|---|---|---|---|---|
| t3-ts-dup-get-item-with-expiry | off vs shadow | shadow | 2 | 0.728804 |
| t3-ts-dup-is-email-valid | off vs shadow | shadow | 2 | 0.520039 |
| t3-ts-dup-get-minimum-commission | off vs shadow | shadow | 2 | 0.666345 |
| t3-ts-dup-get-full-name | off vs shadow | unscored | 0 | 0.0 |
| t3-ts-dup-titleize | off vs shadow | off | 3 | 0.565818 |
| t3-ts-dup-is-blank | off vs shadow | off | 3 | 0.392551 |
| t3-ts-dup-suffixed-number | off vs shadow | off | 3 | 0.349388 |
| t3-ts-dup-process-humps | off vs shadow | off | 3 | 0.576314 |
| t3-ts-dup-has-filters-raw | off vs shadow | unscored | 0 | 0.0 |
| t3-ts-dup-use-previous | off vs shadow | off | 2 | 0.507963 |
| t3-ts-dup-use-debounced-value | off vs shadow | unscored | 0 | 0.0 |
| t3-ts-dup-use-window-scroll | off vs shadow | unscored | 0 | 0.0 |
| t3-ts-dup-use-document-title | off vs shadow | off | 3 | 0.566551 |
| t3-ts-dup-use-background-color | off vs shadow | off | 3 | 0.578903 |
| t3-rb-dup-format-name | off vs shadow | off | 2 | 0.814452 |
| t3-rb-dup-normalize-domain | off vs shadow | shadow | 3 | 0.626133 |
| t3-rb-dup-multi-select-sanitizer | off vs shadow | off | 2 | 0.529912 |
| t3-rb-dup-extract-reply | off vs shadow | shadow | 3 | 1.009736 |
| t3-rb-dup-calculate-profit-margin | off vs shadow | unscored | 0 | 0.0 |
| t3-rb-dup-clean-url | off vs shadow | off | 3 | 0.671853 |
| t3-rb-dup-formatted-address | off vs shadow | unscored | 0 | 0.0 |
| t3-rb-dup-formatted-money | off vs shadow | off | 2 | 0.502565 |
| t3-rb-dup-calculate-net-profit | off vs shadow | tie | 2 | 0.804495 |
| t3-rb-dup-calculate-total-cost | off vs shadow | off | 3 | 0.564083 |
| t3-rb-dup-rate-limit | off vs shadow | tie | 2 | 0.597093 |
| t3-rb-dup-user-attributes | off vs shadow | shadow | 3 | 0.882262 |

### Causal preference (paired cluster-bootstrap 95% CI)

Preference for the treatment arm over control; resampled by TASK. A causal win requires the CI lower bound > 0.5.

| control | treatment | preference | 95% CI | n_tasks | verdict |
|---|---|---|---|---|---|
| off | shadow | 0.350 | [0.175, 0.550] | 20 | not established |

## Cost-adjusted lift (advisory, never blocking)

Nets the judged preference against the treatment arm's extra spend: lift_per_dollar = (preference - 0.5) / ($ mean treatment - $ mean control); lift_per_wall_minute divides by the wall-time delta in minutes.

| control | treatment | preference | lift_per_dollar | lift_per_wall_minute |
|---|---|---|---|---|
| off | shadow | 0.350 | -25.8621 | -0.6071 |

_No baseline entries for this tier yet (baselines.json is empty
until the first release-time update)._

## Errors and skips (excluded from aggregates, never dropped)

- t3-ts-dup-comma-integer-to-number | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-comma-integer-to-number | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-transform-money-value | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-money-string-to-number | shadow | repeat 1 | error: session returncode -1 (error_max_turns)
- t3-ts-dup-transform-money-value | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-transform-integer-value | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-validate-date | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-validate-date | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-remove-html-tags | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-remove-http-prefix | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-file-size | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-file-size | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-get-offer-status | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-ts-dup-use-query-action | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-safe-parse-datetime | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-safe-parse-datetime | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-mask-email | off | repeat 1 | error: session returncode -1
- t3-rb-dup-mask-email | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-markdown-to-html | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-pagination-calculate | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-validate-uuid | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-pagination-calculate | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-filters-service | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-obscure-string | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-obscure-string | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-filters-service | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-attachment-serializer | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-download-link | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-attachment-serializer | off | repeat 1 | error: session returncode -1
- t3-rb-dup-asset-purchase-agreement-url | off | repeat 1 | error: session returncode 1 (error_max_turns)
- t3-rb-dup-asset-purchase-agreement-url | shadow | repeat 1 | error: session returncode 1 (error_max_turns)
