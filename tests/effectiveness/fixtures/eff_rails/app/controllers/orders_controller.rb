class OrdersController < ApplicationController
  def index
    orders = Order.recent.limit(50)
    render json: orders.map { |o|
      { id: o.id, total: MoneyFormatter.format(o.total_cents) }
    }
  end

  def show
    order = Order.find(params[:id])
    render json: { id: order.id, total: MoneyFormatter.format(order.total_cents) }
  end
end
