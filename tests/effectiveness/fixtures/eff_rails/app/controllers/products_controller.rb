class ProductsController < ApplicationController
  def index
    products = Product.all
    render json: products.map { |p|
      { id: p.id, name: p.name, price: MoneyFormatter.format(p.price_cents) }
    }
  end
end
