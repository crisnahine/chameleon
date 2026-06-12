module Orders
  class RefundOrder
    def initialize(order:, days_since_purchase:)
      @order = order
      @days_since_purchase = days_since_purchase
    end

    def call
      cents = RefundCalculator.amount_cents(@order.total_cents, @days_since_purchase)
      @order.update!(refunded_cents: cents)
      Result.success(refund_display: MoneyFormatter.format(cents))
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
