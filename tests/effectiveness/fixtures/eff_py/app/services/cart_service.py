from __future__ import annotations

from app.services.product_service import get_product_service
from app.utils.money import format_money


class CartService:
    def __init__(self) -> None:
        self._items: dict[int, list[dict]] = {}

    def add_item(self, user_id: int, product_id: int, quantity: int) -> bool:
        product = get_product_service().get_product(product_id)
        if product is None:
            return False
        self._items.setdefault(user_id, []).append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "price_cents": product["price_cents"],
            }
        )
        return True

    def get_cart(self, user_id: int) -> dict:
        items = self._items.get(user_id, [])
        subtotal_cents = sum(item["price_cents"] * item["quantity"] for item in items)
        return {
            "user_id": user_id,
            "item_count": sum(item["quantity"] for item in items),
            "subtotal_cents": subtotal_cents,
            "display_subtotal": format_money(subtotal_cents),
        }


_service = CartService()


def get_cart_service() -> CartService:
    return _service
