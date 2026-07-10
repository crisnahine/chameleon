from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.schemas.user import UserIn, UserOut
from app.services.user_service import UserService, get_user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserOut, status_code=201)
def register_user(
    payload: UserIn,
    service: Annotated[UserService, Depends(get_user_service)],
) -> dict:
    return service.register_user(payload.email, payload.name)


@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    service: Annotated[UserService, Depends(get_user_service)],
) -> dict:
    user = service.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user
