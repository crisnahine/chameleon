class RefundCalculator
  FULL_REFUND_WINDOW_DAYS = 30
  PARTIAL_REFUND_RATE = 0.5

  def self.amount_cents(total_cents, days_since_purchase)
    return 0 if total_cents <= 0 || days_since_purchase.negative?
    return total_cents if days_since_purchase <= FULL_REFUND_WINDOW_DAYS

    (total_cents * PARTIAL_REFUND_RATE).floor
  end
end
