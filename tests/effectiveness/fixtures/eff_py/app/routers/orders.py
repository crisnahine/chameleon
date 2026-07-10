from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.schemas.order import OrderIn, OrderOut
from app.services.order_service import OrderService, get_order_service

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", response_model=OrderOut, status_code=201)
def place_order(
    payload: OrderIn,
    service: Annotated[OrderService, Depends(get_order_service)],
) -> dict:
    order = service.place_order(payload.product_id, payload.quantity)
    if order is None:
        raise HTTPException(status_code=404, detail="product not found")
    return order


@router.get("/{order_id}", response_model=OrderOut)
def get_order(
    order_id: int,
    service: Annotated[OrderService, Depends(get_order_service)],
) -> dict:
    order = service.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order
