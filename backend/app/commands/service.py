"""Command Service 공통 실행 (설계서 §9.4·§11.3-6·§12, 부록 A.14).

한 명령 = write() 트랜잭션 1개: 멱등 키 확인 → 권한·버전 검사와 도메인 변경(handler) →
Audit(APPLIED만) → CommandResult. 잠금 timeout은 저장하지 않고 RETRYABLE_ERROR로 응답한다.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.domain.canonical import canonical_hash
from app.domain.models import Actor, Site
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.commands import get_command_result, insert_audit, insert_command_result
from app.store.repos.site import get_actor, get_site

OutcomeStatus = Literal["APPLIED", "REPLAYED", "REJECTED", "RETRYABLE_ERROR"]


class Body(BaseModel):
    """명령 본문. 모르는 필드는 거절한다."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CommandOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: OutcomeStatus
    reason_codes: tuple[str, ...] = ()
    context_version: int | None = None
    plan_revision: int | None = None
    result_refs: dict[str, Any] = {}


@dataclass
class Result:
    """handler 결과. reason_codes가 비어 있으면 APPLIED다.

    거절이면 run_command가 SAVEPOINT로 handler의 쓰기를 모두 되돌린다.
    replayed: 이미 적용된 효과를 돌려준다(승인 2단계, 같은 source_event_id). Audit를 남기지 않는다.
    """

    reason_codes: list[str] = field(default_factory=list)
    refs: dict[str, Any] = field(default_factory=dict)
    audit_reason: str | None = None
    replayed: bool = False

    def reject(self, code: str) -> None:
        if code not in self.reason_codes:
            self.reason_codes.append(code)


@dataclass(frozen=True)
class CommandContext:
    pack: LoadedPack
    site: Site
    actor_id: str
    actor: Actor | None

    @property
    def site_id(self) -> str:
        return self.site.site_id

    def has_role(self, *roles: str) -> bool:
        return self.actor is not None and any(r in self.actor.roles for r in roles)


def request_hash(command_type: str, actor_id: str, body: Body) -> str:
    return canonical_hash(
        {"command_type": command_type, "actor_id": actor_id, "body": body.model_dump(mode="json")}
    )


def run_command[B: Body](
    pack: LoadedPack,
    command_type: str,
    actor_id: str,
    idempotency_key: str,
    body: B,
    handler: Callable[[sqlite3.Connection, CommandContext, B], Result],
) -> CommandOutcome:
    rhash = request_hash(command_type, actor_id, body)
    site_id = pack.site_id
    try:
        with db.write() as tx:
            before = get_site(tx, site_id)
            if before is None:
                raise LookupError(f"site {site_id} not found")
            stored = get_command_result(tx, idempotency_key)
            if stored is not None:
                if stored["command_type"] != command_type or stored["request_hash"] != rhash:
                    return CommandOutcome(
                        status="REJECTED",
                        reason_codes=("IDEMPOTENCY_MISMATCH",),
                        context_version=before.context_version,
                        plan_revision=before.plan_revision,
                    )
                return CommandOutcome(**{**stored["response"], "status": "REPLAYED"})

            ctx = CommandContext(pack, before, actor_id, get_actor(tx, site_id, actor_id))
            # 거절이면 handler의 쓰기를 되돌린다. SAVEPOINT는 같은 트랜잭션 안의 지점이다(중첩 아님).
            tx.execute("SAVEPOINT command_handler")
            result = handler(tx, ctx, body)
            applied = not result.reason_codes
            if not applied:
                tx.execute("ROLLBACK TO command_handler")
            tx.execute("RELEASE command_handler")
            after = get_site(tx, site_id)
            assert after is not None
            outcome = CommandOutcome(
                status="REPLAYED"
                if applied and result.replayed
                else ("APPLIED" if applied else "REJECTED"),
                reason_codes=tuple(result.reason_codes),
                context_version=after.context_version,
                plan_revision=after.plan_revision,
                result_refs=result.refs,
            )
            if applied and not result.replayed:
                insert_audit(
                    tx,
                    site_id,
                    command_type,
                    actor_id,
                    (before.context_version, before.plan_revision),
                    (after.context_version, after.plan_revision),
                    result.audit_reason,
                    {"body": body.model_dump(mode="json"), "result_refs": result.refs},
                )
            insert_command_result(
                tx,
                site_id,
                idempotency_key,
                command_type,
                actor_id,
                rhash,
                "APPLIED" if applied else "REJECTED",
                result.reason_codes,
                result.refs,
                outcome.model_dump(mode="json"),
            )
            return outcome
    except db.StoreBusyError:
        return CommandOutcome(status="RETRYABLE_ERROR", reason_codes=("RETRYABLE_ERROR",))
