"""일정 문서: Horizon 하나의 작업과 배정을 묶은 반정형 문서(JSON)와 그 모델. 순수 계산이다.

문서는 plan_r0.yaml의 뼈대(tasks + assignments)에 머리말을 더한 것이다. 문서의 시각은 현장 날짜·시각
문자열이고(AG-21) 모델은 Horizon 원점 기준 정수 분이다. 위험 태그와 시간창은 담지 않는다 (CV-11).
읽기는 문서 안에서 알 수 있는 것만 검사한다. 현장 사실(구역·자원·담당자)과의 대조는 넣기 명령이 한다.
"""

from typing import Any

from pydantic import Field, ValidationError

from app.domain.calendar import parse_site_time, site_time
from app.domain.canonical import canonical_hash
from app.domain.models import Assignment, Demand, Frozen, Origin, Predecessor, Requirement


class ScheduleError(ValueError):
    """일정 문서를 읽을 수 없다. reasons는 "위치: 사유" 문장이다."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class Hope(Frozen):
    """희망 영역 [start, end)와 출처."""

    start: int
    end: int
    origin: Origin = "STATED"


class ScheduleTask(Frozen):
    task_id: str = Field(min_length=1)
    unit_id: str
    owner_actor_id: str
    work_type: str
    zone_id: str
    duration: int = Field(gt=0)
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    resource_requirements: tuple[Requirement, ...] = ()  # 작업 값 (CV-11)
    pool_demands: tuple[Demand, ...] = ()  # 작업 값 (CV-11)
    predecessors: tuple[Predecessor, ...] = ()
    preferred_window: Hope | None = None
    pinned: bool = False  # 표시용. 넣을 때 고정으로 살리지 않는다 (AG-27)
    # Agent가 정한 값만 적는다 (AG-32)
    origins: dict[str, Origin] = Field(default_factory=dict)


class Schedule(Frozen):
    """머리말 + 작업 + 배정. 배정은 작업마다 하나다."""

    schedule_id: str
    site_id: str
    pack_hash: str
    plan_revision: int = Field(ge=0)  # 꺼낸 시점의 계획 revision
    exported_by: str
    exported_at: str
    tasks: tuple[ScheduleTask, ...]
    assignments: tuple[Assignment, ...]


# ── 문서 모양 (시각은 문자열) ──────────────────────────────────


class _DocHope(Frozen):
    start: str
    end: str
    origin: Origin = "STATED"


class _DocTask(ScheduleTask):
    duration: int | None = Field(default=None, gt=0)  # 없으면 배정의 끝 − 시작으로 채운다
    preferred_window: _DocHope | None = None  # type: ignore[assignment]


class _DocAssignment(Frozen):
    task_id: str
    start: str
    end: str
    resource_id: str | None = None


class _Document(Frozen):
    schedule_id: str
    site_id: str
    pack_hash: str
    plan_revision: int = Field(ge=0)
    exported_by: str
    exported_at: str
    tasks: tuple[_DocTask, ...]
    assignments: tuple[_DocAssignment, ...]


BODY = ("tasks", "assignments")


def content_hash(document: dict[str, Any]) -> str:
    """문서 본문(작업·배정)의 hash. 머리말은 넣지 않는다: 같은 일정을 다시 꺼내면 같은 값이다 (ST-23)."""
    return canonical_hash({k: document[k] for k in BODY})


def to_document(schedule: Schedule, horizon_start_utc: str, timezone: str) -> dict[str, Any]:
    """모델 → 문서. 분을 현장 날짜·시각 문자열로 바꾼다."""

    def at(minute: int) -> str:
        return site_time(horizon_start_utc, timezone, minute)

    document = schedule.model_dump(mode="json")
    for task in document["tasks"]:
        hope = task["preferred_window"]
        if hope is not None:
            hope["start"], hope["end"] = at(hope["start"]), at(hope["end"])
    for a in document["assignments"]:
        a["start"], a["end"] = at(a["start"]), at(a["end"])
    return document


def from_document(
    document: Any, horizon_start_utc: str, timezone: str, horizon_minutes: int
) -> Schedule:
    """문서 → 모델. 모양·시각·문서 안 정합성을 검사하고, 틀린 곳을 모두 모아 ScheduleError로 낸다.

    빠진 값은 채우지 않는다. 작업 시간만 배정의 끝 − 시작으로 채운다. 위험 태그는 받으면 버린다 (CV-11).
    Horizon 밖 시각은 읽지 못한다.
    """
    if not isinstance(document, dict):
        raise ScheduleError(["schedule: must be an object"])
    raw = dict(document)
    if isinstance(raw.get("tasks"), list):
        raw["tasks"] = [
            {k: v for k, v in t.items() if k != "hazard_tags"} if isinstance(t, dict) else t
            for t in raw["tasks"]
        ]
    try:
        doc = _Document.model_validate(raw)
    except ValidationError as e:
        raise ScheduleError(
            [f"schedule.{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        ) from e

    reasons: list[str] = []

    def minute(where: str, text: str) -> int | None:
        try:
            return parse_site_time(text, horizon_start_utc, timezone, horizon_minutes)
        except ValueError as e:
            reasons.append(f"{where}: {e}")
            return None

    def interval(where: str, start: str, end: str) -> tuple[int, int] | None:
        lo, hi = minute(f"{where}.start", start), minute(f"{where}.end", end)
        if lo is None or hi is None:
            return None
        if lo >= hi:
            reasons.append(f"{where}: start must be before end")
            return None
        return lo, hi

    task_ids = [t.task_id for t in doc.tasks]
    for tid in sorted({t for t in task_ids if task_ids.count(t) > 1}):
        reasons.append(f"schedule.tasks: duplicate task_id {tid!r}")
    placed_ids = [a.task_id for a in doc.assignments]
    for tid in sorted({t for t in placed_ids if placed_ids.count(t) > 1}):
        reasons.append(f"schedule.assignments: duplicate task_id {tid!r}")

    assignments: dict[str, Assignment] = {}
    for i, a in enumerate(doc.assignments):
        where = f"schedule.assignments[{i}]"
        if a.task_id not in task_ids:
            reasons.append(f"{where}: undefined task {a.task_id!r}")
        span = interval(where, a.start, a.end)
        if span is not None:
            assignments[a.task_id] = Assignment(
                task_id=a.task_id, start=span[0], end=span[1], resource_id=a.resource_id
            )

    tasks: list[ScheduleTask] = []
    for i, t in enumerate(doc.tasks):
        where = f"schedule.tasks[{i}]"
        placed = assignments.get(t.task_id)
        if t.task_id not in placed_ids:
            reasons.append(f"{where}: no assignment for {t.task_id!r}")
        duration = t.duration
        if placed is not None:
            length = placed.end - placed.start
            if duration is None:
                duration = length
            elif duration != length:
                reasons.append(f"{where}: end - start = {length} != duration {duration}")
        for p in t.predecessors:
            if p.task_id == t.task_id:
                reasons.append(f"{where}: predecessor refers to itself {t.task_id!r}")
            if p.min_lag < 0:
                reasons.append(f"{where}: predecessor {p.task_id!r} min_lag {p.min_lag} < 0")
        hope = None
        if t.preferred_window is not None:
            w = t.preferred_window
            span = interval(f"{where}.preferred_window", w.start, w.end)
            if span is not None:
                hope = Hope(start=span[0], end=span[1], origin=w.origin)
        if duration is not None:
            tasks.append(
                ScheduleTask(
                    **t.model_dump(exclude={"duration", "preferred_window"}),
                    duration=duration,
                    preferred_window=hope,
                )
            )
    if reasons:
        raise ScheduleError(reasons)
    return Schedule(
        **doc.model_dump(exclude=set(BODY)),
        tasks=tuple(tasks),
        assignments=tuple(assignments[t.task_id] for t in tasks),
    )
