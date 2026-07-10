from __future__ import annotations

from app.utils.money import format_money

_PRODUCTS = {
    1: {
        "id": 1,
        "name": "Walnut Desk",
        "description": "Solid walnut standing desk with a cable tray.",
        "price_cents": 79_900,
    },
    2: {
        "id": 2,
        "name": "Task Chair",
        "description": "Ergonomic mesh task chair with lumbar support.",
        "price_cents": 24_500,
    },
    3: {
        "id": 3,
        "name": "Monitor Arm",
        "description": "Gas-spring dual monitor arm, desk clamp mount.",
        "price_cents": 12_900,
    },
}


class ProductService:
    def list_products(self) -> list[dict]:
        return [self._present(product) for product in _PRODUCTS.values()]

    def get_product(self, product_id: int) -> dict | None:
        product = _PRODUCTS.get(product_id)
        return self._present(product) if product else None

    def _present(self, product: dict) -> dict:
        return {**product, "display_price": format_money(product["price_cents"])}


_service = ProductService()


def get_product_service() -> ProductService:
    return _service
