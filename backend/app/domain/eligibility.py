"""자원 적격성 판정. 한 함수를 Rule Engine·Solver 필터·실행 검사·조회·폼이 같이 쓴다 (CV-20).

app 내부에서는 app.domain만 import한다. 속성 이름은 Pack이 선언하고, 여기서는 수치 이상·이하와
목록 포함만 비교한다 (CV-17).
"""

from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from app.domain.models import Frozen, Requirement, Resource, ResourceAttribute

ALL_ZONES = "*"  # allowed_zone_ids의 "모든 구역" 표시 (CV-18)
NUMBER_OPS = ("GTE", "LTE")

ExclusionReason = Literal[
    "TYPE_MISMATCH",  # 자원 유형이 작업이 필요로 하는 유형과 다르다
    "NOT_ALLOWED",  # 이 Unit은 쓸 수 없다
    "NO_AVAILABILITY",  # 가용 구간이 없다
    "ZONE_NOT_ALLOWED",  # 작업 구역에서 쓸 수 없다
    "REQUIREMENT_NOT_MET",  # 요구 조건을 맞추지 못한다(attribute가 어느 속성인지)
]


class Exclusion(Frozen):
    reason: ExclusionReason
    attribute: str | None = None


class Need(Protocol):
    """적격성 판정이 작업에서 읽는 값. Task가 이 모양이다."""

    @property
    def required_resource_type(self) -> str | None: ...
    @property
    def zone_id(self) -> str | None: ...
    @property
    def requirements(self) -> Sequence[Requirement]: ...


class ResourceNeed(Frozen):
    """아직 Task가 아닌 값(폼·시연 요청·조회)의 Need. zone_id가 None이면 구역은 보지 않는다."""

    required_resource_type: str | None
    zone_id: str | None = None
    requirements: tuple[Requirement, ...] = ()


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def requirement_error(req: Requirement, attributes: Mapping[str, ResourceAttribute]) -> str | None:
    """요구 조건이 속성 선언과 맞지 않으면 그 이유. 로더와 폼이 같이 쓴다."""
    decl = attributes.get(req.attribute)
    if decl is None:
        return f"undeclared attribute {req.attribute!r}"
    if decl.type == "NUMBER" and not (req.op in NUMBER_OPS and _number(req.value)):
        return f"attribute {req.attribute!r} is NUMBER: op GTE or LTE with a number"
    if decl.type == "LIST" and not (req.op == "CONTAINS" and isinstance(req.value, str)):
        return f"attribute {req.attribute!r} is LIST: op CONTAINS with a string"
    return None


def meets(req: Requirement, resource: Resource) -> bool:
    """자원이 조건 하나를 맞추는가. 속성 값이 없거나 자료형이 다르면 맞추지 못한 것이다(fail-closed)."""
    value = resource.attributes.get(req.attribute)
    if req.op == "CONTAINS":
        return isinstance(value, tuple) and req.value in value
    if not _number(value) or not _number(req.value):
        return False
    return value >= req.value if req.op == "GTE" else value <= req.value


def exclusion_reasons(task: Need, resource: Resource, unit_id: str) -> list[Exclusion]:
    """(작업, 자원, Unit) → 이 자원을 이 작업에 쓸 수 없는 사유. 비면 적격이다.

    가용 구간은 "하나라도 있는가"만 본다. 배정 구간이 가용 구간 안인지는 Rule Engine AVAILABILITY다.
    """
    out: list[Exclusion] = []
    if resource.resource_type != task.required_resource_type:
        out.append(Exclusion(reason="TYPE_MISMATCH"))
    if unit_id not in resource.allowed_unit_ids:
        out.append(Exclusion(reason="NOT_ALLOWED"))
    if not resource.available_intervals:
        out.append(Exclusion(reason="NO_AVAILABILITY"))
    zones = resource.allowed_zone_ids
    if task.zone_id is not None and ALL_ZONES not in zones and task.zone_id not in zones:
        out.append(Exclusion(reason="ZONE_NOT_ALLOWED"))
    failed = dict.fromkeys(r.attribute for r in task.requirements if not meets(r, resource))
    out += [Exclusion(reason="REQUIREMENT_NOT_MET", attribute=name) for name in failed]
    return out
