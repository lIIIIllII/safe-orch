"""CP-SAT 2단계 최적화 (설계서 §7, 부록 A.11).

트랜잭션 밖에서 돈다. 모든 READY 작업을 넣고 SearchSpec이 허용하지 않은 작업·축은 기준값 상수다.
1단계 min Σ changed_t, 1단계가 OPTIMAL이면 그 값을 고정하고 2단계 min Σ max(0, s_t − base_t).
Rule 데이터는 Pack에서 읽고 app.rules·app.validator를 import하지 않는다.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model
from ortools.util.python.sorted_interval_list import Domain

from app.domain.calendar import start_domain
from app.domain.ids import new_id
from app.domain.models import Movable, SearchSpec, Snapshot, SolverResult
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


def _build(snapshot: Snapshot, spec: SearchSpec, pack: LoadedPack) -> _Built:
    facts = snapshot.facts()
    horizon = facts.horizon_minutes
    base = facts.base_assignments()
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
        # 근무 달력: 시작은 근무 구간 하나 안에 끝나는 값만. 상수(고정 작업)에도 걸어 위반이면
        # INFEASIBLE이다(Rule Engine CALENDAR·Validator C04와 같은 판정, 부록 A.20).
        m.add_linear_expression_in_domain(
            s, Domain.from_intervals(start_domain(d, facts.work_intervals))
        )

        options: list[str] = []
        if t.required_resource_type is not None:
            if ref.resource_id is not None:
                options.append(ref.resource_id)
            if ax.resource:
                options += [r for r in spec.resource_alternatives.get(tid, ()) if r not in options]
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

        if ax.time or ax.resource:
            ch = m.new_bool_var(f"changed_{tid}")
            if ax.time:
                m.add(s == ref.start).only_enforce_if(~ch)
            base_lit = next((lit for rid, lit in lits if rid == ref.resource_id), None)
            if base_lit is not None:
                m.add_implication(~ch, base_lit)
            elif t.required_resource_type is not None:
                m.add(ch == 1)  # 기준 자원이 없으면 배정 자체가 변경
            b.changed.append(ch)
        if ax.time:
            dl = m.new_int_var(0, horizon, f"delay_{tid}")
            m.add(dl >= s - ref.start)
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

    # 선후행
    for t in tasks:
        for p in t.predecessors:
            if p.task_id in b.starts:
                m.add(
                    b.starts[p.task_id] + b.durations[p.task_id] + p.min_lag <= b.starts[t.task_id]
                )
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


def solve(snapshot: Snapshot, spec: SearchSpec, pack: LoadedPack) -> SolverResult:
    began = time.monotonic()
    b1 = _build(snapshot, spec, pack)
    b1.model.minimize(sum(b1.changed))
    status1, solver1 = _solve_stage(b1.model, spec.time_limit_s)
    stage1: dict[str, Any] = {"status": status1, "changed": None, "solution": None}
    if status1 in SOLVED:
        stage1["changed"] = round(solver1.objective_value)
        stage1["solution"] = _solution(b1, solver1)

    stage2: dict[str, Any] | None = None
    if status1 == "OPTIMAL":
        b2 = _build(snapshot, spec, pack)
        b2.model.add(sum(b2.changed) == stage1["changed"])
        b2.model.minimize(sum(b2.delays))
        remaining = max(spec.time_limit_s - (time.monotonic() - began), 0.1)
        status2, solver2 = _solve_stage(b2.model, remaining)
        stage2 = {"status": status2, "delay": None, "solution": None}
        if status2 in SOLVED:
            stage2["delay"] = round(solver2.objective_value)
            stage2["solution"] = _solution(b2, solver2)

    if stage2 is not None and stage2["solution"] is not None:
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
