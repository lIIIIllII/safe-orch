"""POST /dev/reset (설계서 §12, 부록 A.3·A.18). DEMO_MODE 전용.

파일을 지우지 않고 한 write 트랜잭션 안에서 스키마를 다시 만들고 seed한다(db.rebuild_schema).
그 전에 워커가 처리 중인 job을 끝내길 기다리고(최대 RESET_WORKER_WAIT_S), 못 끝내면 409
WORKER_BUSY로 아무것도 바꾸지 않는다. 끝나면 새 워커를 시작한다. ?pack=은 현재 Pack만 받는다.
CommandResult는 남기지 않는다(방금 지웠다). 새 SEED audit 행이 기록이다.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import ActorDep, ApiError, PackDep
from app.commands.service import Body
from app.config import get_settings
from app.coordinator.dispatcher import DispatchWorker
from app.store import db
from app.store.repos.seed import seed_pack

router = APIRouter()

CONFIRM_PHRASE = "RESET safe_orch"
RESET_WORKER_WAIT_S = 30.0


class ResetBody(Body):
    confirm: str
    start: Literal["R0"] = "R0"  # 중간 시작점 (2)·(3)은 D6


@router.post("/dev/reset")
def post_reset(
    request: Request,
    body: ResetBody,
    pack: PackDep,
    actor: ActorDep,
    pack_name: Annotated[str | None, Query(alias="pack")] = None,
) -> JSONResponse:
    settings = get_settings()
    if not settings.demo_mode:
        raise ApiError(404, "NOT_FOUND")
    if body.confirm != CONFIRM_PHRASE:
        raise ApiError(400, "CONFIRM_REQUIRED")
    if pack_name is not None and pack_name != pack.name:
        raise ApiError(400, "PACK_NOT_SUPPORTED")

    worker: DispatchWorker | None = request.app.state.worker
    quiesced = False
    if worker is not None and worker.alive:
        quiesced = worker.quiesce(RESET_WORKER_WAIT_S)
        if not quiesced:
            raise ApiError(409, "WORKER_BUSY")
    try:
        with db.write() as tx:
            db.rebuild_schema(tx)
            seed_pack(tx, pack)
    finally:
        if quiesced:
            assert worker is not None
            worker.release_and_join()
    if worker is not None:
        new = DispatchWorker(pack, worker.poll_s, worker.model_factory)
        new.start()
        request.app.state.worker = new
    return JSONResponse(
        {
            "status": "APPLIED",
            "reason_codes": [],
            "context_version": 0,
            "plan_revision": 0,
            "result_refs": {
                "pack": pack.name,
                "pack_hash": pack.pack_hash,
                "site_id": pack.site_id,
            },
        }
    )
