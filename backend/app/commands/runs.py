"""Run 취소 (설계서 §12 /runs/{rid}/cancel, 부록 A.18).

SUPERVISOR만. RUNNING·WAITING_HUMAN·ERROR Run을 CANCELLED로 바꾸고 RESERVED step·SolverJob을
ABORTED로 둔다. 실행 중인 그래프는 다음 RUNNING 확인에서 멈춘다. 취소 뒤 RECHECK는 자동 등록하지
않는다(사람이 멈춘 것이므로 다음 재계획은 새 사실에서 시작한다). 보낸 요청의 취소는 D5.
"""

import sqlite3

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.packs.loader import LoadedPack
from app.store.repos.runs import abort_reserved, end_run, get_run

CANCELLABLE = ("RUNNING", "WAITING_HUMAN", "ERROR")


class CancelRun(Body):
    run_id: str


def _cancel(tx: sqlite3.Connection, ctx: CommandContext, body: CancelRun) -> Result:
    r = Result()
    run = get_run(tx, body.run_id)
    if run is None:
        r.reject("RUN_NOT_FOUND")
        return r
    if not ctx.has_role("SUPERVISOR"):
        r.reject("NOT_AUTHORIZED")
        return r
    if run.status not in CANCELLABLE:
        r.reject("RUN_NOT_ACTIVE")
        return r
    abort_reserved(tx, run.run_id, "CANCELLED")
    end_run(tx, run.run_id, "CANCELLED", f"CANCELLED_BY:{ctx.actor_id}", CANCELLABLE)
    r.refs = {"run_id": run.run_id, "previous_status": run.status}
    return r


def cancel_run(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: CancelRun
) -> CommandOutcome:
    return run_command(pack, "CANCEL_RUN", actor_id, idempotency_key, body, _cancel)
