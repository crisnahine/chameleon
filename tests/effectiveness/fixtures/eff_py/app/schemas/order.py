from pydantic import BaseModel


class OrderIn(BaseModel):
    product_id: int
    quantity: int


class OrderOut(BaseModel):
    id: int
    product_id: int
    quantity: int
    total_cents: int
    display_total: str
