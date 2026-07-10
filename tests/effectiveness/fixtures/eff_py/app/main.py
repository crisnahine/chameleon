from fastapi import FastAPI

from app.routers.carts import router as carts_router
from app.routers.orders import router as orders_router
from app.routers.products import router as products_router
from app.routers.users import router as users_router

app = FastAPI(title="eff-py-fixture")
app.include_router(products_router)
app.include_router(orders_router)
app.include_router(users_router)
app.include_router(carts_router)
