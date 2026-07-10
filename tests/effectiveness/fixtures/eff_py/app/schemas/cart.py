from pydantic import BaseModel


class CartItemIn(BaseModel):
    product_id: int
    quantity: int


class CartOut(BaseModel):
    user_id: int
    item_count: int
    subtotal_cents: int
    display_subtotal: str
