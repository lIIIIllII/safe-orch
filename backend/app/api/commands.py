"""명령 엔드포인트. commands 함수를 그대로 부르고 공통 응답 모양으로 돌려준다.

경로의 id는 본문에 두지 않고 API가 합쳐 명령 Body를 만든다(request_hash에 들어간다).
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import Field

from app.api.deps import ActorDep, KeyDep, PackDep, check_site, respond
from app.commands.approval import (
    ApproveRequest,
    ChooseRequest,
    RejectAllRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    choose_candidate,
    reject_all,
    reject_candidate,
    waive,
)
from app.commands.events import (
    EventReport,
    HoldRelease,
    Resolution,
    receive_event,
    release_hold_command,
)
from app.commands.intake import IntakeRequest, submit_intake
from app.commands.messages import (
    ChangeAnswer,
    ChangeReplyRequest,
    Decision,
    ProposalDecision,
    ReplyRequest,
    confirm_proposal,
    discard_proposal,
    reply_change_request,
    reply_message,
)
from app.commands.moves import (
    MoveRequest,
    RemoveRequest,
    ResourceRequest,
    change_resource,
    move_task,
    remove_task,
)
from app.commands.pins import TaskRef, pin_task, unpin_task
from app.commands.runs import CancelRun, cancel_run
from app.commands.schedule import ImportRequest, export_schedule, import_schedule
from app.commands.service import Body
from app.commands.task_edit import EditRequest, edit_task
from app.commands.task_request import (
    TaskRequestForm,
    TaskWithdraw,
    submit_task_request,
    withdraw_task_request,
)

router = APIRouter()


class ApproveBody(Body):
    validation_id: str
    expected_context_version: int


class ChooseBody(Body):
    validation_id: str


class RejectAllBody(Body):
    comment: str


class RejectBody(Body):
    validation_id: str
    reason_code: str
    target_task_ids: tuple[str, ...] = ()
    comment: str = ""


class WaiveBody(Body):
    task_ids: tuple[str, ...] = Field(min_length=1)
    comment: str


class WithdrawBody(Body):
    comment: str = ""


# 값 타입은 명령 계층 정의를 그대로 쓴다. 두 곳에 두면 한쪽만 바뀌어 API가 422를 낸다
class ReplyBody(Body):
    decision: Decision
    comment: str = ""


class ChangeReplyBody(Body):
    answers: tuple[ChangeAnswer, ...] = Field(min_length=1)


class CommentBody(Body):
    comment: str = ""


class MoveBody(Body):
    start: int


class ResourceBody(Body):
    resource_id: str = Field(min_length=1)


class EditBody(Body):
    """작업 카드에서 고친 값(준 것만 바뀐다)."""

    zone_id: str | None = None
    duration: int | None = Field(default=None, gt=0)
    earliest_start: int | None = None
    latest_start: int | None = None
    latest_end: int | None = None
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    # 요청 시작 범위(기준 위치). 계획에 아직 없고 기준 위치가 있는 작업만 고친다
    base_start: int | None = None
    base_start_max: int | None = None


class ReleaseBody(Body):
    resolution: Resolution
    expected_context_version: int
    comment: str = ""


@router.post("/sites/{site_id}/task-requests")
def post_task_request(
    site_id: str, form: TaskRequestForm, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    check_site(site_id, pack)
    return respond(submit_task_request(pack, actor.actor_id, key, form))


@router.post("/sites/{site_id}/intakes")
def post_intake(
    site_id: str, body: IntakeRequest, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """자연어 작업 요청 → Work Intake Run."""
    check_site(site_id, pack)
    return respond(submit_intake(pack, actor.actor_id, key, body))


@router.post("/sites/{site_id}/schedules/export")
def post_schedule_export(site_id: str, pack: PackDep, actor: ActorDep, key: KeyDep) -> JSONResponse:
    """지금 확정 계획을 일정 문서로 꺼내 기록에 남긴다. 문서는 GET /schedules/{id}로 받는다."""
    check_site(site_id, pack)
    return respond(export_schedule(pack, actor.actor_id, key))


@router.post("/sites/{site_id}/schedules/import")
def post_schedule_import(
    site_id: str, body: ImportRequest, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """일정 문서를 넣는다. 넣는 사람의 Unit 작업만, 넣기 하나가 사건 하나다 (ST-24)."""
    check_site(site_id, pack)
    return respond(import_schedule(pack, actor.actor_id, key, body))


@router.post("/tasks/{task_id}/withdraw")
def post_withdraw(
    task_id: str, body: WithdrawBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """Plan에 없는 READY 작업(해결 못 한 요청) 철회."""
    req = TaskWithdraw(task_id=task_id, **body.model_dump())
    return respond(withdraw_task_request(pack, actor.actor_id, key, req))


@router.post("/tasks/{task_id}/pin")
def post_pin(task_id: str, pack: PackDep, actor: ActorDep, key: KeyDep) -> JSONResponse:
    """작업 고정. 담당자는 자기 작업, Supervisor는 모든 작업 (AG-27)."""
    return respond(pin_task(pack, actor.actor_id, key, TaskRef(task_id=task_id)))


@router.post("/tasks/{task_id}/unpin")
def post_unpin(task_id: str, pack: PackDep, actor: ActorDep, key: KeyDep) -> JSONResponse:
    return respond(unpin_task(pack, actor.actor_id, key, TaskRef(task_id=task_id)))


@router.post("/tasks/{task_id}/move")
def post_move(
    task_id: str, body: MoveBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """담당자의 직접 이동: 자기 작업의 시각을 옮기고 바로 확정한다 (AG-31)."""
    req = MoveRequest(task_id=task_id, **body.model_dump())
    return respond(move_task(pack, actor.actor_id, key, req))


@router.post("/tasks/{task_id}/resource")
def post_resource(
    task_id: str, body: ResourceBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """담당자가 작업 카드에서 계획에 있는 자기 작업의 자원을 바꾸고 바로 확정한다 (AG-31)."""
    req = ResourceRequest(task_id=task_id, **body.model_dump())
    return respond(change_resource(pack, actor.actor_id, key, req))


@router.post("/tasks/{task_id}/edit")
def post_edit(
    task_id: str, body: EditBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """담당자가 작업 카드에서 자기 작업의 값을 고치거나 Agent가 정한 값을 확인한다 (AG-33)."""
    req = EditRequest(task_id=task_id, **body.model_dump())
    return respond(edit_task(pack, actor.actor_id, key, req))


@router.post("/tasks/{task_id}/remove")
def post_remove(task_id: str, pack: PackDep, actor: ActorDep, key: KeyDep) -> JSONResponse:
    """담당자가 계획에 있는 자기 작업을 없애고 바로 확정한다 (AG-31). 계획 밖 요청은 withdraw."""
    return respond(remove_task(pack, actor.actor_id, key, RemoveRequest(task_id=task_id)))


@router.post("/candidates/{candidate_id}/approve")
def post_approve(
    candidate_id: str, body: ApproveBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    req = ApproveRequest(candidate_id=candidate_id, **body.model_dump())
    return respond(approve_and_commit(pack, actor.actor_id, key, req))


@router.post("/candidates/{candidate_id}/choose")
def post_choose(
    candidate_id: str, body: ChooseBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """Supervisor가 이 안을 고른다. 고른 안만 협의한다 (AG-29)."""
    req = ChooseRequest(candidate_id=candidate_id, **body.model_dump())
    return respond(choose_candidate(pack, actor.actor_id, key, req))


@router.post("/candidates/{candidate_id}/reject")
def post_reject(
    candidate_id: str, body: RejectBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    req = RejectRequest(candidate_id=candidate_id, **body.model_dump())
    return respond(reject_candidate(pack, actor.actor_id, key, req))


@router.post("/cases/{case_id}/reject-all")
def post_reject_all(
    case_id: str, body: RejectAllBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """이 Case의 살아 있는 안을 한 번에 거절한다. 사유는 한 번만 적는다 (AG-29)."""
    req = RejectAllRequest(case_id=case_id, **body.model_dump())
    return respond(reject_all(pack, actor.actor_id, key, req))


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


@router.post("/messages/{message_id}/reply")
def post_reply(
    message_id: str, body: ReplyBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """받은 요청에 답한다. 제안이 붙은 메시지면 ACCEPT = 확인, DECLINE = 폐기."""
    req = ReplyRequest(message_id=message_id, **body.model_dump())
    return respond(reply_message(pack, actor.actor_id, key, req))


@router.post("/requests/{request_group_id}/reply")
def post_request_reply(
    request_group_id: str, body: ChangeReplyBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    """변경 요청 한 통에 답한다. 그 통의 항목 전부를 항목별 수락·이견으로 한 번에 보낸다 (ST-26)."""
    req = ChangeReplyRequest(request_group_id=request_group_id, **body.model_dump())
    return respond(reply_change_request(pack, actor.actor_id, key, req))


@router.post("/proposals/{proposal_id}/confirm")
def post_confirm(
    proposal_id: str, body: CommentBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    req = ProposalDecision(proposal_id=proposal_id, **body.model_dump())
    return respond(confirm_proposal(pack, actor.actor_id, key, req))


@router.post("/proposals/{proposal_id}/discard")
def post_discard(
    proposal_id: str, body: CommentBody, pack: PackDep, actor: ActorDep, key: KeyDep
) -> JSONResponse:
    req = ProposalDecision(proposal_id=proposal_id, **body.model_dump())
    return respond(discard_proposal(pack, actor.actor_id, key, req))
