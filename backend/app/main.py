from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.api import health
from app.config import get_settings
from app.packs.loader import load_pack, pack_dir
from app.store import db
from app.store.repos.site import ensure_pack_matches


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    pack = load_pack(pack_dir(get_settings().pack))
    with db.read() as conn:
        ensure_pack_matches(conn, pack)  # 다르면 기동 거절, 자동 reset 없음 (부록 A.4)
    app.state.pack = pack
    yield


app = FastAPI(title="SAFE-ORCH", lifespan=lifespan)

api = APIRouter(prefix="/api")
api.include_router(health.router)
app.include_router(api)
