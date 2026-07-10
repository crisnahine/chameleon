import unittest

from app.services.cart_service import CartService
from app.services.order_service import OrderService
from app.services.user_service import UserService
from app.utils.clamp import clamp
from app.utils.money import format_money
from app.utils.slugs import slugify
from app.utils.text import truncate_text


class UtilsTest(unittest.TestCase):
    def test_format_money_positive(self):
        self.assertEqual(format_money(123456), "USD 1234.56")

    def test_format_money_negative(self):
        self.assertEqual(format_money(-5), "-USD 0.05")

    def test_clamp_within_range(self):
        self.assertEqual(clamp(5, 1, 99), 5)

    def test_clamp_below_low(self):
        self.assertEqual(clamp(-5, 1, 99), 1)

    def test_clamp_above_high(self):
        self.assertEqual(clamp(500, 1, 99), 99)

    def test_slugify(self):
        self.assertEqual(slugify("Hello,  World!"), "hello-world")

    def test_truncate_text(self):
        self.assertEqual(truncate_text("abcdef", 4), "abc…")


class ServicesTest(unittest.TestCase):
    def test_place_order_totals_and_display(self):
        service = OrderService()
        order = service.place_order(2, 2)
        self.assertEqual(order["total_cents"], 49_000)
        self.assertEqual(order["display_total"], "USD 490.00")

    def test_place_order_clamps_quantity(self):
        service = OrderService()
        order = service.place_order(1, 500)
        self.assertEqual(order["quantity"], 99)

    def test_place_order_unknown_product(self):
        service = OrderService()
        self.assertIsNone(service.place_order(999, 1))

    def test_cart_subtotal(self):
        service = CartService()
        service.add_item(7, 3, 2)
        cart = service.get_cart(7)
        self.assertEqual(cart["subtotal_cents"], 25_800)
        self.assertEqual(cart["display_subtotal"], "USD 258.00")

    def test_register_user_normalizes_email(self):
        service = UserService()
        user = service.register_user("  Bob@Example.COM ", "Bob")
        self.assertEqual(user["email"], "bob@example.com")


if __name__ == "__main__":
    unittest.main()
