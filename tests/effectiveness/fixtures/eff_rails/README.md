# eff-rails-fixture

Small commerce backend used by the chameleon effectiveness eval.

Run the tests with: `ruby -Itest tests/run_tests.rb` (plain Ruby, no bundle
install needed).

Conventions: service objects are module-wrapped classes in
app/services/<domain>/ with #initialize + #call; pure helpers live in
app/lib/; money display goes through MoneyFormatter.format only.
