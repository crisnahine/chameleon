# Effectiveness run effectiveness_20260803T034125Z

tier: ci | arms: off, shadow | model: claude-sonnet-5 | toggle: none
cells: 24 ok: 23 errors: 1 skipped: 0 | total cost: $19.87

## Aggregates

| category | arm | cells | conv viol | broken exp | stale callers | verify rate | dup rate | $ mean | wall s |
|---|---|---|---|---|---|---|---|---|---|
| convention | off | 3 | 0.0 | - | - | 1.0 | - | 1.1153 | 112.0 |
| convention | shadow | 2 | 0.0 | - | - | 1.0 | - | 0.9023 | 105.585 |
| crossfile | off | 3 | 0.6667 | 0.0 | 0.0 | 1.0 | - | 0.8893 | 71.7067 |
| crossfile | shadow | 3 | 0.6667 | 0.0 | 0.0 | 1.0 | - | 0.8216 | 76.5067 |
| duplication | off | 3 | 0.0 | - | - | 0.3333 | 0.0 | 0.7152 | 54.6133 |
| duplication | shadow | 3 | 0.0 | - | - | 0.6667 | 0.0 | 0.7446 | 74.5733 |
| verification | off | 3 | 0.0 | - | - | 0.6667 | - | 0.6075 | 37.3833 |
| verification | shadow | 3 | 0.0 | - | - | 0.6667 | - | 0.6566 | 67.6067 |

## Per-arm turn overhead (advisory, never blocking)

turns_mean charges the arm's real turn overhead over ok cells; error_max_turns counts cells that died at the turn cap (a truncated cell measures nothing, so the count is the signal).

| arm | ok cells | turns_mean | error_max_turns | $ mean | wall s mean |
|---|---|---|---|---|---|
| off | 12 | 19.4167 | 0 | 0.8318 | 68.9258 |
| shadow | 11 | 16.6364 | 1 | 0.7703 | 78.8391 |

## Cost-adjusted lift (advisory, never blocking)

Nets the judged preference against the treatment arm's extra spend: lift_per_dollar = (preference - 0.5) / ($ mean treatment - $ mean control); lift_per_wall_minute divides by the wall-time delta in minutes.

n/a (no judged preference)

_No baseline entries for this tier yet (baselines.json is empty
until the first release-time update)._

## Errors and skips (excluded from aggregates, never dropped)

- t1-rails-convention-service | shadow | repeat 1 | error: session ended abnormally: exit code 1 (error_max_turns)
