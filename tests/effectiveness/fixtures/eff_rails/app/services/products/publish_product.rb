module Products
  class PublishProduct < ApplicationService
    def initialize(product:)
      @product = product
    end

    def call
      @product.update!(published_at: Time.current)
      Result.success(product: @product, price_display: MoneyFormatter.format(@product.price_cents))
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
