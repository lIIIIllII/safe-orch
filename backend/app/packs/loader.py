"""Domain Pack 로더.

기동 시 한 번 읽는다. 위반은 모아서 PackError(사유 목록)로 거절한다.
의존 방향: app.domain ← app.packs ← app.store.repos (app.store를 import하지 않는다).
"""

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field, ValidationError

from app.config import REPO_ROOT
from app.domain.calendar import fits_work_interval
from app.domain.canonical import canonical_hash
from app.domain.eligibility import ALL_ZONES, ResourceNeed, exclusion_reasons, requirement_error
from app.domain.models import (
    Actor,
    Assignment,
    Demand,
    FieldRecord,
    Frozen,
    Movable,
    Pool,
    PoolDemand,
    PoolKind,
    Predecessor,
    Relation,
    Requirement,
    Resource,
    ResourceAttribute,
    Rule,
    Task,
    WorkType,
    WorkUnit,
    Zone,
    ZoneRelation,
    pool_for,
)

PACK_FORMAT = 3  # pack.yaml pack_format. 형식이 바뀌면 올린다(1 = 버전 없는 옛 형식)
PACK_FILES = ("pack.yaml", "rules.yaml", "site.yaml", "plan_r0.yaml", "scenario.yaml")
EVALUATORS = {"SEPARATION", "CAPACITY"}
RULE_RELATIONS = {"SAME", "ADJACENT", "BELOW"}
FIXTURE_SOURCE_REF = "fixture:plan_r0"
SITE_DESCRIPTION_MAX = 100


class PackError(Exception):
    """Pack 검증 실패. reasons에 위반 사유를 모두 담는다."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("invalid domain pack:\n- " + "\n- ".join(reasons))


class NewTaskRequest(Frozen):
    """scenario.yaml의 신규 작업 요청. task로 seed하지 않는다."""

    task_id: str
    # 시연값 이름. 폼 본문이 아니므로 model_dump에서 뺀다(작업 값으로 쓰는 곳이 있다).
    label: str = Field(min_length=1, exclude=True)
    unit_id: str
    owner_actor_id: str
    work_type: str
    zone_id: str
    duration: int
    earliest_start: int
    latest_start: int
    latest_end: int
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    resource_requirements: tuple[Requirement, ...] = ()
    pool_demands: tuple[Demand, ...] = ()
    predecessors: tuple[Predecessor, ...] = ()
    movable: Movable
    requested: Assignment


class DemoRequest(Frozen):
    """scenario.yaml의 시연 요청. 작업 요청 폼 본문 + 요청자. task로 seed하지 않는다."""

    task_id: str
    label: str = Field(min_length=1)
    requester: str  # actor_id. Unit·담당자는 폼처럼 요청자로 정해진다
    work_type: str
    zone_id: str
    duration: int = Field(gt=0)
    earliest_start: int
    latest_start: int
    latest_end: int
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    resource_requirements: tuple[Requirement, ...] = ()
    pool_demands: tuple[Demand, ...] = ()


class DemoEvent(Frozen):
    """scenario.yaml의 지연 신고 시연 문구."""

    label: str = Field(min_length=1)
    event_type: Literal["DELAY", "OTHER"]
    text: str = Field(min_length=1)
    target_task_id: str | None = None
    answer: str = ""  # 되묻기(ASK_REPORTER)에 신고자가 답하는 문장(사람 역할)


class DemoRejection(Frozen):
    """scenario.yaml의 구조화 거절 시연값. 사유 코드는 명령이 검사한다."""

    label: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    target_task_ids: tuple[str, ...] = ()
    axes: tuple[Literal["TIME", "RESOURCE"], ...] = ()
    comment: str = ""


class DemoIntake(Frozen):
    """scenario.yaml의 자연어 작업 요청 시연값 (Work Intake)."""

    label: str = Field(min_length=1)
    requester: str
    task_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    answer: str = ""  # 확인 질문을 받으면 요청자가 답하는 문장(사람 역할)


class LoadedPack(Frozen):
    name: str
    pack_hash: str
    work_types: dict[str, WorkType]
    resource_types: dict[str, str] = Field(default_factory=dict)  # 코드 → 표시 이름
    resource_attributes: dict[str, ResourceAttribute] = Field(default_factory=dict)  # 속성 선언
    currency: str = ""  # 자원 비용·풀 단가의 화폐 단위
    pool_kinds: dict[str, PoolKind] = Field(default_factory=dict)  # 수량 풀 종류 선언
    pools: tuple[Pool, ...] = ()  # 수량 풀 (CV-23)
    rules: tuple[Rule, ...]
    site_id: str
    site_description: str  # Replanning System prompt의 현장 설명
    timezone: str  # IANA
    horizon_start_utc: str
    horizon_minutes: int
    work_intervals: tuple[tuple[int, int], ...]  # 근무 달력
    units: tuple[WorkUnit, ...]
    actors: tuple[Actor, ...]
    zones: tuple[Zone, ...]
    zone_relations: tuple[ZoneRelation, ...]  # ADJACENT는 양방향 2개
    resources: tuple[Resource, ...]
    tasks: tuple[Task, ...]  # plan_r0 기존 작업, revision 1
    plan_r0: tuple[Assignment, ...]
    new_task: NewTaskRequest
    demo_requests: tuple[DemoRequest, ...] = ()
    demo_events: tuple[DemoEvent, ...] = ()
    demo_rejections: tuple[DemoRejection, ...] = ()
    demo_intakes: tuple[DemoIntake, ...] = ()

    def hazard_tags(self, work_type: str) -> tuple[str, ...]:
        wt = self.work_types.get(work_type)
        return wt.hazard_tags if wt else ()

    def default_requirements(self, work_type: str) -> tuple[Requirement, ...]:
        """작업 유형의 기본 자원 요구 조건. 위험 태그처럼 서버가 도출한다 (CV-11)."""
        wt = self.work_types.get(work_type)
        return wt.resource_requirements if wt else ()

    def default_demands(self, work_type: str) -> tuple[PoolDemand, ...]:
        """작업 유형의 기본 수요와 필수 직종. 서버가 도출한다 (CV-11)."""
        wt = self.work_types.get(work_type)
        return wt.pool_demands if wt else ()

    def rel(self, zone_a: str, zone_b: str) -> Relation | None:
        """hazard_a 작업 구역에서 hazard_b 작업 구역으로 본 관계. 선언이 없으면 None."""
        if zone_a == zone_b:
            return "SAME"
        for r in self.zone_relations:
            if r.zone_a == zone_a and r.zone_b == zone_b:
                return r.relation
        return None


def pack_dir(pack: str) -> Path:
    return REPO_ROOT / "domain_packs" / pack


def compute_pack_hash(raw: dict[str, Any]) -> str:
    """{파일명: safe_load 결과}의 canonical JSON sha256. 주석·공백·줄바꿈은 영향 없음."""
    return canonical_hash(raw)


def load_pack(path: Path) -> "LoadedPack":
    path = Path(path)
    raw: dict[str, Any] = {}
    reasons: list[str] = []
    for fname in PACK_FILES:
        f = path / fname
        if not f.is_file():
            reasons.append(f"{fname}: file not found in {path}")
            continue
        try:
            raw[fname] = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            reasons.append(f"{fname}: YAML error: {e}")
    if reasons:
        raise PackError(reasons)
    try:
        pack_hash = compute_pack_hash(raw)
    except (TypeError, ValueError) as e:
        # 따옴표 없는 날짜(datetime), NaN 등
        raise PackError([f"not canonical JSON (quote date/time strings): {e}"]) from e
    return _build(path.name, pack_hash, raw)


# ── 내부 ───────────────────────────────────────────────────────


def _as_dict(value: Any, where: str, reasons: list[str]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    reasons.append(f"{where}: expected mapping")
    return {}


def _as_list(value: Any, where: str, reasons: list[str]) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    reasons.append(f"{where}: expected list")
    return []


def _model(cls: type, data: Any, where: str, reasons: list[str]) -> Any:
    try:
        return cls.model_validate(data)
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            reasons.append(f"{where}{'.' + loc if loc else ''}: {err['msg']}")
        return None


def _check_intervals(
    where: str, intervals: Any, horizon: Any, reasons: list[str]
) -> tuple[tuple[int, int], ...]:
    """0 ≤ lo < hi ≤ horizon, 시작 순, 겹치거나 맞닿지 않음.

    판정은 "구간 하나에 포함"으로 하므로 구간 모양을 강제해 합집합 판정과 같게 한다.
    """
    out: list[tuple[int, int]] = []
    prev_hi = None
    for iv in intervals:
        if (
            not isinstance(iv, (list, tuple))
            or len(iv) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in iv)
        ):
            reasons.append(f"{where}: {iv!r} must be [lo, hi] integer minutes")
            continue
        lo, hi = iv
        if not 0 <= lo < hi or (isinstance(horizon, int) and hi > horizon):
            reasons.append(f"{where}: [{lo}, {hi}] must satisfy 0 <= lo < hi <= {horizon}")
        if prev_hi is not None and lo <= prev_hi:
            reasons.append(
                f"{where}: [{lo}, {hi}] must start after previous end {prev_hi}"
                " (sorted, no overlap or touching)"
            )
        prev_hi = hi
        out.append((lo, hi))
    return tuple(out)


def _duplicates(where: str, ids: list[Any], reasons: list[str]) -> None:
    for key, n in Counter(ids).items():
        if n > 1:
            reasons.append(f"{where}: duplicate id {key!r}")


def confirmed_fields(
    task: dict[str, Any], critical: tuple[str, ...], source_ref: str
) -> dict[str, FieldRecord]:
    """critical field별 CONFIRMED 확인 기록. 값이 없는 필드는 키를 두지 않는다.

    task의 resource_requirements는 검증된 값이어야 한다(Requirement 또는 같은 모양의 dict).
    """
    values: dict[str, Any] = {
        "zone_id": task.get("zone_id"),
        "duration": task.get("duration"),
        "window": {
            "earliest_start": task.get("earliest_start"),
            "latest_start": task.get("latest_start"),
            "latest_end": task.get("latest_end"),
        },
        "resource": None
        if task.get("required_resource_type") is None and task.get("requested_resource_id") is None
        else {
            "required_resource_type": task.get("required_resource_type"),
            "requested_resource_id": task.get("requested_resource_id"),
            # 요구 조건은 resource 필드에 묶어 값 확인 한 번에 같이 확인한다
            "resource_requirements": [
                Requirement.model_validate(r).model_dump()
                for r in task.get("resource_requirements") or ()
            ],
        },
    }
    return {
        name: FieldRecord(value=values[name], status="CONFIRMED", source_ref=source_ref)
        for name in critical
        if values.get(name) is not None
    }


def _requirements(
    where: str, items: Any, attributes: dict[str, ResourceAttribute], reasons: list[str]
) -> tuple[Requirement, ...]:
    """자원 요구 조건 목록. 선언되지 않은 속성, 속성 자료형과 맞지 않는 비교·값을 거절한다."""
    out: list[Requirement] = []
    for i, item in enumerate(_as_list(items, where, reasons)):
        req = _model(Requirement, item, f"{where}[{i}]", reasons)
        if not req:
            continue
        error = requirement_error(req, attributes)
        if error:
            reasons.append(f"{where}[{i}]: {error}")
        else:
            out.append(req)
    return tuple(out)


def _demands(
    where: str,
    items: Any,
    kinds: dict[str, PoolKind],
    reasons: list[str],
    model: type[Demand] = Demand,
) -> tuple[Any, ...]:
    """수요 목록. 선언되지 않은 종류와 같은 종류의 중복을 거절한다."""
    out: list[Demand] = []
    for i, item in enumerate(_as_list(items, where, reasons)):
        d = _model(model, item, f"{where}[{i}]", reasons)
        if not d:
            continue
        if d.kind not in kinds:
            reasons.append(f"{where}[{i}]: undeclared pool kind {d.kind!r}")
        elif any(x.kind == d.kind for x in out):
            reasons.append(f"{where}[{i}]: duplicate pool kind {d.kind!r}")
        else:
            out.append(d)
    return tuple(out)


def _missing_pools(
    where: str, unit_id: Any, wt: WorkType | None, pools: list[Pool], reasons: list[str]
) -> None:
    """필수 직종의 풀이 그 Unit에 없으면 거절한다(폼 검사와 같은 기준)."""
    for d in wt.pool_demands if wt else ():
        if d.required and pool_for(pools, unit_id, d.kind) is None:
            reasons.append(f"{where}: unit {unit_id!r} has no pool for required kind {d.kind!r}")


EXCLUSION_TEXT = {
    "TYPE_MISMATCH": "resource type mismatch",
    "NOT_ALLOWED": "requester unit not allowed on resource",
    "NO_AVAILABILITY": "resource has no available interval",
    "ZONE_NOT_ALLOWED": "resource not allowed in zone",
    "REQUIREMENT_NOT_MET": "resource does not meet requirement",
}


def _demo_requests(
    scen_doc: dict[str, Any],
    work_types: dict[str, WorkType],
    attributes: dict[str, ResourceAttribute],
    pool_kinds: dict[str, PoolKind],
    pools: list[Pool],
    actors: list[Actor],
    zone_ids: set[str],
    resources: list[Resource],
    horizon: Any,
    work_intervals: tuple[tuple[int, int], ...],
    reasons: list[str],
) -> list[DemoRequest]:
    """시연 요청을 폼 검사와 같은 기준으로 확인한다. 요청 일정은 근무 구간 안이어야 한다."""
    actors_by_id = {a.actor_id: a for a in actors}
    resources_by_id = {r.resource_id: r for r in resources}
    out: list[DemoRequest] = []
    items = _as_list(scen_doc.get("demo_requests"), "scenario.yaml.demo_requests", reasons)
    for i, d in enumerate(items):
        where = f"scenario.yaml.demo_requests[{i}]"
        req = _model(DemoRequest, d, where, reasons)
        if not req:
            continue
        own = _requirements(
            f"{where}.resource_requirements",
            [r.model_dump() for r in req.resource_requirements],
            attributes,
            reasons,
        )
        out.append(req)
        _demands(
            f"{where}.pool_demands", [d.model_dump() for d in req.pool_demands], pool_kinds, reasons
        )
        requester = actors_by_id.get(req.requester)
        if requester is not None:
            _missing_pools(where, requester.unit_id, work_types.get(req.work_type), pools, reasons)
        if requester is None:
            reasons.append(f"{where}: undefined actor {req.requester!r}")
        elif "UNIT_PLANNER" not in requester.roles:
            reasons.append(f"{where}: requester {req.requester!r} is not UNIT_PLANNER")
        wt = work_types.get(req.work_type)
        if wt is None:
            reasons.append(f"{where}: undefined work_type {req.work_type!r}")
        elif "resource" in wt.critical_fields and (
            req.required_resource_type is None or req.requested_resource_id is None
        ):
            reasons.append(f"{where}: {req.work_type} needs required_resource_type and resource")
        if req.zone_id not in zone_ids:
            reasons.append(f"{where}: undefined zone {req.zone_id!r}")
        if req.requested_resource_id is not None:
            res = resources_by_id.get(req.requested_resource_id)
            if res is None:
                reasons.append(f"{where}: undefined resource {req.requested_resource_id!r}")
            elif requester is not None:
                # 폼 검사와 같은 적격성 함수 (CV-20)
                need = ResourceNeed(
                    required_resource_type=req.required_resource_type,
                    zone_id=req.zone_id,
                    requirements=(*(wt.resource_requirements if wt else ()), *own),
                )
                for e in exclusion_reasons(need, res, requester.unit_id):
                    detail = f" {e.attribute!r}" if e.attribute else ""
                    reasons.append(f"{where}: {EXCLUSION_TEXT[e.reason]}{detail}")
        end = req.earliest_start + req.duration
        if not (
            0 <= req.earliest_start <= req.latest_start
            and end <= req.latest_end
            and (not isinstance(horizon, int) or end <= horizon)
        ):
            reasons.append(f"{where}: invalid window")
        elif work_intervals and not fits_work_interval(req.earliest_start, end, work_intervals):
            reasons.append(
                f"{where}: requested [{req.earliest_start}, {end}) outside work_intervals"
            )
    return out


def _check_predecessors(
    where: str,
    task_id: str,
    predecessors: tuple[Predecessor, ...],
    plan_tasks: dict[str, Task],
    reasons: list[str],
) -> None:
    """선행 작업은 plan_r0 작업만(기동 때 seed되는 것), 자기 참조 금지, min_lag ≥ 0."""
    for p in predecessors:
        if p.task_id == task_id:
            reasons.append(f"{where}: predecessor refers to itself {task_id!r}")
        elif p.task_id not in plan_tasks:
            reasons.append(f"{where}: undefined predecessor task {p.task_id!r} (plan_r0 only)")
        if p.min_lag < 0:
            reasons.append(f"{where}: predecessor {p.task_id!r} min_lag {p.min_lag} < 0")


def _check_cycles(tasks: list[Task], reasons: list[str]) -> None:
    """plan_r0 작업끼리의 선후행 순환을 거절한다. 자기 참조는 따로 보고한다."""
    ids = {t.task_id for t in tasks}
    edges = {
        t.task_id: sorted({p.task_id for p in t.predecessors if p.task_id in ids} - {t.task_id})
        for t in tasks
    }
    state: dict[str, int] = {}  # 1 방문 중, 2 끝남

    def visit(tid: str, path: list[str]) -> None:
        state[tid] = 1
        for nxt in edges[tid]:
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt) :] + [nxt]
                reasons.append(f"plan_r0.yaml.tasks: predecessor cycle {' -> '.join(cycle)}")
            elif nxt not in state:
                visit(nxt, [*path, nxt])
        state[tid] = 2

    for tid in sorted(edges):
        if tid not in state:
            visit(tid, [tid])


def _build(name: str, pack_hash: str, raw: dict[str, Any]) -> LoadedPack:
    reasons: list[str] = []
    pack_doc = _as_dict(raw["pack.yaml"], "pack.yaml", reasons)
    rules_doc = _as_dict(raw["rules.yaml"], "rules.yaml", reasons)
    site_doc = _as_dict(raw["site.yaml"], "site.yaml", reasons)
    plan_doc = _as_dict(raw["plan_r0.yaml"], "plan_r0.yaml", reasons)
    scen_doc = _as_dict(raw["scenario.yaml"], "scenario.yaml", reasons)

    if pack_doc.get("pack_format") != PACK_FORMAT:
        reasons.append(
            f"pack.yaml: pack_format must be {PACK_FORMAT}, got {pack_doc.get('pack_format')!r}"
        )

    # resource_attributes: 자원 속성 선언. 자원 값과 요구 조건은 여기 선언된 이름만 쓴다 (CV-17)
    attributes: dict[str, ResourceAttribute] = {}
    for attr_id, spec in _as_dict(
        pack_doc.get("resource_attributes"), "pack.yaml.resource_attributes", reasons
    ).items():
        where = f"pack.yaml.resource_attributes.{attr_id}"
        attr = _model(
            ResourceAttribute, {"name": attr_id, **_as_dict(spec, where, reasons)}, where, reasons
        )
        if attr:
            attributes[attr_id] = attr

    # pool_kinds: 수량 풀 종류 선언. 풀과 수요는 여기 선언된 종류만 쓴다
    pool_kinds: dict[str, PoolKind] = {}
    for kind_id, spec in _as_dict(
        pack_doc.get("pool_kinds"), "pack.yaml.pool_kinds", reasons
    ).items():
        where = f"pack.yaml.pool_kinds.{kind_id}"
        kind = _model(PoolKind, {"kind": kind_id, **_as_dict(spec, where, reasons)}, where, reasons)
        if kind:
            pool_kinds[kind_id] = kind

    # work_types
    work_types: dict[str, WorkType] = {}
    for wt_id, spec in _as_dict(
        pack_doc.get("work_types"), "pack.yaml.work_types", reasons
    ).items():
        spec = _as_dict(spec, f"pack.yaml.work_types.{wt_id}", reasons)
        if not spec.get("hazard_tags"):
            reasons.append(f"pack.yaml.work_types.{wt_id}: empty hazard_tags")
            continue
        defaults = _requirements(
            f"pack.yaml.work_types.{wt_id}.resource_requirements",
            spec.get("resource_requirements"),
            attributes,
            reasons,
        )
        demands = _demands(
            f"pack.yaml.work_types.{wt_id}.pool_demands",
            spec.get("pool_demands"),
            pool_kinds,
            reasons,
            PoolDemand,
        )
        wt = _model(
            WorkType,
            {
                "work_type": wt_id,
                **spec,
                "resource_requirements": defaults,
                "pool_demands": demands,
            },
            f"pack.yaml.work_types.{wt_id}",
            reasons,
        )
        if wt:
            work_types[wt_id] = wt
    tags = {t for wt in work_types.values() for t in wt.hazard_tags}

    # resource_types: 코드 → 표시 이름. 자원의 resource_type은 여기 있어야 한다.
    resource_types: dict[str, str] = {}
    for rt_id, spec in _as_dict(
        pack_doc.get("resource_types"), "pack.yaml.resource_types", reasons
    ).items():
        rt_name = _as_dict(spec, f"pack.yaml.resource_types.{rt_id}", reasons).get("display_name")
        if not isinstance(rt_name, str) or not rt_name.strip():
            reasons.append(f"pack.yaml.resource_types.{rt_id}: display_name missing")
            continue
        resource_types[rt_id] = rt_name

    # rules
    rules: list[Rule] = []
    rule_items = _as_list(rules_doc.get("rules"), "rules.yaml.rules", reasons)
    _duplicates(
        "rules.yaml.rules", [r.get("rule_id") for r in rule_items if isinstance(r, dict)], reasons
    )
    for i, r in enumerate(rule_items):
        where = f"rules.yaml.rules[{i}]"
        r = _as_dict(r, where, reasons)
        if r.get("type") not in EVALUATORS:
            reasons.append(f"{where}: unsupported evaluator {r.get('type')!r}")
            continue
        if r.get("type") == "SEPARATION":
            for key in ("hazard_a", "hazard_b"):
                if r.get(key) not in tags:
                    reasons.append(f"{where}: undefined hazard tag {r.get(key)!r} ({key})")
            for rel in _as_list(r.get("relations"), f"{where}.relations", reasons):
                if rel not in RULE_RELATIONS:
                    reasons.append(f"{where}: undefined relation {rel!r}")
        rule = _model(Rule, r, where, reasons)
        if rule:
            rules.append(rule)
    # CP-SAT는 Pack과 관계없이 모든 자원에 NoOverlap을 건다. Rule Engine·Validator 기준을 맞추려고
    # CAPACITY Rule을 정확히 1개 요구한다.
    n_capacity = sum(1 for r in rule_items if isinstance(r, dict) and r.get("type") == "CAPACITY")
    if n_capacity != 1:
        reasons.append(f"rules.yaml.rules: exactly one CAPACITY rule required, got {n_capacity}")

    # units·actors·zones
    unit_items = _as_list(site_doc.get("units"), "site.yaml.units", reasons)
    units = [
        m
        for i, u in enumerate(unit_items)
        if (m := _model(WorkUnit, u, f"site.yaml.units[{i}]", reasons))
    ]
    unit_ids = {u.unit_id for u in units}
    _duplicates("site.yaml.units", [u.unit_id for u in units], reasons)

    actor_items = _as_list(site_doc.get("actors"), "site.yaml.actors", reasons)
    actors = [
        m
        for i, a in enumerate(actor_items)
        if (m := _model(Actor, a, f"site.yaml.actors[{i}]", reasons))
    ]
    actor_ids = {a.actor_id for a in actors}
    _duplicates("site.yaml.actors", [a.actor_id for a in actors], reasons)
    for a in actors:
        if a.unit_id not in unit_ids:
            reasons.append(f"site.yaml.actors {a.actor_id}: undefined unit {a.unit_id!r}")

    zone_list = _as_list(site_doc.get("zones"), "site.yaml.zones", reasons)
    _duplicates("site.yaml.zones", zone_list, reasons)
    zones = [
        m
        for i, z in enumerate(zone_list)
        if (m := _model(Zone, {"zone_id": z}, f"site.yaml.zones[{i}]", reasons))
    ]
    zone_ids = {z.zone_id for z in zones}

    # zone_relations: ADJACENT는 zones: [a, b], BELOW는 upper/lower만
    relations: list[ZoneRelation] = []
    for i, r in enumerate(
        _as_list(site_doc.get("zone_relations"), "site.yaml.zone_relations", reasons)
    ):
        where = f"site.yaml.zone_relations[{i}]"
        r = _as_dict(r, where, reasons)
        kind = r.get("relation")
        if kind == "ADJACENT":
            pair = r.get("zones")
            if set(r) != {"relation", "zones"} or not isinstance(pair, list) or len(pair) != 2:
                reasons.append(f"{where}: ADJACENT needs exactly zones: [a, b]")
                continue
            ends = [(pair[0], pair[1]), (pair[1], pair[0])]
        elif kind == "BELOW":
            if set(r) != {"relation", "upper", "lower"}:
                reasons.append(f"{where}: BELOW needs direction (upper, lower)")
                continue
            ends = [(r["upper"], r["lower"])]
        else:
            reasons.append(f"{where}: undefined relation {kind!r}")
            continue
        for a, b in ends:
            if a not in zone_ids or b not in zone_ids:
                reasons.append(f"{where}: undefined zone in ({a!r}, {b!r})")
            elif a == b:
                reasons.append(f"{where}: relation to itself {a!r} (SAME is implicit)")
            else:
                relations.append(ZoneRelation(zone_a=a, zone_b=b, relation=kind))
    _duplicates("site.yaml.zone_relations", [(r.zone_a, r.zone_b) for r in relations], reasons)

    # timezone·근무 달력
    timezone = site_doc.get("timezone")
    if not isinstance(timezone, str) or not timezone:
        reasons.append("site.yaml: timezone missing (IANA name, e.g. Asia/Seoul)")
    else:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            reasons.append(f"site.yaml: unknown timezone {timezone!r}")
    # prompt 현장 설명. System을 str.format으로 렌더링하므로 중괄호를 막는다.
    site_description = site_doc.get("site_description")
    if not isinstance(site_description, str) or not site_description.strip():
        reasons.append("site.yaml: site_description missing (one line, <= 100 chars)")
    elif (
        len(site_description.splitlines()) != 1
        or len(site_description) > SITE_DESCRIPTION_MAX
        or "{" in site_description
        or "}" in site_description
    ):
        reasons.append(
            f"site.yaml: site_description must be one line, <= {SITE_DESCRIPTION_MAX} chars,"
            " without braces"
        )
    # 원점 시각은 horizon_start_utc + timezone으로 계산한다
    start_utc = site_doc.get("horizon_start_utc")
    try:
        if datetime.fromisoformat(str(start_utc)).tzinfo is None:
            raise ValueError("no timezone")
    except ValueError:
        reasons.append(
            f"site.yaml: horizon_start_utc must be an ISO time with offset, got {start_utc!r}"
        )
    horizon = site_doc.get("horizon_minutes")
    raw_work = site_doc.get("work_intervals")
    if not isinstance(raw_work, list) or not raw_work:
        reasons.append("site.yaml: work_intervals missing or empty")
        raw_work = []
    work_intervals = _check_intervals("site.yaml.work_intervals", raw_work, horizon, reasons)

    currency = site_doc.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        reasons.append("site.yaml: currency missing (e.g. KRW)")

    # resources
    resources: list[Resource] = []
    res_items = _as_list(site_doc.get("resources"), "site.yaml.resources", reasons)
    for i, r in enumerate(res_items):
        where = f"site.yaml.resources[{i}]"
        r = _as_dict(r, where, reasons)
        if r.get("capacity") != 1:
            reasons.append(f"{where}: capacity must be 1, got {r.get('capacity')!r}")
            continue
        # 사용 가능 구역은 필수다. 모든 구역은 "*"로 적는다(빠뜨린 자원이 모든 구역에서 쓰이지 않게, CV-18)
        allowed_zones = r.get("allowed_zone_ids")
        if allowed_zones in (ALL_ZONES, [ALL_ZONES]):
            r = {**r, "allowed_zone_ids": [ALL_ZONES]}
        elif not isinstance(allowed_zones, list) or not allowed_zones:
            reasons.append(
                f'{where}: allowed_zone_ids required (zone id list, or "{ALL_ZONES}" for all zones)'
            )
            continue
        else:
            for z in allowed_zones:
                if z not in zone_ids:
                    reasons.append(f"{where}: undefined zone {z!r} in allowed_zone_ids")
        values = _as_dict(r.get("attributes") or {}, f"{where}.attributes", reasons)
        for attr_id, value in values.items():
            decl = attributes.get(attr_id)
            number = isinstance(value, (int, float)) and not isinstance(value, bool)
            texts = isinstance(value, list) and all(isinstance(x, str) for x in value)
            if decl is None:
                reasons.append(f"{where}: undeclared attribute {attr_id!r}")
            elif (decl.type == "NUMBER" and not number) or (decl.type == "LIST" and not texts):
                reasons.append(f"{where}: attribute {attr_id!r} must be {decl.type}, got {value!r}")
        res = _model(Resource, r, where, reasons)
        if res:
            resources.append(res)
    _duplicates("site.yaml.resources", [r.resource_id for r in resources], reasons)
    for r in resources:
        if r.resource_type not in resource_types:
            reasons.append(
                f"site.yaml.resources {r.resource_id}: undefined resource_type {r.resource_type!r}"
            )
    for r in resources:
        for u in (r.owner_unit_id, *r.allowed_unit_ids):
            if u not in unit_ids:
                reasons.append(f"site.yaml.resources {r.resource_id}: undefined unit {u!r}")
    # 가용 구간: 0 ≤ lo < hi ≤ horizon, 시작 순, 겹치거나 맞닿지 않음.
    # Rule Engine은 한 구간 포함, CP-SAT은 합집합으로 판정하므로 두 판정이 같도록 강제한다.
    for r in resources:
        where = f"site.yaml.resources {r.resource_id}: available_intervals"
        _check_intervals(where, r.available_intervals, horizon, reasons)
    resource_ids = {r.resource_id for r in resources}

    # pools: 수량 풀. allowed_unit_ids를 생략하면 소유 Unit만 쓴다.
    pools: list[Pool] = []
    for i, p in enumerate(_as_list(site_doc.get("pools"), "site.yaml.pools", reasons)):
        where = f"site.yaml.pools[{i}]"
        p = _as_dict(p, where, reasons)
        if p.get("allowed_unit_ids") is None:
            p = {**p, "allowed_unit_ids": [p.get("owner_unit_id")]}
        pool = _model(Pool, p, where, reasons)
        if not pool:
            continue
        pools.append(pool)
        if pool.kind not in pool_kinds:
            reasons.append(f"{where}: undeclared pool kind {pool.kind!r}")
        for u in (pool.owner_unit_id, *pool.allowed_unit_ids):
            if u not in unit_ids:
                reasons.append(f"{where}: undefined unit {u!r}")
    _duplicates("site.yaml.pools", [p.pool_id for p in pools], reasons)
    # 한 Unit이 한 종류에 쓰는 풀은 하나다. 작업이 쓸 풀이 결정적으로 정해져야 한다 (CV-23)
    for (unit, kind), n in Counter(
        (u, p.kind) for p in pools for u in dict.fromkeys(p.allowed_unit_ids)
    ).items():
        if n > 1:
            reasons.append(
                f"site.yaml.pools: unit {unit!r} has {n} pools of kind {kind!r} (one per unit and kind)"
            )

    def check_task_refs(where: str, t: dict[str, Any]) -> None:
        _missing_pools(where, t.get("unit_id"), work_types.get(t.get("work_type")), pools, reasons)
        if t.get("unit_id") not in unit_ids:
            reasons.append(f"{where}: undefined unit {t.get('unit_id')!r}")
        if t.get("owner_actor_id") not in actor_ids:
            reasons.append(f"{where}: undefined actor {t.get('owner_actor_id')!r}")
        if t.get("work_type") not in work_types:
            reasons.append(f"{where}: undefined work_type {t.get('work_type')!r}")
        if t.get("zone_id") not in zone_ids:
            reasons.append(f"{where}: undefined zone {t.get('zone_id')!r}")
        rid = t.get("requested_resource_id")
        if rid is not None and rid not in resource_ids:
            reasons.append(f"{where}: undefined resource {rid!r}")

    # plan_r0 tasks: hazard_tags 입력은 버리고 work_type에서 도출
    tasks: list[Task] = []
    task_items = _as_list(plan_doc.get("tasks"), "plan_r0.yaml.tasks", reasons)
    for i, t in enumerate(task_items):
        where = f"plan_r0.yaml.tasks[{i}]"
        t = {k: v for k, v in _as_dict(t, where, reasons).items() if k != "hazard_tags"}
        check_task_refs(where, t)
        wt = work_types.get(t.get("work_type"))
        t["resource_requirements"] = _requirements(
            f"{where}.resource_requirements", t.get("resource_requirements"), attributes, reasons
        )
        t["pool_demands"] = _demands(
            f"{where}.pool_demands", t.get("pool_demands"), pool_kinds, reasons
        )
        task = _model(
            Task,
            {
                **t,
                "revision": 1,
                "lifecycle": "READY",
                "hazard_tags": wt.hazard_tags if wt else (),
                "default_requirements": wt.resource_requirements if wt else (),
                "default_demands": wt.pool_demands if wt else (),
                "fields": confirmed_fields(t, wt.critical_fields if wt else (), FIXTURE_SOURCE_REF),
            },
            where,
            reasons,
        )
        if task:
            tasks.append(task)
    tasks_by_id = {t.task_id: t for t in tasks}
    for t in tasks:
        where = f"plan_r0.yaml.tasks {t.task_id}"
        _check_predecessors(where, t.task_id, t.predecessors, tasks_by_id, reasons)
    _check_cycles(tasks, reasons)

    plan_r0: list[Assignment] = []
    for i, a in enumerate(
        _as_list(plan_doc.get("assignments"), "plan_r0.yaml.assignments", reasons)
    ):
        where = f"plan_r0.yaml.assignments[{i}]"
        asg = _model(Assignment, a, where, reasons)
        if not asg:
            continue
        plan_r0.append(asg)
        task = tasks_by_id.get(asg.task_id)
        if task is None:
            reasons.append(f"{where}: undefined task {asg.task_id!r}")
        elif asg.end - asg.start != task.duration:
            reasons.append(
                f"{where}: end - start = {asg.end - asg.start} != duration {task.duration}"
            )
        if asg.resource_id is not None and asg.resource_id not in resource_ids:
            reasons.append(f"{where}: undefined resource {asg.resource_id!r}")
        if work_intervals and not fits_work_interval(asg.start, asg.end, work_intervals):
            reasons.append(f"{where}: [{asg.start}, {asg.end}) outside work_intervals (CALENDAR)")
    _duplicates("plan_r0.yaml.assignments", [a.task_id for a in plan_r0], reasons)

    # scenario
    new_task = None
    nt = scen_doc.get("new_task")
    if nt is None:
        reasons.append("scenario.yaml: new_task missing")
    else:
        nt = {
            k: v
            for k, v in _as_dict(nt, "scenario.yaml.new_task", reasons).items()
            if k != "hazard_tags"
        }
        check_task_refs("scenario.yaml.new_task", nt)
        nt["resource_requirements"] = _requirements(
            "scenario.yaml.new_task.resource_requirements",
            nt.get("resource_requirements"),
            attributes,
            reasons,
        )
        nt["pool_demands"] = _demands(
            "scenario.yaml.new_task.pool_demands", nt.get("pool_demands"), pool_kinds, reasons
        )
        if isinstance(nt.get("requested"), dict):
            nt["requested"] = {"task_id": nt.get("task_id"), **nt["requested"]}
        new_task = _model(NewTaskRequest, nt, "scenario.yaml.new_task", reasons)
        # 신규 작업의 기준 배정 = (earliest_start, requested_resource_id)
        if new_task and new_task.requested.start != new_task.earliest_start:
            reasons.append(
                f"scenario.yaml.new_task: requested.start {new_task.requested.start}"
                f" != earliest_start {new_task.earliest_start}"
            )
        if new_task:
            _check_predecessors(
                "scenario.yaml.new_task",
                new_task.task_id,
                new_task.predecessors,
                tasks_by_id,
                reasons,
            )
        if new_task and work_intervals:
            req = new_task.requested
            if not fits_work_interval(req.start, req.end, work_intervals):
                reasons.append("scenario.yaml.new_task: requested outside work_intervals")

    # 시연 요청·신고 문구
    demo_requests = _demo_requests(
        scen_doc,
        work_types,
        attributes,
        pool_kinds,
        pools,
        actors,
        zone_ids,
        resources,
        horizon,
        work_intervals,
        reasons,
    )
    _duplicates(
        "tasks (plan_r0 + scenario)",
        [t.task_id for t in tasks]
        + ([new_task.task_id] if new_task else [])
        + [d.task_id for d in demo_requests],
        reasons,
    )
    known_tasks = {t.task_id for t in tasks} | ({new_task.task_id} if new_task else set())
    demo_events: list[DemoEvent] = []
    for i, e in enumerate(
        _as_list(scen_doc.get("demo_events"), "scenario.yaml.demo_events", reasons)
    ):
        where = f"scenario.yaml.demo_events[{i}]"
        ev = _model(DemoEvent, e, where, reasons)
        if not ev:
            continue
        demo_events.append(ev)
        if ev.target_task_id is not None and ev.target_task_id not in known_tasks:
            reasons.append(f"{where}: undefined task {ev.target_task_id!r}")
    demo_rejections: list[DemoRejection] = []
    for i, x in enumerate(
        _as_list(scen_doc.get("demo_rejections"), "scenario.yaml.demo_rejections", reasons)
    ):
        where = f"scenario.yaml.demo_rejections[{i}]"
        rej = _model(DemoRejection, x, where, reasons)
        if not rej:
            continue
        demo_rejections.append(rej)
        for tid in rej.target_task_ids:
            if tid not in known_tasks:
                reasons.append(f"{where}: undefined task {tid!r}")

    demo_intakes: list[DemoIntake] = []
    by_actor = {a.actor_id: a for a in actors}
    plan_ids = {t.task_id for t in tasks}
    for i, x in enumerate(
        _as_list(scen_doc.get("demo_intakes"), "scenario.yaml.demo_intakes", reasons)
    ):
        where = f"scenario.yaml.demo_intakes[{i}]"
        intake = _model(DemoIntake, x, where, reasons)
        if not intake:
            continue
        demo_intakes.append(intake)
        requester = by_actor.get(intake.requester)
        if requester is None or "UNIT_PLANNER" not in requester.roles:
            reasons.append(f"{where}: requester {intake.requester!r} is not UNIT_PLANNER")
        if intake.task_id in plan_ids:
            reasons.append(f"{where}: task_id {intake.task_id!r} is a plan_r0 task")

    if reasons:
        raise PackError(reasons)
    try:
        return LoadedPack(
            name=name,
            pack_hash=pack_hash,
            work_types=work_types,
            resource_types=resource_types,
            resource_attributes=attributes,
            currency=currency,
            pool_kinds=pool_kinds,
            pools=tuple(pools),
            rules=tuple(rules),
            site_id=site_doc.get("site_id"),
            site_description=site_description,
            timezone=timezone,
            horizon_start_utc=site_doc.get("horizon_start_utc"),
            horizon_minutes=site_doc.get("horizon_minutes"),
            work_intervals=work_intervals,
            units=tuple(units),
            actors=tuple(actors),
            zones=tuple(zones),
            zone_relations=tuple(relations),
            resources=tuple(resources),
            tasks=tuple(tasks),
            plan_r0=tuple(plan_r0),
            new_task=new_task,
            demo_requests=tuple(demo_requests),
            demo_events=tuple(demo_events),
            demo_rejections=tuple(demo_rejections),
            demo_intakes=tuple(demo_intakes),
        )
    except ValidationError as e:
        raise PackError([f"site.yaml: {err['loc']}: {err['msg']}" for err in e.errors()]) from e
