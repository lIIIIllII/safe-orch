"""Safety Rule Engine: 충돌 탐지 (설계서 §6, 부록 A.10).

Rule 위반과 기본 제약 위반을 모두 보고한다. 충돌이 없는 것은 PASS가 아니다(PASS는 Validator).
Solver와 코드를 나눈다: app.solver를 import하지 않는다. Rule 데이터는 Pack에서 읽는다.
"""

from collections.abc import Iterable

from app.domain.calendar import fits_work_interval
from app.domain.models import Assignment, Conflict, Rule, Snapshot, SnapshotContent, Task
from app.packs.loader import LoadedPack


def _overlaps(a: Assignment, b: Assignment) -> bool:
    """[start, end) 겹침. 종료와 다음 시작이 같으면 겹치지 않는다."""
    return a.start < b.end and b.start < a.end


def _separated(x: Assignment, y: Assignment, gap: int) -> bool:
    return x.end + gap <= y.start or y.end + gap <= x.start


def _conflict(
    rule_id: str,
    items: list[tuple[Task, Assignment]],
    resource_id: str | None = None,
) -> Conflict:
    return Conflict(
        rule_id=rule_id,
        task_ids=tuple(sorted({t.task_id for t, _ in items})),
        resource_id=resource_id,
        zone_ids=tuple(sorted({t.zone_id for t, _ in items})),
        interval=(min(a.start for _, a in items), max(a.end for _, a in items)),
    )


# _basic이 내는 rule_id. 이 밖의 값은 내지 않는다.
BASIC_RULE_IDS = frozenset(
    {
        "DURATION",
        "WINDOW",
        "PRECEDENCE",
        "PREDECESSOR_MISSING",
        "RESOURCE_MISSING",
        "RESOURCE_TYPE",
        "RESOURCE_AUTH",
        "AVAILABILITY",
        "CALENDAR",
    }
)


def _basic(facts: SnapshotContent, pairs: list[tuple[Task, Assignment]]) -> list[Conflict]:
    """기본 제약: duration, 시간창·Horizon, 근무 달력, 선후행, 필요 자원, 유형, 권한, 가용 구간.

    CALENDAR는 WINDOW와 따로 판정한다(Horizon 밖이면 둘 다 보고, 부록 A.20).
    선행 작업이 검사 대상 배정에 없으면 건너뛰지 않고 PREDECESSOR_MISSING이다(fail-closed, 부록 A.22).
    """
    out: list[Conflict] = []
    resources = facts.resource_map()
    by_id = {t.task_id: (t, a) for t, a in pairs}
    for t, a in pairs:
        if a.end - a.start != t.duration:
            out.append(_conflict("DURATION", [(t, a)]))
        if (
            a.start < t.earliest_start
            or a.start > t.latest_start
            or a.end > t.latest_end
            or a.start < 0
            or a.end > facts.horizon_minutes
        ):
            out.append(_conflict("WINDOW", [(t, a)]))
        if not fits_work_interval(a.start, a.end, facts.work_intervals):
            out.append(_conflict("CALENDAR", [(t, a)]))
        for p in t.predecessors:
            pred = by_id.get(p.task_id)
            if pred is None:
                out.append(_conflict("PREDECESSOR_MISSING", [(t, a)]))
            elif pred[1].end + p.min_lag > a.start:
                out.append(_conflict("PRECEDENCE", [pred, (t, a)]))
        if t.required_resource_type is not None and a.resource_id is None:
            out.append(_conflict("RESOURCE_MISSING", [(t, a)]))
        if a.resource_id is None:
            continue
        r = resources.get(a.resource_id)
        if r is None or (
            t.required_resource_type is not None and r.resource_type != t.required_resource_type
        ):
            out.append(_conflict("RESOURCE_TYPE", [(t, a)], a.resource_id))
        if r is None:
            continue
        if t.unit_id not in r.allowed_unit_ids:
            out.append(_conflict("RESOURCE_AUTH", [(t, a)], a.resource_id))
        if not any(lo <= a.start and a.end <= hi for lo, hi in r.available_intervals):
            out.append(_conflict("AVAILABILITY", [(t, a)], a.resource_id))
    return out


def _capacity(rule: Rule, pairs: list[tuple[Task, Assignment]]) -> list[Conflict]:
    out = []
    for i, (tx, ax) in enumerate(pairs):
        for ty, ay in pairs[i + 1 :]:
            if (
                ax.resource_id is not None
                and ax.resource_id == ay.resource_id
                and _overlaps(ax, ay)
            ):
                out.append(_conflict(rule.rule_id, [(tx, ax), (ty, ay)], ax.resource_id))
    return out


def _separation(
    rule: Rule, facts: SnapshotContent, pairs: list[tuple[Task, Assignment]]
) -> list[Conflict]:
    """x가 hazard_a, y가 hazard_b이고 rel(zone_x, zone_y) ∈ relations면 gap 이상 떨어져야 한다."""
    out = []
    seen: set[tuple[str, ...]] = set()
    for tx, ax in pairs:
        if rule.hazard_a not in tx.hazard_tags:
            continue
        for ty, ay in pairs:
            if ty.task_id == tx.task_id or rule.hazard_b not in ty.hazard_tags:
                continue
            if facts.rel(tx.zone_id, ty.zone_id) not in rule.relations:
                continue
            if _separated(ax, ay, rule.min_gap):
                continue
            key = tuple(sorted((tx.task_id, ty.task_id)))
            if key not in seen:
                seen.add(key)
                out.append(_conflict(rule.rule_id, [(tx, ax), (ty, ay)]))
    return out


def detect_conflicts(
    snapshot: Snapshot, assignments: Iterable[Assignment], pack: LoadedPack
) -> list[Conflict]:
    """snapshot의 사실(작업·자원·구역 관계)과 Pack Rule로 배정을 검사한다.

    snapshot에 없는 작업의 배정은 건너뛴다(작업 보존은 Validator C02).
    """
    facts = snapshot.facts()
    tasks = facts.task_map()
    pairs = sorted(
        ((tasks[a.task_id], a) for a in assignments if a.task_id in tasks),
        key=lambda p: p[0].task_id,
    )
    out = _basic(facts, pairs)
    for rule in pack.rules:
        if rule.type == "CAPACITY":
            out += _capacity(rule, pairs)
        elif rule.type == "SEPARATION":
            out += _separation(rule, facts, pairs)
    return sorted(out, key=lambda c: (c.rule_id, c.task_ids, c.resource_id or ""))
