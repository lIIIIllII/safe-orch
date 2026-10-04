"""Independent Validator.

Snapshot·SearchSpec을 데이터로 읽어 후보의 전체 계획을 검사한다. DB를 읽지 않는 순수 함수이고,
어떤 후보가 들어와도 예외를 내지 않는다. app.solver를 import하지 않는다.
"""

from collections import Counter
from typing import Any

from app.domain.canonical import canonical_hash
from app.domain.hashes import candidate_hash, search_spec_hash
from app.domain.ids import new_id
from app.domain.models import (
    Assignment,
    Candidate,
    Movable,
    SearchSpec,
    Snapshot,
    SnapshotContent,
    Task,
    Validation,
    ValidationCheck,
)
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts

CHECK_IDS = tuple(f"C{i:02d}" for i in range(1, 12))
BASIC_TO_CHECK = {
    "DURATION": "C03",
    "WINDOW": "C04",
    "PRECEDENCE": "C05",
    "PREDECESSOR_MISSING": "C05",  # 선행 작업 누락
    "RESOURCE_MISSING": "C07",
    "RESOURCE_TYPE": "C07",
    "RESOURCE_AUTH": "C08",
    "RESOURCE_ZONE": "C07",  # 사용 가능 구역
    "RESOURCE_REQUIREMENT": "C07",  # 자원 요구 조건
    "AVAILABILITY": "C09",
    "CALENDAR": "C04",  # 근무 달력
    "POOL_MISSING": "C07",  # 필수 직종의 풀이 없다
    "POOL_CAPACITY": "C09",  # 수량 풀 초과
}
RULE_TYPE_TO_CHECK = {"CAPACITY": "C09", "SEPARATION": "C10"}
NOT_MOVABLE = Movable(time=False, resource=False)

Violation = tuple[str, tuple[str, ...], str]  # (check_id, task_ids, reason_code)


def _c01(
    snapshot: Snapshot,
    facts: SnapshotContent,
    candidate: Candidate,
    spec: SearchSpec | None,
    pack: LoadedPack,
) -> list[str]:
    out = []
    if canonical_hash(snapshot.content) != snapshot.snapshot_hash:
        out.append("SNAPSHOT_HASH_MISMATCH")
    expected = candidate_hash(
        candidate.assignments,
        candidate.base_plan_revision,
        candidate.context_version,
        snapshot.snapshot_hash,
        candidate.search_spec_hash,
        candidate.pack_hash,
    )
    if expected != candidate.candidate_hash:
        out.append("CANDIDATE_HASH_MISMATCH")
    if candidate.snapshot_id != snapshot.snapshot_id:
        out.append("SNAPSHOT_REF_MISMATCH")
    if len({candidate.pack_hash, facts.pack_hash, pack.pack_hash}) != 1:
        out.append("PACK_HASH_MISMATCH")
    if candidate.context_version != facts.context_version:
        out.append("CONTEXT_VERSION_MISMATCH")
    if candidate.base_plan_revision != facts.plan_revision:
        out.append("PLAN_REVISION_MISMATCH")
    if candidate.kind == "REPLAN" and spec is None:
        out.append("SEARCH_SPEC_MISSING")
    if candidate.kind == "RECONFIRM" and spec is not None:
        out.append("SEARCH_SPEC_UNEXPECTED")
        spec = None  # C06과 같이 spec이 없는 것으로 본다. 한 원인은 한 번만 보고한다.
    if spec is not None:
        if (
            candidate.search_spec_id != spec.search_spec_id
            or spec.snapshot_id != snapshot.snapshot_id
        ):
            out.append("SEARCH_SPEC_REF_MISMATCH")
        recomputed = search_spec_hash(
            snapshot.snapshot_hash,
            spec.acting_unit_id,
            spec.axes,
            spec.resource_alternatives,
            spec.time_limit_s,
        )
        if len({candidate.search_spec_hash, spec.hash, recomputed}) != 1:
            out.append("SEARCH_SPEC_HASH_MISMATCH")
    return out


def _c02(facts: SnapshotContent, candidate: Candidate) -> list[Violation]:
    counts = Counter(a.task_id for a in candidate.assignments)
    ready = {t.task_id for t in facts.tasks}
    out: list[Violation] = []
    out += [("C02", (t,), "TASK_MISSING") for t in sorted(ready - set(counts))]
    out += [("C02", (t,), "TASK_UNKNOWN") for t in sorted(set(counts) - ready)]
    out += [("C02", (t,), "TASK_DUPLICATE") for t, n in sorted(counts.items()) if n > 1]
    return out


def _c06(
    facts: SnapshotContent,
    candidate: Candidate,
    spec: SearchSpec | None,
    usable: dict[str, Assignment],
) -> list[Violation]:
    base = facts.base_assignments()
    tasks = facts.task_map()
    axes = spec.axes if spec is not None and candidate.kind == "REPLAN" else {}
    alternatives = spec.resource_alternatives if spec is not None else {}
    out: list[Violation] = []
    for tid, a in usable.items():
        ref = base[tid]
        ax = axes.get(tid, NOT_MOVABLE)
        if not ax.time and a.start != ref.start:
            out.append(("C06", (tid,), "TIME_AXIS_NOT_ALLOWED"))
        if not ax.resource and a.resource_id != ref.resource_id:
            out.append(("C06", (tid,), "RESOURCE_AXIS_NOT_ALLOWED"))
        if ax.resource and a.resource_id not in {ref.resource_id, *alternatives.get(tid, ())}:
            out.append(("C06", (tid,), "RESOURCE_NOT_IN_SPEC"))
    # 고정된 작업은 search_spec과 관계없이 따로 확인한다: 시각·자원 모두 기준 배정 그대로 (AG-27)
    for tid in sorted(facts.pinned_task_ids()):
        a = usable.get(tid)
        if a is None:
            continue
        ref = base[tid]
        if a.start != ref.start or a.resource_id != ref.resource_id:
            out.append(("C06", (tid,), "TASK_PINNED"))
    for tid in sorted(axes):
        task = tasks.get(tid)
        if task is not None and task.unit_id != spec.acting_unit_id:
            out.append(("C06", (tid,), "OUTSIDE_ACTING_UNIT"))
    return out


def _field_values(t: Task) -> dict[str, Any]:
    """task 컬럼 값을 fields 모양으로."""
    return {
        "zone_id": t.zone_id,
        "duration": t.duration,
        "window": {
            "earliest_start": t.earliest_start,
            "latest_start": t.latest_start,
            "latest_end": t.latest_end,
        },
        "resource": {
            "required_resource_type": t.required_resource_type,
            "requested_resource_id": t.requested_resource_id,
            "resource_requirements": [r.model_dump() for r in t.resource_requirements],
        },
    }


def _c11(facts: SnapshotContent, pack: LoadedPack) -> list[Violation]:
    out: list[Violation] = []
    for t in sorted(facts.tasks, key=lambda t: t.task_id):
        wt = pack.work_types.get(t.work_type)
        if wt is None:
            out.append(("C11", (t.task_id,), "UNKNOWN_WORK_TYPE"))
            continue
        if tuple(t.hazard_tags) != wt.hazard_tags:
            out.append(("C11", (t.task_id,), "HAZARD_TAGS_MISMATCH"))
        if t.default_requirements != wt.resource_requirements:
            out.append(("C11", (t.task_id,), "DEFAULT_REQUIREMENTS_MISMATCH"))
        if t.default_demands != wt.pool_demands:
            out.append(("C11", (t.task_id,), "DEFAULT_DEMANDS_MISMATCH"))
        values = _field_values(t)
        for name in wt.critical_fields:
            record = t.fields.get(name)
            if record is None:
                out.append(("C11", (t.task_id,), "FIELD_MISSING"))
            elif record.status != "CONFIRMED":
                out.append(("C11", (t.task_id,), "FIELD_NOT_CONFIRMED"))
            elif record.value != values.get(name):
                out.append(("C11", (t.task_id,), "CONFIRMED_VALUE_MISMATCH"))
    return out


def _conflict_checks(
    snapshot: Snapshot, usable: dict[str, Assignment], pack: LoadedPack
) -> list[Violation]:
    rule_types = {r.rule_id: r.type for r in pack.rules}
    out: list[Violation] = []
    for c in detect_conflicts(snapshot, usable.values(), pack):
        check = BASIC_TO_CHECK.get(c.rule_id) or RULE_TYPE_TO_CHECK.get(
            rule_types.get(c.rule_id, "")
        )
        if check is not None:
            out.append((check, c.task_ids, c.rule_id))
        else:
            # fail-closed: 매핑되지 않는 rule_id는 Pack·코드 무결성 문제로 C01에 보고한다
            out.append(("C01", c.task_ids, "UNMAPPED_RULE"))
    return out


def validate(
    snapshot: Snapshot, candidate: Candidate, search_spec: SearchSpec | None, pack: LoadedPack
) -> Validation:
    facts = snapshot.facts()
    ready = facts.task_map()
    counts = Counter(a.task_id for a in candidate.assignments)
    # 누락·초과·중복은 C02에서만. 나머지는 snapshot에 있고 한 번만 나온 배정만 쓴다.
    usable = {
        a.task_id: a for a in candidate.assignments if a.task_id in ready and counts[a.task_id] == 1
    }

    violations: list[Violation] = [
        ("C01", (), reason) for reason in _c01(snapshot, facts, candidate, search_spec, pack)
    ]
    violations += _c02(facts, candidate)
    violations += _conflict_checks(snapshot, usable, pack)
    violations += _c06(facts, candidate, search_spec, usable)
    violations += _c11(facts, pack)

    checks: list[ValidationCheck] = []
    for check_id in CHECK_IDS:
        # 한 check 안은 (task_ids, reason_code) 순. 배정 순서만 다른 후보는 checks가 같다.
        found = sorted(
            (
                (tuple(sorted(task_ids)), reason)
                for c, task_ids, reason in violations
                if c == check_id
            )
        )
        if not found:
            checks.append(ValidationCheck(check_id=check_id, status="PASS"))
            continue
        status = "INCOMPLETE" if check_id == "C11" else "FAIL"
        checks += [
            ValidationCheck(
                check_id=check_id,
                status=status,
                task_ids=task_ids,
                reason_code=reason,
            )
            for task_ids, reason in found
        ]

    statuses = {c.status for c in checks}
    overall = "INCOMPLETE" if "INCOMPLETE" in statuses else "FAIL" if "FAIL" in statuses else "PASS"
    return Validation(
        validation_id=new_id("val"),
        candidate_id=candidate.candidate_id,
        status=overall,
        checks=tuple(checks),
    )
