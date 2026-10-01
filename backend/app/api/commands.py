"""명령 엔드포인트 (설계서 §12, 부록 A.18). commands 함수를 그대로 부르고 §12 응답으로 돌려준다.

경로의 id는 본문에 두지 않고 API가 합쳐 명령 Body를 만든다(request_hash에 들어간다).
"""

from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import Field

from app.api.deps import ActorDep, KeyDep, PackDep, check_site, respond
from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    reject_candidate,
    waive,
)
from app.commands.events import EventReport, HoldRelease, receive_event, release_hold_command
from app.commands.runs import CancelRun, cancel_run
from app.commands.service import Body
from app.commands.task_request import (
    TaskRequestForm,
    TaskWithdraw,
    submit_task_request,
    withdraw_task_request,
)
from app.domain.models import Axis

router = APIRouter()


class ApproveBody(Body):
    validation_id: str
    expected_context_version: int


class RejectBody(Body):
    validation_id: str
    reason_code: str
    target_task_ids: tuple[str, ...] = ()
    axes: tuple[Axis, ...] = ()
    comment: str = ""


class WaiveBody(Body):
    task_ids: tuple[str, ...] = Field(min_length=1)
    comment: str


class WithdrawBody(Body):
    comment: str = ""


class ReleaseBody(Body):
    resolution: Literal["FACT_CONFIRMED", "NO_CHANGE"]
    expected_context_version: int
    comment: str = ""


@router.post("/sites/{site_id}/task-requests")
def post_task_request(
    site_id: str, form: TaskRequestForm, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    check_site(site_id, pack)
    return respond(submit_task_request(pack, actor.actor_id, key, form))


@router.post("/tasks/{task_id}/withdraw")
def post_withdraw(
    task_id: str, body: WithdrawBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """Plan에 없는 READY 작업(해결 못 한 요청) 철회 (부록 A.20)."""
    req = TaskWithdraw(task_id=task_id, **body.model_dump())
    return respond(withdraw_task_request(pack, actor.actor_id, key, req))


@router.post("/candidates/{candidate_id}/approve")
def post_approve(
    candidate_id: str, body: ApproveBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    req = ApproveRequest(candidate_id=candidate_id, **body.model_dump())
    return respond(approve_and_commit(pack, actor.actor_id, key, req))


@router.post("/candidates/{candidate_id}/reject")
def post_reject(
    candidate_id: str, body: RejectBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    req = RejectRequest(candidate_id=candidate_id, **body.model_dump())
    return respond(reject_candidate(pack, actor.actor_id, key, req))


@router.post("/consultations/{candidate_id}/waive")
def post_waive(
    candidate_id: str, body: WaiveBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    req = WaiveRequest(candidate_id=candidate_id, **body.model_dump())
    return respond(waive(pack, actor.actor_id, key, req))


@router.post("/sites/{site_id}/events")
def post_event(
    site_id: str, body: EventReport, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    check_site(site_id, pack)
    return respond(receive_event(pack, actor.actor_id, key, body))


@router.post("/holds/{hold_id}/release")
def post_release(
    hold_id: str, body: ReleaseBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    req = HoldRelease(hold_id=hold_id, **body.model_dump())
    return respond(release_hold_command(pack, actor.actor_id, key, req))


@router.post("/runs/{run_id}/cancel")
def post_cancel(run_id: str, pack: PackDep, actor: ActorDep, key: KeyDep) -> JSONResponse:
    return respond(cancel_run(pack, actor.actor_id, key, CancelRun(run_id=run_id)))
