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

## deprecated
