from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.agents.llm import openai_model
from app.api import commands, dev, health, state
from app.api.deps import ApiError, envelope
from app.config import get_settings
from app.coordinator.dispatcher import DispatchWorker
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
    app.state.worker = None
    settings = get_settings()
    worker = (
        DispatchWorker(pack, settings.dispatch_poll_s, lambda: openai_model(settings))
        if settings.dispatch_worker
        else None
    )
    if worker is not None:
        worker.start()
    app.state.worker = worker
    try:
        yield
    finally:
        # /dev/reset이 워커를 새로 만들 수 있으므로 app.state의 현재 워커를 멈춘다
        if app.state.worker is not None:
            app.state.worker.stop()


app = FastAPI(title="SAFE-ORCH", lifespan=lifespan)


@app.exception_handler(ApiError)
def api_error(request: Request, exc: ApiError) -> JSONResponse:
    pack = getattr(request.app.state, "pack", None)
    return JSONResponse(
        envelope("REJECTED", [exc.reason], pack, exc.detail), status_code=exc.status_code
    )


@app.exception_handler(RequestValidationError)
def invalid_body(request: Request, exc: RequestValidationError) -> JSONResponse:
    """본문 검증 실패(모르는 필드·형식)도 §12 모양으로. 명령 함수는 부르지 않는다 (A.18)."""
    pack = getattr(request.app.state, "pack", None)
    detail = jsonable_encoder(exc.errors())
    return JSONResponse(envelope("REJECTED", ["INVALID_BODY"], pack, detail), status_code=422)


api = APIRouter(prefix="/api")
api.include_router(health.router)
api.include_router(state.router)
api.include_router(commands.router)
api.include_router(dev.router)
app.include_router(api)
