# eff-ts-fixture

Small storefront UI library used by the chameleon effectiveness eval.

Run the tests with: `npm test` (requires Node >= 22.6; no install step).

Conventions: components are PascalCase .tsx files in src/components/ with a
`type XProps` above a named-export function; utilities are snake_case files in
src/utils/; services call apiGet/apiPost from src/api/client.ts, never raw
fetch; money is integer cents formatted via formatMoney.
