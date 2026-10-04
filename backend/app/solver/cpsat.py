"""CP-SAT 단계 최적화: 변경 작업 수 → 지연 → 자원을 바꾸는 작업 수.

목적 순서(SearchSpec.objective)는 접근이 정한다 (CV-27).

트랜잭션 밖에서 돈다. 모든 READY 작업을 넣고 SearchSpec이 허용하지 않은 작업·축은 기준값 상수다.
1단계 min Σ changed_t, 1단계가 OPTIMAL이면 그 값을 고정하고 2단계 min Σ delay_t.
변경과 지연은 작업의 기준 시작 범위에서 잰다 (CV-29): Plan에 있는 작업은 승인된 시작 한 점, 기준 위치가
있는 새 작업은 요청한 시작 범위, 기준 위치가 없는 새 작업(폼 요청)은 범위가 없다. 범위 안이면 변경으로
세지 않고 delay_t는 0이며, 밖이면 변경 하나이고 delay_t는 범위 끝에서 벗어난 거리(앞뒤 모두)다. 범위가
없으면 시간창 안 어디든 변경도 지연도 아니다. 자원이 기준 자원이 아니면 변경이다.
목적 순서가 지연 먼저(DELAY_FIRST)면 두 단계의 목적을 바꾼다: 1단계 지연, 2단계 변경 작업 수 (CV-27).
기존 먼저(EXISTING_FIRST)·추가 먼저(ADDED_FIRST)는 1단계에서 변경 수를 기존 작업(계획에 있는 작업)과
추가 작업(계획에 없는 작업)으로 나눠 사전식으로 줄인다: 먼저 줄일 쪽에 나머지 쪽 작업 수보다 큰 가중치를
곱해 한 번에 푼다(뒤쪽 합이 가중치를 넘지 못하므로 사전식 순서와 같고, 단계 수와 시간 한도는 그대로다).
2단계는 두 수를 고정하고 지연을 줄인다.
마지막 단계: 두 값이 모두 OPTIMAL이면 둘을 고정하고 자원을 바꾸는 작업 수를 줄인다 (CV-12). 변경 수는
작업당 하나라 시각을 바꾼 작업의 자원을 더 바꿔도 앞의 두 값이 같기 때문이다. 이 단계의 해는 2단계의
해를 대신하고, 2단계 결과에 자원을 바꾸는 작업 수(resource_changed)와 이 단계의 상태(resource_status)를
남긴다. 이 단계가 해를 못 내거나 자원을 덜 바꾸는 해가 없으면 2단계 해를 그대로 쓴다.
Rule 데이터는 Pack에서 읽고 app.rules·app.validator를 import하지 않는다.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model
from ortools.util.python.sorted_interval_list import Domain

from app.domain.calendar import start_domain
from app.domain.ids import new_id
from app.domain.models import Movable, SearchSpec, Snapshot, SolverResult, pool_for
from app.packs.loader import LoadedPack

RANDOM_SEED = 0
SOLVED = ("OPTIMAL", "FEASIBLE")


@dataclass
class _Built:
    model: cp_model.CpModel
    starts: dict[str, cp_model.IntVar | int] = field(default_factory=dict)
    durations: dict[str, int] = field(default_factory=dict)
    choices: dict[str, list[tuple[str, cp_model.IntVar]]] = field(default_factory=dict)
    changed: list[cp_model.IntVar] = field(default_factory=list)
    # changed를 기존 작업(계획에 있는 작업)과 추가 작업(계획에 없는 작업)으로 나눈 것
    changed_existing: list[cp_model.IntVar] = field(default_factory=list)
    changed_added: list[cp_model.IntVar] = field(default_factory=list)
    delays: list[cp_model.IntVar] = field(default_factory=list)
    # 작업마다 "기준 자원이 아닌 자원을 쓴다" (자원 축이 열려 있고 고를 자원이 둘 이상인 작업만)
    swaps: list[cp_model.IntVar] = field(default_factory=list)


def _build(snapshot: Snapshot, spec: SearchSpec, pack: LoadedPack) -> _Built:
    facts = snapshot.facts()
    horizon = facts.horizon_minutes
    base = facts.base_assignments()
    resources = facts.resource_map()
    in_plan = {a.task_id for a in facts.plan.assignments}
    tasks = sorted(facts.tasks, key=lambda t: t.task_id)
    m = cp_model.CpModel()
    b = _Built(model=m)
    fixed = Movable(time=False, resource=False)
    by_resource: dict[str, list[cp_model.IntervalVar]] = {}

    for t in tasks:
        tid, d, ax, ref = t.task_id, t.duration, spec.axes.get(t.task_id, fixed), base[t.task_id]
        s = m.new_int_var(0, horizon, f"s_{tid}") if ax.time else m.new_constant(ref.start)
        b.starts[tid], b.durations[tid] = s, d
        m.add(s >= t.earliest_start)
        m.add(s <= t.latest_start)
        m.add(s + d <= t.latest_end)
        m.add(s + d <= horizon)
        # Agent가 건 조건: 시작 범위를 좁힌다. 축이 닫힌 작업에는 걸리지 않는다(SearchSpec이 거절한다)
        cond = spec.conditions.get(tid)
        if cond is not None and ax.time:
            if cond.start_min is not None:
                m.add(s >= cond.start_min)
            if cond.start_max is not None:
                m.add(s <= cond.start_max)
        # 근무 달력: 시작은 근무 구간 하나 안에 끝나는 값만. 상수(고정 작업)에도 걸어 위반이면
        # INFEASIBLE이다(Rule Engine CALENDAR·Validator C04와 같은 판정).
        m.add_linear_expression_in_domain(
            s, Domain.from_intervals(start_domain(d, facts.work_intervals))
        )

        options: list[str] = []
        if t.required_resource_type is not None:
            if ref.resource_id is not None:
                options.append(ref.resource_id)
            if ax.resource:
                options += [r for r in spec.resource_alternatives.get(tid, ()) if r not in options]
            if cond is not None and cond.resource_id is not None:
                options = [r for r in options if r == cond.resource_id]  # 자원 지정
        lits = []
        for rid in options:
            lit = m.new_bool_var(f"x_{tid}_{rid}")
            lits.append((rid, lit))
            by_resource.setdefault(rid, []).append(
                m.new_optional_fixed_size_interval_var(s, d, lit, f"iv_{tid}_{rid}")
            )
        b.choices[tid] = lits
        if t.required_resource_type is not None:
            m.add_exactly_one([lit for _, lit in lits])  # 대안이 없으면 불가능

        # 기준 시작 범위: 이 안이면 변경이 아니고 지연도 0이다. 범위가 없는 새 작업은 아직 자기
        # 자리가 없으므로 시간창 안 어디든 변경이 아니다 (CV-29)
        within = facts.base_range(tid)
        if ax.time or ax.resource:
            ch = m.new_bool_var(f"changed_{tid}")
            if ax.time and within is not None:
                m.add(s >= within[0]).only_enforce_if(~ch)
                m.add(s <= within[1]).only_enforce_if(~ch)
            base_lit = next((lit for rid, lit in lits if rid == ref.resource_id), None)
            if base_lit is not None:
                m.add_implication(~ch, base_lit)
            elif t.required_resource_type is not None:
                m.add(ch == 1)  # 기준 자원이 없으면 배정 자체가 변경
            b.changed.append(ch)
            (b.changed_existing if tid in in_plan else b.changed_added).append(ch)
            if ax.resource and base_lit is not None and len(lits) > 1:
                swap = m.new_bool_var(f"swap_{tid}")
                m.add(swap + base_lit == 1)
                b.swaps.append(swap)
        if ax.time:
            dl = m.new_int_var(0, horizon, f"delay_{tid}")
            if within is not None:
                # 범위 끝에서 벗어난 거리(앞뒤 모두). 범위가 없는 작업은 재지 않는다(0)
                m.add(dl >= within[0] - s)
                m.add(dl >= s - within[1])
            b.delays.append(dl)

    # 자원별 NoOverlap (고정 작업 포함) + 가용 구간 밖은 막힌 구간으로 넣는다
    for rid, intervals in by_resource.items():
        blocked, cursor = [], 0
        r = resources.get(rid)
        for lo, hi in sorted(r.available_intervals if r else ()):
            if lo > cursor:
                blocked.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < horizon:
            blocked.append((cursor, horizon))
        fixed_ivs = [
            m.new_fixed_size_interval_var(lo, hi - lo, f"na_{rid}_{lo}")
            for lo, hi in blocked
            if hi > lo
        ]
        m.add_no_overlap(intervals + fixed_ivs)

    # 수량 풀: 풀별 누적 제약(겹치는 구간의 수요 합 ≤ 수량). 고정 작업도 수요에 들어간다.
    # 필수 직종의 풀이 없으면 해를 내지 않는다(Rule Engine POOL_MISSING과 같은 기준).
    for t in tasks:
        if any(pool_for(facts.pools, t.unit_id, kind) is None for kind in t.required_kinds):
            m.add_bool_or([])
    for pool in sorted(facts.pools, key=lambda p: p.pool_id):
        users = [t for t in tasks if t.unit_id in pool.allowed_unit_ids and pool.kind in t.demands]
        if users:
            m.add_cumulative(
                [
                    m.new_fixed_size_interval_var(
                        b.starts[t.task_id], t.duration, f"pool_{pool.pool_id}_{t.task_id}"
                    )
                    for t in users
                ],
                [t.demands[pool.kind] for t in users],
                pool.quantity,
            )

    # SEPARATION: 순서 bool 쌍
    for rule in pack.rules:
        if rule.type != "SEPARATION":
            continue
        seen: set[tuple[str, str]] = set()
        for x in tasks:
            if rule.hazard_a not in x.hazard_tags:
                continue
            for y in tasks:
                if y.task_id == x.task_id or rule.hazard_b not in y.hazard_tags:
                    continue
                if facts.rel(x.zone_id, y.zone_id) not in rule.relations:
                    continue
                key = tuple(sorted((x.task_id, y.task_id)))
                if key in seen:
                    continue
                seen.add(key)
                sx, sy = b.starts[x.task_id], b.starts[y.task_id]
                before = m.new_bool_var(f"sep_{rule.rule_id}_{x.task_id}_{y.task_id}")
                m.add(sx + x.duration + rule.min_gap <= sy).only_enforce_if(before)
                m.add(sy + y.duration + rule.min_gap <= sx).only_enforce_if(~before)

    # 선후행. 선행 작업이 Snapshot에 없으면 해를 내지 않는다(INFEASIBLE). Rule Engine
    # PREDECESSOR_MISSING·Validator C05와 같은 기준.
    for t in tasks:
        for p in t.predecessors:
            if p.task_id in b.starts:
                m.add(
                    b.starts[p.task_id] + b.durations[p.task_id] + p.min_lag <= b.starts[t.task_id]
                )
            else:
                m.add_bool_or([])
    return b


def _solve_stage(model: cp_model.CpModel, time_limit_s: float) -> tuple[str, cp_model.CpSolver]:
    """한 단계 실행. 테스트는 UNKNOWN 재현을 위해 이 함수를 monkeypatch한다."""
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = RANDOM_SEED
    status = solver.solve(model)
    return solver.status_name(status), solver


def _solution(b: _Built, solver: cp_model.CpSolver) -> list[dict[str, Any]]:
    out = []
    for tid in sorted(b.starts):
        start = solver.value(b.starts[tid])
        rid = next((r for r, lit in b.choices[tid] if solver.value(lit)), None)
        out.append(
            {"task_id": tid, "start": start, "end": start + b.durations[tid], "resource_id": rid}
        )
    return out


def _resource_changes(snapshot: Snapshot, solution: list[dict[str, Any]]) -> int:
    """해가 기준 자원과 다른 자원에 놓은 작업 수."""
    base = snapshot.facts().base_assignments()
    return sum(
        1
        for a in solution
        if a["task_id"] in base and a["resource_id"] != base[a["task_id"]].resource_id
    )


def _first_objective(b: _Built, objective: str) -> Any:
    """1단계 목적식. 기존 먼저·추가 먼저는 가중치로 사전식 순서를 한 번에 푼다."""
    if objective == "DELAY_FIRST":
        return sum(b.delays)
    if objective == "EXISTING_FIRST":
        return (len(b.changed_added) + 1) * sum(b.changed_existing) + sum(b.changed_added)
    if objective == "ADDED_FIRST":
        return (len(b.changed_existing) + 1) * sum(b.changed_added) + sum(b.changed_existing)
    return sum(b.changed)


def _fix_changed(b: _Built, stage1: dict[str, Any], split: bool) -> None:
    """다음 단계에서 1단계의 변경 수를 고정한다. 나눠 푼 목적은 기존·추가 수를 각각 고정한다."""
    if split:
        b.model.add(sum(b.changed_existing) == stage1["changed_existing"])
        b.model.add(sum(b.changed_added) == stage1["changed_added"])
    else:
        b.model.add(sum(b.changed) == stage1["changed"])


def solve(snapshot: Snapshot, spec: SearchSpec, pack: LoadedPack) -> SolverResult:
    began = time.monotonic()
    delay_first = spec.objective == "DELAY_FIRST"
    split = spec.objective in ("EXISTING_FIRST", "ADDED_FIRST")
    b1 = _build(snapshot, spec, pack)
    b1.model.minimize(_first_objective(b1, spec.objective))
    status1, solver1 = _solve_stage(b1.model, spec.time_limit_s)
    stage1: dict[str, Any] = {"status": status1, "changed": None, "solution": None}
    if delay_first:
        stage1["delay"] = None
    if split:
        stage1["changed_existing"] = stage1["changed_added"] = None
    if status1 in SOLVED:
        if split:
            stage1["changed_existing"] = sum(solver1.value(v) for v in b1.changed_existing)
            stage1["changed_added"] = sum(solver1.value(v) for v in b1.changed_added)
            stage1["changed"] = stage1["changed_existing"] + stage1["changed_added"]
        else:
            stage1["delay" if delay_first else "changed"] = round(solver1.objective_value)
        stage1["solution"] = _solution(b1, solver1)

    stage2: dict[str, Any] | None = None
    if status1 == "OPTIMAL":
        b2 = _build(snapshot, spec, pack)
        remaining = max(spec.time_limit_s - (time.monotonic() - began), 0.1)
        if delay_first:
            # 지연 먼저: 1단계의 지연을 고정하고 변경 작업 수를 줄인다
            b2.model.add(sum(b2.delays) == stage1["delay"])
            b2.model.minimize(sum(b2.changed))
            status2, solver2 = _solve_stage(b2.model, remaining)
            stage2 = {
                "status": status2,
                "delay": stage1["delay"],
                "changed": None,
                "solution": None,
            }
            if status2 in SOLVED:
                stage2["changed"] = round(solver2.objective_value)
                stage2["solution"] = _solution(b2, solver2)
        else:
            _fix_changed(b2, stage1, split)
            b2.model.minimize(sum(b2.delays))
            status2, solver2 = _solve_stage(b2.model, remaining)
            stage2 = {"status": status2, "delay": None, "solution": None}
            if status2 in SOLVED:
                stage2["delay"] = round(solver2.objective_value)
                stage2["solution"] = _solution(b2, solver2)

    if stage2 is not None and stage2["solution"] is not None:
        if stage2["status"] == "OPTIMAL":
            # 마지막 단계: 변경 수와 지연을 고정하고 자원을 바꾸는 작업 수를 줄인다 (CV-12)
            b3 = _build(snapshot, spec, pack)
            if b3.swaps:
                if delay_first:
                    b3.model.add(sum(b3.changed) == stage2["changed"])
                else:
                    _fix_changed(b3, stage1, split)
                b3.model.add(sum(b3.delays) == stage2["delay"])
                b3.model.minimize(sum(b3.swaps))
                remaining = max(spec.time_limit_s - (time.monotonic() - began), 0.1)
                status3, solver3 = _solve_stage(b3.model, remaining)
                stage2["resource_status"] = status3
                # 자원을 덜 바꾸는 해가 있을 때만 바꾼다(같으면 2단계 해를 그대로 둔다)
                if status3 in SOLVED:
                    better = _solution(b3, solver3)
                    if _resource_changes(snapshot, better) < _resource_changes(
                        snapshot, stage2["solution"]
                    ):
                        stage2["solution"] = better
        stage2["resource_changed"] = _resource_changes(snapshot, stage2["solution"])
        chosen = 2
    elif stage1["solution"] is not None:
        chosen = 1  # 2단계가 해를 못 내면 1단계 해
    else:
        chosen = None
    return SolverResult(
        solver_result_id=new_id("sr"),
        search_spec_id=spec.search_spec_id,
        stage1=stage1,
        stage2=stage2,
        chosen_stage=chosen,
    )
