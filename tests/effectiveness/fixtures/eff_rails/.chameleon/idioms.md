# idioms

## active

### services-return-result-never-raise
Language: ruby
Status: active (added 2026-06-12)
Service objects live in app/services/<domain>/, are module-wrapped classes
with #initialize + #call, and return Result objects. Never let a service
raise to the controller; rescue and return Result.failure.

### money-display-via-money-formatter
Language: ruby
Status: active (added 2026-06-12)
All money display goes through MoneyFormatter.format. Never call
number_to_currency or format cents inline; amounts are integer cents.

## deprecated
