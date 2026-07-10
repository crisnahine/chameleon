from __future__ import annotations

from app.services.product_service import get_product_service
from app.utils.clamp import clamp
from app.utils.money import format_money

MIN_QUANTITY = 1
MAX_QUANTITY = 99


class OrderService:
    def __init__(self) -> None:
        self._orders: dict[int, dict] = {}
        self._next_id = 1

    def place_order(self, product_id: int, quantity: int) -> dict | None:
        product = get_product_service().get_product(product_id)
        if product is None:
            return None
        qty = clamp(quantity, MIN_QUANTITY, MAX_QUANTITY)
        total_cents = product["price_cents"] * qty
        order = {
            "id": self._next_id,
            "product_id": product_id,
            "quantity": qty,
            "total_cents": total_cents,
            "display_total": format_money(total_cents),
        }
        self._orders[order["id"]] = order
        self._next_id += 1
        return order

    def get_order(self, order_id: int) -> dict | None:
        return self._orders.get(order_id)


_service = OrderService()


def get_order_service() -> OrderService:
    return _service
