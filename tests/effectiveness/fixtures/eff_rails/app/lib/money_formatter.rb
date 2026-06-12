class MoneyFormatter
  def self.format(cents, currency = "USD")
    sign = cents.negative? ? "-" : ""
    abs = cents.abs
    "#{sign}#{currency} #{abs / 100}.#{(abs % 100).to_s.rjust(2, '0')}"
  end
end
