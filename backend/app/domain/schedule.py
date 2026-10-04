"""일정 문서: Horizon 하나의 작업과 배정을 묶은 반정형 문서(JSON)와 그 모델. 순수 계산이다.

문서는 plan_r0.yaml의 뼈대(tasks + assignments)에 머리말을 더한 것이다. 문서의 시각은 현장 날짜·시각
문자열이고(AG-21) 모델은 Horizon 원점 기준 정수 분이다. 위험 태그와 시간창은 담지 않는다 (CV-11).
읽기는 문서 안에서 알 수 있는 것만 작업별로 검사한다. 현장 사실(구역·자원·담당자)과의 대조는 넣기
명령(commands/schedule.py)이 한다.
"""

from typing import Any

from pydantic import Field, ValidationError

from app.domain.calendar import parse_site_time, site_time
from app.domain.canonical import canonical_hash
from app.domain.models import Assignment, Demand, Frozen, Origin, Predecessor, Requirement

# 옛 문서의 칸. 받으면 버린다: 위험 태그는 서버가 도출하고(CV-11) 희망 영역은 쓰지 않는다
DROPPED = ("hazard_tags", "preferred_window")


class ScheduleError(ValueError):
    """일정 문서를 읽을 수 없다. reasons는 "위치: 사유" 문장이다."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


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


class _DocTask(ScheduleTask):
    duration: int | None = Field(default=None, gt=0)  # 없으면 배정의 끝 − 시작으로 채운다


class _DocAssignment(Frozen):
    task_id: str
    start: str
    end: str
    resource_id: str | None = None


BODY = ("tasks", "assignments")


def content_hash(document: dict[str, Any]) -> str:
    """문서 본문(작업·배정)의 hash. 머리말은 넣지 않는다: 같은 일정을 다시 꺼내면 같은 값이다 (ST-23)."""
    return canonical_hash({k: document[k] for k in BODY})


def to_document(schedule: Schedule, horizon_start_utc: str, timezone: str) -> dict[str, Any]:
    """모델 → 문서. 분을 현장 날짜·시각 문자열로 바꾼다."""

    def at(minute: int) -> str:
        return site_time(horizon_start_utc, timezone, minute)

    document = schedule.model_dump(mode="json")
    for a in document["assignments"]:
        a["start"], a["end"] = at(a["start"]), at(a["end"])
    return document


class Entry(Frozen):
    """문서의 작업 하나를 읽은 결과. 읽지 못했으면 task·assignment가 비고 사유가 남는다.

    codes는 사유 코드(화면·판정용), details는 "위치: 사유" 문장이다. hope_dropped는 옛 문서의 희망 영역
    칸을 받아서 버렸다는 표시다."""

    task_id: str
    task: ScheduleTask | None = None
    assignment: Assignment | None = None
    codes: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    hope_dropped: bool = False


class Parsed(Frozen):
    """머리말과 작업별로 읽은 결과. 작업 하나의 오류는 그 작업에만 남는다."""

    schedule_id: str
    site_id: str
    pack_hash: str
    plan_revision: int
    exported_by: str
    exported_at: str
    entries: tuple[Entry, ...]


class _Header(Frozen):
    schedule_id: str
    site_id: str
    pack_hash: str
    plan_revision: int = Field(ge=0)
    exported_by: str
    exported_at: str
    tasks: list[Any]
    assignments: list[Any]


def _field_errors(where: str, e: ValidationError) -> tuple[list[str], list[str]]:
    codes, details = [], []
    for err in e.errors():
        codes.append("FIELD_MISSING" if err["type"] == "missing" else "INVALID_VALUE")
        details.append(f"{where}.{'.'.join(str(p) for p in err['loc'])}: {err['msg']}")
    return codes, details


def read_document(
    document: Any, horizon_start_utc: str, timezone: str, horizon_minutes: int
) -> Parsed:
    """문서 → 머리말과 작업별로 읽은 결과. 문서 전체 모양이 깨졌을 때만 ScheduleError를 낸다.

    작업 하나의 모양·시각·정합성 오류는 그 작업의 사유로 남긴다(넣기 미리보기가 작업별로 판정한다).
    빠진 값은 채우지 않는다. 작업 시간만 배정의 끝 − 시작으로 채운다. 위험 태그는 받으면 버린다 (CV-11).
    옛 문서의 희망 영역 칸도 받으면 버리고, 버렸다는 표시를 남긴다.
    """
    if not isinstance(document, dict):
        raise ScheduleError(["schedule: must be an object"])
    try:
        head = _Header.model_validate(document)
    except ValidationError as e:
        raise ScheduleError(_field_errors("schedule", e)[1]) from e

    def minute(where: str, text: str, codes: list[str], details: list[str]) -> int | None:
        try:
            return parse_site_time(text, horizon_start_utc, timezone, horizon_minutes)
        except ValueError as e:
            codes.append("OUTSIDE_HORIZON" if "outside the horizon" in str(e) else "TIME_INVALID")
            details.append(f"{where}: {e}")
            return None

    def interval(
        where: str, start: str, end: str, codes: list[str], details: list[str]
    ) -> tuple[int, int] | None:
        lo = minute(f"{where}.start", start, codes, details)
        hi = minute(f"{where}.end", end, codes, details)
        if lo is None or hi is None:
            return None
        if lo >= hi:
            codes.append("TIME_INVALID")
            details.append(f"{where}: start must be before end")
            return None
        return lo, hi

    # 배정: 작업 ID로 모은다. 작업을 가리키지 못하는 배정은 문서 전체의 오류다
    raw_ids = [t.get("task_id") if isinstance(t, dict) else None for t in head.tasks]
    broken: list[str] = []
    placed: dict[str, list[tuple[int, _DocAssignment]]] = {}
    for i, a in enumerate(head.assignments):
        where = f"schedule.assignments[{i}]"
        try:
            item = _DocAssignment.model_validate(a)
        except ValidationError as e:
            tid = a.get("task_id") if isinstance(a, dict) else None
            if tid in raw_ids and isinstance(tid, str):
                placed.setdefault(tid, []).append(
                    (i, _DocAssignment(task_id=tid, start="", end=""))
                )
            else:
                broken += _field_errors(where, e)[1]
            continue
        if item.task_id not in raw_ids:
            broken.append(f"{where}: undefined task {item.task_id!r}")
        placed.setdefault(item.task_id, []).append((i, item))
    if broken:
        raise ScheduleError(broken)

    entries: list[Entry] = []
    for i, raw in enumerate(head.tasks):
        where = f"schedule.tasks[{i}]"
        codes: list[str] = []
        details: list[str] = []
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("task_id"), str)
            or not raw["task_id"]
        ):
            raise ScheduleError([f"{where}: task_id missing"])
        tid = raw["task_id"]
        hope_dropped = raw.get("preferred_window") is not None
        try:
            t = _DocTask.model_validate({k: v for k, v in raw.items() if k not in DROPPED})
        except ValidationError as e:
            codes, details = _field_errors(where, e)
            entries.append(
                Entry(
                    task_id=tid,
                    codes=tuple(codes),
                    details=tuple(details),
                    hope_dropped=hope_dropped,
                )
            )
            continue
        if raw_ids.count(tid) > 1:
            codes.append("DUPLICATE_TASK_ID")
            details.append(f"schedule.tasks: duplicate task_id {tid!r}")
        found = placed.get(tid, [])
        assignment = None
        if not found:
            codes.append("NO_ASSIGNMENT")
            details.append(f"{where}: no assignment for {tid!r}")
        elif len(found) > 1:
            codes.append("DUPLICATE_TASK_ID")
            details.append(f"schedule.assignments: duplicate task_id {tid!r}")
        else:
            index, a = found[0]
            span = interval(f"schedule.assignments[{index}]", a.start, a.end, codes, details)
            if span is not None:
                assignment = Assignment(
                    task_id=tid, start=span[0], end=span[1], resource_id=a.resource_id
                )
        duration = t.duration
        if assignment is not None:
            length = assignment.end - assignment.start
            if duration is None:
                duration = length
            elif duration != length:
                codes.append("DURATION_MISMATCH")
                details.append(f"{where}: end - start = {length} != duration {duration}")
        for p in t.predecessors:
            if p.task_id == tid:
                codes.append("PREDECESSOR_INVALID")
                details.append(f"{where}: predecessor refers to itself {tid!r}")
            if p.min_lag < 0:
                codes.append("PREDECESSOR_INVALID")
                details.append(f"{where}: predecessor {p.task_id!r} min_lag {p.min_lag} < 0")
        task = None
        if not codes and duration is not None:
            task = ScheduleTask(**t.model_dump(exclude={"duration"}), duration=duration)
        entries.append(
            Entry(
                task_id=tid,
                task=task,
                assignment=assignment if task is not None else None,
                codes=tuple(dict.fromkeys(codes)),
                details=tuple(details),
                hope_dropped=hope_dropped,
            )
        )
    return Parsed(**head.model_dump(exclude=set(BODY)), entries=tuple(entries))


def from_document(
    document: Any, horizon_start_utc: str, timezone: str, horizon_minutes: int
) -> Schedule:
    """문서 → 모델. 읽지 못한 작업이 하나라도 있으면 틀린 곳을 모두 모아 ScheduleError로 낸다."""
    parsed = read_document(document, horizon_start_utc, timezone, horizon_minutes)
    reasons = [d for e in parsed.entries for d in e.details]
    if reasons:
        raise ScheduleError(reasons)
    return Schedule(
        **parsed.model_dump(exclude={"entries"}),
        tasks=tuple(e.task for e in parsed.entries if e.task is not None),
        assignments=tuple(e.assignment for e in parsed.entries if e.assignment is not None),
    )
