module Orders
  class CreateOrder
    def initialize(user:, product:, quantity:)
      @user = user
      @product = product
      @quantity = quantity
    end

    def call
      total_cents = @product.price_cents * @quantity
      order = Order.create!(user: @user, product: @product, total_cents: total_cents)
      Result.success(order: order, display_total: MoneyFormatter.format(total_cents))
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
