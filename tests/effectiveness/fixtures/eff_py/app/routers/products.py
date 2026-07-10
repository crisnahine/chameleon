from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.schemas.product import ProductOut
from app.services.product_service import ProductService, get_product_service

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=list[ProductOut])
def list_products(
    service: Annotated[ProductService, Depends(get_product_service)],
) -> list[dict]:
    return service.list_products()


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: int,
    service: Annotated[ProductService, Depends(get_product_service)],
) -> dict:
    product = service.get_product(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product
