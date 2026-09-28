from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.api import health
from app.store import db


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="SAFE-ORCH", lifespan=lifespan)

api = APIRouter(prefix="/api")
api.include_router(health.router)
app.include_router(api)
