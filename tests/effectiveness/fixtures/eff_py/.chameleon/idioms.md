# idioms

## active

### services-via-provider-functions
Language: python
Status: active (added 2026-06-12)
Domain logic lives in service classes under app/services/, one class per
module with a module-level instance exposed through a get_<name>_service()
provider. Routers receive services via Depends on the provider; never
instantiate a service class inside a router.

### money-is-integer-cents
Language: python
Status: active (added 2026-06-12)
Money amounts are integer cents end to end. Format for display only via
format_money from app/utils/money.py; never use float math or inline
f-string formatting on amounts.

## deprecated
