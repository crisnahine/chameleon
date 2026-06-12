require_relative "../app/lib/money_formatter"
require_relative "../app/lib/email_normalizer"
require_relative "../app/lib/refund_calculator"

failures = []

def check(failures, label, actual, expected)
  failures << "#{label}: expected #{expected.inspect}, got #{actual.inspect}" unless actual == expected
end

check(failures, "format positive", MoneyFormatter.format(123_456), "USD 1234.56")
check(failures, "format negative", MoneyFormatter.format(-5), "-USD 0.05")
check(failures, "normalize", EmailNormalizer.normalize("  Bob@Example.COM "), "bob@example.com")
check(failures, "full refund inside window", RefundCalculator.amount_cents(10_000, 10), 10_000)
check(failures, "partial refund after window", RefundCalculator.amount_cents(10_000, 60), 5_000)
check(failures, "no refund on zero total", RefundCalculator.amount_cents(0, 5), 0)

if failures.empty?
  puts "ok: 6 assertions"
else
  failures.each { |f| warn "FAIL #{f}" }
  exit 1
end
