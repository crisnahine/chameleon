from pydantic import BaseModel


class ProductOut(BaseModel):
    id: int
    name: str
    description: str
    price_cents: int
    display_price: str
