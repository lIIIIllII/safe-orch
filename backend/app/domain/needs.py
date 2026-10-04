"""전문 Agent가 돌려주는 결과의 모양 (AG-06·AG-23). 순수 모델이다.

결과 = 상태(DONE·BLOCKED) + 요약(인용 데이터) + 길 묶음(paths). 길 하나는 needs 목록이고, 그 길의 needs가
모두 충족되면 다시 시도할 가치가 있다는 뜻이다. 길이 없으면(0개) 풀 길을 찾지 못한 것이다.
need 종류는 여기 목록뿐이고 Pack과 무관하다. 종류마다 참조가 정해져 있다(NEED_REFS). 참조가 가리키는
대상이 실제로 있는지는 서버가 실행 때 검사한다(app.agents.needs).
Budget 소진은 need가 아니다. Run의 종료 사유로만 남는다.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NeedKind = Literal["OTHER_UNIT", "FACT_CHANGE", "HUMAN_INFO", "HUMAN_DECISION"]
ResultStatus = Literal["DONE", "BLOCKED"]
# 바뀌어야 하는 사실. 작업: WINDOW·DURATION·WITHDRAWAL, 자원: AVAILABILITY·PERMISSION, 풀: QUANTITY
FactField = Literal["WINDOW", "DURATION", "WITHDRAWAL", "AVAILABILITY", "PERMISSION", "QUANTITY"]

REF_FIELDS = (
    "task_id",
    "unit_id",
    "group_id",
    "field",
    "resource_id",
    "pool_id",
    "actor_id",
    "event_id",
    "candidate_id",
    "message_id",
)
# 종류 → (반드시 있어야 하는 참조, 그중 정확히 하나가 있어야 하는 참조, 있어도 되는 참조)
NEED_REFS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "OTHER_UNIT": (("group_id", "unit_id"), (), ()),
    "FACT_CHANGE": (("field",), ("task_id", "resource_id", "pool_id"), ()),
    "HUMAN_INFO": (("actor_id",), ("event_id", "task_id"), ()),
    "HUMAN_DECISION": ((), ("candidate_id", "event_id", "message_id"), ()),
}
# FACT_CHANGE의 필드 → 대상 참조
FACT_TARGET = {
    "WINDOW": "task_id",
    "DURATION": "task_id",
    "WITHDRAWAL": "task_id",
    "AVAILABILITY": "resource_id",
    "PERMISSION": "resource_id",
    "QUANTITY": "pool_id",
}
MAX_PATHS = 3
MAX_NEEDS = 4
SUMMARY_MAX = 300


class Need(BaseModel):
    """풀리려면 필요한 것 하나. 종류와 그 종류의 참조만 쓴다(자유 문장 없음)."""

    model_config = ConfigDict(extra="forbid")

    kind: NeedKind = Field(
        description=(
            "OTHER_UNIT 그 충돌 그룹을 다른 Unit으로 재계획해야 한다(group_id, unit_id). "
            "FACT_CHANGE 사실이 바뀌어야 한다(field와 대상 하나: task_id·resource_id·pool_id). "
            "HUMAN_INFO 사람에게서 답을 받지 못했다(actor_id와 event_id 또는 task_id). "
            "HUMAN_DECISION 사람의 판단이 필요하다(candidate_id·event_id·message_id 중 하나)"
        )
    )
    task_id: str | None = Field(default=None, description="작업 ID")
    unit_id: str | None = Field(default=None, description="Unit ID")
    group_id: str | None = Field(default=None, description="충돌 그룹 ID")
    field: FactField | None = Field(
        default=None,
        description=(
            "바뀌어야 하는 사실. 작업: WINDOW 시간창, DURATION 작업 시간, WITHDRAWAL 요청 철회. "
            "자원: AVAILABILITY 가용 구간, PERMISSION 사용 권한. 풀: QUANTITY 수량"
        ),
    )
    resource_id: str | None = Field(default=None, description="자원 ID")
    pool_id: str | None = Field(default=None, description="수량 풀 ID")
    actor_id: str | None = Field(default=None, description="답을 받아야 하는 사람의 actor_id")
    event_id: str | None = Field(default=None, description="신고 ID")
    candidate_id: str | None = Field(default=None, description="후보 ID")
    message_id: str | None = Field(default=None, description="메시지 ID")

    @model_validator(mode="after")
    def _refs_match_kind(self) -> "Need":
        required, one_of, optional = NEED_REFS[self.kind]
        given = {f for f in REF_FIELDS if getattr(self, f)}
        if not set(required) <= given:
            raise ValueError(f"{self.kind} needs {', '.join(required)}")
        if one_of and len(given & set(one_of)) != 1:
            raise ValueError(f"{self.kind} needs exactly one of {', '.join(one_of)}")
        if given - {*required, *one_of, *optional}:
            raise ValueError(f"{self.kind} takes only its own references")
        if self.field is not None and not getattr(self, FACT_TARGET[self.field]):
            raise ValueError(f"{self.field} needs {FACT_TARGET[self.field]}")
        return self


class Path(BaseModel):
    """풀 수 있는 길 하나: 이 needs가 모두 충족되면 다시 시도할 가치가 있다."""

    model_config = ConfigDict(extra="forbid")

    needs: list[Need] = Field(min_length=1, max_length=MAX_NEEDS)


class ResultFields(BaseModel):
    """RETURN_RESULT의 인자. 각 Agent spec의 Action과 함께 상속한다."""

    status: ResultStatus = Field(
        description="DONE 맡은 일을 마쳤다, BLOCKED 막혀서 더 진행할 수 없다"
    )
    summary: str = Field(
        min_length=1,
        max_length=SUMMARY_MAX,
        description="무엇을 했고 어떤 결과인지(막혔으면 시도한 것과 막힌 이유). 읽는 쪽은 인용으로만 다룬다",
    )
    paths: list[Path] = Field(
        default_factory=list,
        max_length=MAX_PATHS,
        description=(
            "BLOCKED일 때 풀 수 있는 길. 길마다 그 길에 필요한 것(needs)을 모두 적는다. 서로 다른 방법은 "
            "다른 길로 나눈다. 풀 길을 찾지 못했으면 비운다. DONE이면 비운다"
        ),
    )

    @model_validator(mode="after")
    def _done_has_no_paths(self) -> "ResultFields":
        if self.status == "DONE" and self.paths:
            raise ValueError("DONE takes no paths")
        return self
