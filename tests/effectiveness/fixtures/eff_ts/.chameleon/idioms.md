# idioms

## active

### all-http-via-api-client
Language: typescript
Status: active (added 2026-06-12)
All HTTP calls go through apiGet/apiPost from src/api/client.ts. Never call
fetch directly outside that module — the wrappers own error mapping (ApiError)
and headers.

### money-is-integer-cents
Language: typescript
Status: active (added 2026-06-12)
Money amounts are integer cents end to end. Format for display only via
formatMoney from src/utils/format_money.ts; never use toFixed or string math
on amounts.

### class-names-via-cx
Language: typescript
Status: active (added 2026-07-27)
Compose className values with cx from src/utils/cx.ts. classnames is still in
package.json for an old page that has not been migrated; do not reach for it in
new components, and do not build className with template literals or string
concatenation.

## deprecated
