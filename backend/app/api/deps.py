"""API 공통: 데모 인증(X-Actor), Idempotency-Key, site 확인, §12 응답 (부록 A.18).

본문은 언제나 §12 모양 {status, reason_codes, context_version, plan_revision, result_refs}이다.
HTTP: APPLIED·REPLAYED 200, NOT_AUTHORIZED만 403, *_NOT_FOUND 하나뿐이면 404, 그 밖의 거절 409,
본문 검증 실패 422, RETRYABLE_ERROR 503(+Retry-After). REPLAYED도 저장된 사유로 같은 코드를 낸다.
"""

import re
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse

from app.commands.service import CommandOutcome
from app.domain.models import Actor
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.site import get_actor, get_site

KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ApiError(Exception):
    """명령 함수까지 가지 않는 거절(인증·키·경로). CommandResult를 남기지 않는다."""

    def __init__(self, status_code: int, reason: str, detail: Any = None):
        self.status_code = status_code
        self.reason = reason
        self.detail = detail


def envelope(
    status: str, reasons: list[str], pack: LoadedPack | None, detail: Any = None
) -> dict[str, Any]:
    versions: tuple[int | None, int | None] = (None, None)
    if pack is not None:
        with db.read() as conn:
            site = get_site(conn, pack.site_id)
        if site is not None:
            versions = (site.context_version, site.plan_revision)
    body: dict[str, Any] = {
        "status": status,
        "reason_codes": reasons,
        "context_version": versions[0],
        "plan_revision": versions[1],
        "result_refs": {},
    }
    if detail is not None:
        body["detail"] = detail
    return body


def http_status(outcome: CommandOutcome) -> int:
    codes = outcome.reason_codes
    if outcome.status == "RETRYABLE_ERROR":
        return 503
    if not codes:
        return 200
    if codes == ("NOT_AUTHORIZED",):
        return 403
    if len(codes) == 1 and codes[0].endswith("_NOT_FOUND"):
        return 404
    return 409


def respond(outcome: CommandOutcome) -> JSONResponse:
    code = http_status(outcome)
    headers = {"Retry-After": "1"} if code == 503 else None
    return JSONResponse(outcome.model_dump(mode="json"), status_code=code, headers=headers)


def get_pack(request: Request) -> LoadedPack:
    return request.app.state.pack


PackDep = Annotated[LoadedPack, Depends(get_pack)]


def current_actor(
    pack: PackDep, x_actor: Annotated[str | None, Header(alias="X-Actor")] = None
) -> Actor:
    if not x_actor:
        raise ApiError(401, "ACTOR_REQUIRED")
    with db.read() as conn:
        actor = get_actor(conn, pack.site_id, x_actor)
    if actor is None:
        raise ApiError(401, "UNKNOWN_ACTOR")
    return actor


def idempotency_key(
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if not key:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED")
    if not KEY_PATTERN.match(key):
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY")
    return key


def check_site(site_id: str, pack: LoadedPack) -> None:
    if site_id != pack.site_id:
        raise ApiError(404, "SITE_NOT_FOUND")


ActorDep = Annotated[Actor, Depends(current_actor)]
KeyDep = Annotated[str, Depends(idempotency_key)]
