from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.schemas.cart import CartItemIn, CartOut
from app.services.cart_service import CartService, get_cart_service

router = APIRouter(prefix="/carts", tags=["carts"])


@router.post("/{user_id}/items", response_model=CartOut, status_code=201)
def add_item(
    user_id: int,
    payload: CartItemIn,
    service: Annotated[CartService, Depends(get_cart_service)],
) -> dict:
    if not service.add_item(user_id, payload.product_id, payload.quantity):
        raise HTTPException(status_code=404, detail="product not found")
    return service.get_cart(user_id)


@router.get("/{user_id}", response_model=CartOut)
def get_cart(
    user_id: int,
    service: Annotated[CartService, Depends(get_cart_service)],
) -> dict:
    return service.get_cart(user_id)
