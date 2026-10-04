"""CP-SAT 단계 최적화: 변경 작업 수 → 지연 → 자원을 바꾸는 작업 수.

트랜잭션 밖에서 돈다. 모든 READY 작업을 넣고 SearchSpec이 허용하지 않은 작업·축은 기준값 상수다.
1단계 min Σ changed_t, 1단계가 OPTIMAL이면 그 값을 고정하고 2단계 min Σ delay_t.
delay_t는 세 경우다 (CV-29): 희망 영역이 있는 작업은 희망 시작 범위 밖으로 벗어난 거리(앞뒤 모두,
안이면 0), 희망 영역이 없고 Plan에 있는 작업은 Plan의 시작에서 옮긴 거리 |s_t − base_t|, 희망 영역도 없고
Plan에도 없는 작업은 0이다. Plan에 없는 작업의 시작은 희망 시작 범위 안이면(희망 영역이 없으면 시간창 안
어디든) 변경으로 세지 않는다.
목적 순서가 지연 먼저(DELAY_FIRST)면 두 단계의 목적을 바꾼다: 1단계 지연, 2단계 변경 작업 수 (CV-27).
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
    delays: list[cp_model.IntVar] = field(default_factory=list)
    # 작업마다 "기준 자원이 아닌 자원을 쓴다" (자원 축이 열려 있고 고를 자원이 둘 이상인 작업만)
    swaps: list[cp_model.IntVar] = field(default_factory=list)


def _build(snapshot: Snapshot, spec: SearchSpec, pack: LoadedPack) -> _Built:
    facts = snapshot.facts()
    horizon = facts.horizon_minutes
    base = facts.base_assignments()
    wanted = facts.preferred_map()
    in_plan = {a.task_id for a in facts.plan.assignments}
    resources = facts.resource_map()
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

        hope = wanted[tid].start_range(d) if tid in wanted else None
        if ax.time or ax.resource:
            ch = m.new_bool_var(f"changed_{tid}")
            if ax.time and tid not in in_plan:
                # Plan에 없는 작업: 희망 시작 범위 안이면 어디든 변경이 아니다. 희망 영역이 없으면
                # 아직 자기 자리가 없으므로 시간창 안 어디든 변경이 아니다 (CV-29)
                if hope is not None:
                    m.add(s >= hope[0]).only_enforce_if(~ch)
                    m.add(s <= hope[1]).only_enforce_if(~ch)
            elif ax.time:
                m.add(s == ref.start).only_enforce_if(~ch)
            base_lit = next((lit for rid, lit in lits if rid == ref.resource_id), None)
            if base_lit is not None:
                m.add_implication(~ch, base_lit)
            elif t.required_resource_type is not None:
                m.add(ch == 1)  # 기준 자원이 없으면 배정 자체가 변경
            b.changed.append(ch)
            if ax.resource and base_lit is not None and len(lits) > 1:
                swap = m.new_bool_var(f"swap_{tid}")
                m.add(swap + base_lit == 1)
                b.swaps.append(swap)
        if ax.time:
            dl = m.new_int_var(0, horizon, f"delay_{tid}")
            if hope is not None:
                m.add(dl >= hope[0] - s)
                m.add(dl >= s - hope[1])
            elif tid in in_plan:
                # 희망 영역이 없는 계획 작업: 계획의 시작에서 옮긴 거리(앞뒤 모두)
                m.add(dl >= s - ref.start)
                m.add(dl >= ref.start - s)
            # 희망 영역도 없고 Plan에도 없는 작업은 재지 않는다(0)
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


def solve(snapshot: Snapshot, spec: SearchSpec, pack: LoadedPack) -> SolverResult:
    began = time.monotonic()
    delay_first = spec.objective == "DELAY_FIRST"
    b1 = _build(snapshot, spec, pack)
    b1.model.minimize(sum(b1.delays if delay_first else b1.changed))
    status1, solver1 = _solve_stage(b1.model, spec.time_limit_s)
    stage1: dict[str, Any] = {"status": status1, "changed": None, "solution": None}
    if delay_first:
        stage1["delay"] = None
    if status1 in SOLVED:
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
            b2.model.add(sum(b2.changed) == stage1["changed"])
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
                fixed = stage2["changed"] if delay_first else stage1["changed"]
                b3.model.add(sum(b3.changed) == fixed)
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
