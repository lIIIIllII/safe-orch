"""Domain Pack 로더 (설계서 §5.3, 부록 A.4).

기동 시 한 번 읽는다. 위반은 모아서 PackError(사유 목록)로 거절한다.
의존 방향: app.domain ← app.packs ← app.store.repos (app.store를 import하지 않는다).
"""

from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.config import REPO_ROOT
from app.domain.canonical import canonical_hash
from app.domain.models import (
    Actor,
    Assignment,
    FieldRecord,
    Frozen,
    Movable,
    Predecessor,
    Relation,
    Resource,
    Rule,
    Task,
    WorkType,
    WorkUnit,
    Zone,
    ZoneRelation,
)

PACK_FILES = ("pack.yaml", "rules.yaml", "site.yaml", "plan_r0.yaml", "scenario.yaml")
EVALUATORS = {"SEPARATION", "CAPACITY"}
RULE_RELATIONS = {"SAME", "ADJACENT", "BELOW"}
FIXTURE_SOURCE_REF = "fixture:plan_r0"


class PackError(Exception):
    """Pack 검증 실패. reasons에 위반 사유를 모두 담는다."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("invalid domain pack:\n- " + "\n- ".join(reasons))


class NewTaskRequest(Frozen):
    """scenario.yaml의 신규 작업 요청. task로 seed하지 않는다."""

    task_id: str
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
    predecessors: tuple[Predecessor, ...] = ()
    movable: Movable
    requested: Assignment


class LoadedPack(Frozen):
    name: str
    pack_hash: str
    work_types: dict[str, WorkType]
    rules: tuple[Rule, ...]
    site_id: str
    horizon_start_utc: str
    horizon_minutes: int
    units: tuple[WorkUnit, ...]
    actors: tuple[Actor, ...]
    zones: tuple[Zone, ...]
    zone_relations: tuple[ZoneRelation, ...]  # ADJACENT는 양방향 2개
    resources: tuple[Resource, ...]
    tasks: tuple[Task, ...]  # plan_r0 기존 작업, revision 1
    plan_r0: tuple[Assignment, ...]
    new_task: NewTaskRequest

    def hazard_tags(self, work_type: str) -> tuple[str, ...]:
        wt = self.work_types.get(work_type)
        return wt.hazard_tags if wt else ()

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


def _duplicates(where: str, ids: list[Any], reasons: list[str]) -> None:
    for key, n in Counter(ids).items():
        if n > 1:
            reasons.append(f"{where}: duplicate id {key!r}")


def confirmed_fields(
    task: dict[str, Any], critical: tuple[str, ...], source_ref: str
) -> dict[str, FieldRecord]:
    """critical field별 CONFIRMED 확인 기록 (부록 A.8). 값이 없는 필드는 키를 두지 않는다."""
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
        },
    }
    return {
        name: FieldRecord(value=values[name], status="CONFIRMED", source_ref=source_ref)
        for name in critical
        if values.get(name) is not None
    }


def _build(name: str, pack_hash: str, raw: dict[str, Any]) -> LoadedPack:
    reasons: list[str] = []
    pack_doc = _as_dict(raw["pack.yaml"], "pack.yaml", reasons)
    rules_doc = _as_dict(raw["rules.yaml"], "rules.yaml", reasons)
    site_doc = _as_dict(raw["site.yaml"], "site.yaml", reasons)
    plan_doc = _as_dict(raw["plan_r0.yaml"], "plan_r0.yaml", reasons)
    scen_doc = _as_dict(raw["scenario.yaml"], "scenario.yaml", reasons)

    # work_types
    work_types: dict[str, WorkType] = {}
    for wt_id, spec in _as_dict(
        pack_doc.get("work_types"), "pack.yaml.work_types", reasons
    ).items():
        spec = _as_dict(spec, f"pack.yaml.work_types.{wt_id}", reasons)
        if not spec.get("hazard_tags"):
            reasons.append(f"pack.yaml.work_types.{wt_id}: empty hazard_tags")
            continue
        wt = _model(
            WorkType, {"work_type": wt_id, **spec}, f"pack.yaml.work_types.{wt_id}", reasons
        )
        if wt:
            work_types[wt_id] = wt
    tags = {t for wt in work_types.values() for t in wt.hazard_tags}

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

    # resources
    resources: list[Resource] = []
    res_items = _as_list(site_doc.get("resources"), "site.yaml.resources", reasons)
    for i, r in enumerate(res_items):
        where = f"site.yaml.resources[{i}]"
        r = _as_dict(r, where, reasons)
        if r.get("capacity") != 1:
            reasons.append(f"{where}: capacity must be 1, got {r.get('capacity')!r}")
            continue
        res = _model(Resource, r, where, reasons)
        if res:
            resources.append(res)
    _duplicates("site.yaml.resources", [r.resource_id for r in resources], reasons)
    for r in resources:
        for u in (r.owner_unit_id, *r.allowed_unit_ids):
            if u not in unit_ids:
                reasons.append(f"site.yaml.resources {r.resource_id}: undefined unit {u!r}")
    # 가용 구간: 0 ≤ lo < hi ≤ horizon, 시작 순, 겹치거나 맞닿지 않음 (부록 A.4).
    # Rule Engine은 한 구간 포함, CP-SAT은 합집합으로 판정하므로 두 판정이 같도록 강제한다.
    horizon = site_doc.get("horizon_minutes")
    for r in resources:
        where = f"site.yaml.resources {r.resource_id}: available_intervals"
        prev_hi = None
        for lo, hi in r.available_intervals:
            if not 0 <= lo < hi or (isinstance(horizon, int) and hi > horizon):
                reasons.append(f"{where}: [{lo}, {hi}] must satisfy 0 <= lo < hi <= {horizon}")
            if prev_hi is not None and lo <= prev_hi:
                reasons.append(
                    f"{where}: [{lo}, {hi}] must start after previous end {prev_hi}"
                    " (sorted, no overlap or touching)"
                )
            prev_hi = hi
    resource_ids = {r.resource_id for r in resources}

    def check_task_refs(where: str, t: dict[str, Any]) -> None:
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

    # plan_r0 tasks: hazard_tags 입력은 버리고 work_type에서 도출 (I-14)
    tasks: list[Task] = []
    task_items = _as_list(plan_doc.get("tasks"), "plan_r0.yaml.tasks", reasons)
    for i, t in enumerate(task_items):
        where = f"plan_r0.yaml.tasks[{i}]"
        t = {k: v for k, v in _as_dict(t, where, reasons).items() if k != "hazard_tags"}
        check_task_refs(where, t)
        wt = work_types.get(t.get("work_type"))
        task = _model(
            Task,
            {
                **t,
                "revision": 1,
                "lifecycle": "READY",
                "hazard_tags": wt.hazard_tags if wt else (),
                "fields": confirmed_fields(t, wt.critical_fields if wt else (), FIXTURE_SOURCE_REF),
            },
            where,
            reasons,
        )
        if task:
            tasks.append(task)
    tasks_by_id = {t.task_id: t for t in tasks}

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
        if isinstance(nt.get("requested"), dict):
            nt["requested"] = {"task_id": nt.get("task_id"), **nt["requested"]}
        new_task = _model(NewTaskRequest, nt, "scenario.yaml.new_task", reasons)
        # 신규 작업의 기준 배정 = (earliest_start, requested_resource_id) (부록 A.10)
        if new_task and new_task.requested.start != new_task.earliest_start:
            reasons.append(
                f"scenario.yaml.new_task: requested.start {new_task.requested.start}"
                f" != earliest_start {new_task.earliest_start}"
            )
    _duplicates(
        "tasks (plan_r0 + scenario)",
        [t.task_id for t in tasks] + ([new_task.task_id] if new_task else []),
        reasons,
    )

    if reasons:
        raise PackError(reasons)
    try:
        return LoadedPack(
            name=name,
            pack_hash=pack_hash,
            work_types=work_types,
            rules=tuple(rules),
            site_id=site_doc.get("site_id"),
            horizon_start_utc=site_doc.get("horizon_start_utc"),
            horizon_minutes=site_doc.get("horizon_minutes"),
            units=tuple(units),
            actors=tuple(actors),
            zones=tuple(zones),
            zone_relations=tuple(relations),
            resources=tuple(resources),
            tasks=tuple(tasks),
            plan_r0=tuple(plan_r0),
            new_task=new_task,
        )
    except ValidationError as e:
        raise PackError([f"site.yaml: {err['loc']}: {err['msg']}" for err in e.errors()]) from e
