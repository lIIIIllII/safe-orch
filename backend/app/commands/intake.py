"""자연어 작업 요청 접수 → Work Intake Run (설계서 §18.2.4, 부록 A.26).

요청 문장은 값으로 해석하지 않는다. Intake Run이 조회·질문·확인으로 TaskSpec을 만들고, 요청자가 확인한 값만
critical field CONFIRMED가 된다(D01). 이 명령은 START_RUN(INTAKE) 등록과 Audit만 한다.
"""

import sqlite3

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.ids import new_id
from app.packs.loader import LoadedPack
from app.store.repos.dispatch import register_job

COMMAND = "SUBMIT_INTAKE"
TEXT_MAX = 1000


class IntakeRequest(Body):
    task_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=TEXT_MAX)


def _in_intake(tx: sqlite3.Connection, site_id: str, task_id: str) -> bool:
    """같은 task_id로 열린(또는 시작 대기 중인) Intake가 있는가."""
    run = tx.execute(
        "SELECT 1 FROM agent_run WHERE site_id = ? AND agent_type = 'INTAKE'"
        " AND status IN ('RUNNING', 'WAITING_HUMAN')"
        " AND json_extract(input_ref, '$.task_id') = ?",
        (site_id, task_id),
    ).fetchone()
    job = tx.execute(
        "SELECT 1 FROM dispatch_job WHERE site_id = ? AND kind = 'START_RUN'"
        " AND status IN ('PENDING', 'CLAIMED')"
        " AND json_extract(payload, '$.agent_type') = 'INTAKE'"
        " AND json_extract(payload, '$.task_id') = ?",
        (site_id, task_id),
    ).fetchone()
    return bool(run or job)


def _handle(tx: sqlite3.Connection, ctx: CommandContext, body: IntakeRequest) -> Result:
    r = Result()
    if not ctx.has_role("UNIT_PLANNER") or ctx.actor is None:
        r.reject("NOT_AUTHORIZED")
        return r
    site_id = ctx.site_id
    if tx.execute(
        "SELECT 1 FROM task WHERE site_id = ? AND task_id = ?", (site_id, body.task_id)
    ).fetchone():
        r.reject("TASK_ID_EXISTS")
    if _in_intake(tx, site_id, body.task_id):
        r.reject("TASK_ID_IN_INTAKE")
    if r.reason_codes:
        return r
    intake_id = new_id("intake")
    payload = {
        "agent_type": "INTAKE",
        "intake_id": intake_id,
        "task_id": body.task_id,
        # 요청 문장은 인용 데이터다(Observation request.quoted_text)
        "quoted_text": body.text,
        "requester_actor_id": ctx.actor.actor_id,
        "acting_actor_id": ctx.actor.actor_id,
        "acting_unit_id": ctx.actor.unit_id,
        "case_id": new_id("case"),
    }
    register_job(tx, site_id, "START_RUN", f"START_RUN:INTAKE:{intake_id}", payload)
    r.refs = {"intake_id": intake_id, "task_id": body.task_id}
    return r


def submit_intake(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: IntakeRequest
) -> CommandOutcome:
    return run_command(pack, COMMAND, actor_id, idempotency_key, body, _handle)
