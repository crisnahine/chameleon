module Orders
  class CancelOrder < ApplicationService
    def initialize(order:, reason:)
      @order = order
      @reason = reason
    end

    def call
      @order.update!(cancelled_at: Time.current, cancellation_reason: @reason)
      Result.success(order: @order)
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
