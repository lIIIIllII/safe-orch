"""시연 확장 기대값(부록 A.20 D 표)과 §15 값을 다시 계산하는 근거 스크립트.

    cd backend && uv run python -m scripts.verify_demo_values

- 입력은 Pack YAML(site·plan_r0·scenario·rules)을 yaml.safe_load로 직접 읽는다.
- 계산은 app 코드(rules·solver·validator)를 import하지 않는 독립 구현이다. CP-SAT 모델(§7 + CALENDAR)과
  전수 열거를 함께 돌려 상태·변경 수·지연·해가 같은지 대조하고, 최적해가 하나뿐인지 센다.
- 결과는 콘솔에만 쓴다. pytest(tests/test_demo_extension.py)가 같은 값을 운영 코드로 재현한다.
"""

import datetime as dt
import itertools
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from ortools.sat.python import cp_model
from ortools.util.python.sorted_interval_list import Domain

PACK = Path(__file__).resolve().parents[2] / "domain_packs" / "shipyard"
WEEKDAY = "월화수목금토일"
FIXED = (False, False, ())


@dataclass(frozen=True)
class T:
    id: str
    unit: str
    tag: str  # hazard tag (work_type마다 1개)
    zone: str
    d: int
    es: int
    ls: int
    le: int
    rtype: str | None
    res: str | None  # 기준 자원
    mt: bool
    mr: bool
    start: int | None = None  # Plan 배정. 없으면 신규 작업이고 기준 시작 = es (A.10)


@dataclass
class World:
    horizon: int
    cal: tuple[tuple[int, int], ...] | None  # None이면 달력 제약 없음 (비교용)
    work_cal: tuple[tuple[int, int], ...]  # 근무 분 지연 계산용 (항상 Pack 달력)
    origin: dt.datetime
    rules: list[tuple[str, str, str, set[str], int]]
    adjacent: set[tuple[str, str]]
    below: set[tuple[str, str]]
    resources: dict[str, tuple[str, set[str]]]

    def rel(self, a: str, b: str) -> str | None:
        if a == b:
            return "SAME"
        if (a, b) in self.adjacent:
            return "ADJACENT"
        if (a, b) in self.below:
            return "BELOW"
        return None

    def clock(self, m: int) -> str:
        t = self.origin + dt.timedelta(minutes=m)
        return f"{t.month}/{t.day}({WEEKDAY[t.weekday()]}) {t:%H:%M}"

    def work_minutes(self, a: int, b: int) -> int:
        return sum(max(0, min(b, hi) - max(a, lo)) for lo, hi in self.work_cal)


def load() -> tuple[World, list[T], T, list[T]]:
    raw = {f: yaml.safe_load((PACK / f).read_text(encoding="utf-8")) for f in (
        "pack.yaml", "rules.yaml", "site.yaml", "plan_r0.yaml", "scenario.yaml",
    )}  # fmt: skip
    tags = {k: v["hazard_tags"][0] for k, v in raw["pack.yaml"]["work_types"].items()}
    site = raw["site.yaml"]
    origin_utc = dt.datetime.fromisoformat(site["horizon_start_utc"])
    world = World(
        horizon=site["horizon_minutes"],
        cal=tuple(tuple(iv) for iv in site["work_intervals"]),
        work_cal=tuple(tuple(iv) for iv in site["work_intervals"]),
        origin=origin_utc.astimezone(ZoneInfo(site["timezone"])).replace(tzinfo=None),
        rules=[
            (r["rule_id"], r["hazard_a"], r["hazard_b"], set(r["relations"]), r["min_gap"])
            for r in raw["rules.yaml"]["rules"]
            if r["type"] == "SEPARATION"
        ],
        adjacent={
            p
            for r in site["zone_relations"]
            if r["relation"] == "ADJACENT"
            for p in ((r["zones"][0], r["zones"][1]), (r["zones"][1], r["zones"][0]))
        },
        below={
            (r["upper"], r["lower"]) for r in site["zone_relations"] if r["relation"] == "BELOW"
        },
        resources={
            r["resource_id"]: (r["resource_type"], set(r["allowed_unit_ids"]))
            for r in site["resources"]
        },
    )
    unit_of = {a["actor_id"]: a["unit_id"] for a in site["actors"]}
    plan = {a["task_id"]: a["start"] for a in raw["plan_r0.yaml"]["assignments"]}

    def task(t: dict[str, Any], unit: str, movable: dict[str, bool], start: int | None) -> T:
        return T(
            t["task_id"], unit, tags[t["work_type"]], t["zone_id"], t["duration"],
            t["earliest_start"], t["latest_start"], t["latest_end"],
            t.get("required_resource_type"), t.get("requested_resource_id"),
            movable["time"], movable["resource"], start,
        )  # fmt: skip

    fixture = [
        task(t, t["unit_id"], t["movable"], plan[t["task_id"]])
        for t in raw["plan_r0.yaml"]["tasks"]
    ]
    nt = raw["scenario.yaml"]["new_task"]
    a = task(nt, nt["unit_id"], nt["movable"], None)
    form_movable = {"time": True, "resource": False}  # 폼 고정값 (A.14)
    demos = [
        task(d, unit_of[d["requester"]], form_movable, None)
        for d in raw["scenario.yaml"]["demo_requests"]
    ]
    return world, fixture, a, demos


def base(t: T) -> tuple[int, str | None]:
    return (t.start if t.start is not None else t.es, t.res)


def ok_one(w: World, t: T, s: int, r: str | None) -> bool:
    e = s + t.d
    if s < t.es or s > t.ls or e > t.le or s < 0 or e > w.horizon:
        return False
    if w.cal is not None and not any(lo <= s and e <= hi for lo, hi in w.cal):
        return False
    if t.rtype and r is None:
        return False
    return r is None or (w.resources[r][0] == t.rtype and t.unit in w.resources[r][1])


def sep_violations(w: World, x: T, sx: int, y: T, sy: int) -> list[str]:
    """두 작업이 어기는 SEPARATION rule_id 목록 (§6)."""
    return [
        rid
        for rid, ha, hb, rels, gap in w.rules
        for p, sp, q, sq in ((x, sx, y, sy), (y, sy, x, sx))
        if p.tag == ha
        and q.tag == hb
        and w.rel(p.zone, q.zone) in rels
        and not (sp + p.d + gap <= sq or sq + q.d + gap <= sp)
    ]


def ok_pair(w: World, x: T, sx: int, rx: str | None, y: T, sy: int, ry: str | None) -> bool:
    if rx is not None and rx == ry and sx < sy + y.d and sy < sx + x.d:
        return False
    return not sep_violations(w, x, sx, y, sy)


def conflicts(w: World, tasks: list[T]) -> list[tuple[str, tuple[str, ...]]]:
    out = []
    for t in tasks:
        s, _ = base(t)
        if w.cal is not None and not any(lo <= s and s + t.d <= hi for lo, hi in w.cal):
            out.append(("CALENDAR", (t.id,)))
    for i, x in enumerate(tasks):
        for y in tasks[i + 1 :]:
            (sx, rx), (sy, ry) = base(x), base(y)
            pair = tuple(sorted((x.id, y.id)))
            if rx is not None and rx == ry and sx < sy + y.d and sy < sx + x.d:
                out.append(("CAP-RESOURCE", pair))
            out += [(rid, pair) for rid in sep_violations(w, x, sx, y, sy)]
    return sorted(set(out))


def scope(tasks: list[T], conflict_ids: tuple[str, ...], unit: str, level: str) -> list[T]:
    acting = [t for t in tasks if t.unit == unit]
    l0 = [t for t in acting if t.id in conflict_ids]
    if level == "L0":
        return l0
    if level == "L1":
        zones = {t.zone for t in l0}
        res = {base(t)[1] for t in l0} - {None}
        return [t for t in acting if t.zone in zones or base(t)[1] in res]
    return acting


def axes(sc: list[T], try_res: dict[str, list[str]]) -> dict[str, tuple[bool, bool, tuple]]:
    return {t.id: (t.mt, t.mr, tuple(try_res.get(t.id, ())) if t.mr else ()) for t in sc}


def eff_hash(ax: dict[str, tuple[bool, bool, tuple]]) -> tuple:
    return tuple(sorted((k, v) for k, v in ax.items() if v[0] or v[1]))


def cpsat(w: World, tasks: list[T], ax: dict) -> tuple:
    def build() -> tuple:
        m = cp_model.CpModel()
        starts, changed, delays, choices, by_res = {}, [], [], {}, {}
        for t in tasks:
            mt, mr, alts = ax.get(t.id, FIXED)
            bs, br = base(t)
            s = m.new_int_var(0, w.horizon, f"s_{t.id}") if mt else m.new_constant(bs)
            starts[t.id] = s
            m.add(s >= t.es)
            m.add(s <= t.ls)
            m.add(s + t.d <= t.le)
            m.add(s + t.d <= w.horizon)
            if w.cal is not None:
                dom = [[lo, hi - t.d] for lo, hi in w.cal if hi - t.d >= lo]
                m.add_linear_expression_in_domain(s, Domain.from_intervals(dom))
            opts = ([br] if t.rtype and br else []) + (
                [r for r in alts if r != br] if t.rtype and mr else []
            )
            lits = []
            for r in opts:
                lit = m.new_bool_var(f"x_{t.id}_{r}")
                lits.append((r, lit))
                iv = m.new_optional_fixed_size_interval_var(s, t.d, lit, f"iv_{t.id}_{r}")
                by_res.setdefault(r, []).append(iv)
            choices[t.id] = lits
            if t.rtype:
                m.add_exactly_one([lit for _, lit in lits])
            if mt or mr:
                ch = m.new_bool_var(f"ch_{t.id}")
                if mt:
                    m.add(s == bs).only_enforce_if(~ch)
                bl = next((lit for r, lit in lits if r == br), None)
                if bl is not None:
                    m.add_implication(~ch, bl)
                changed.append(ch)
            if mt:
                dl = m.new_int_var(0, w.horizon, f"dl_{t.id}")
                m.add(dl >= s - bs)
                delays.append(dl)
        for ivs in by_res.values():
            m.add_no_overlap(ivs)
        for _, ha, hb, rels, gap in w.rules:
            for x, y in itertools.combinations(tasks, 2):
                for p, q in ((x, y), (y, x)):
                    if p.tag == ha and q.tag == hb and w.rel(p.zone, q.zone) in rels:
                        b = m.new_bool_var(f"sep_{p.id}_{q.id}")
                        m.add(starts[p.id] + p.d + gap <= starts[q.id]).only_enforce_if(b)
                        m.add(starts[q.id] + q.d + gap <= starts[p.id]).only_enforce_if(~b)
        return m, starts, changed, delays, choices

    def run(m: cp_model.CpModel) -> tuple[str, cp_model.CpSolver]:
        sv = cp_model.CpSolver()
        sv.parameters.num_workers = 1
        sv.parameters.random_seed = 0
        sv.parameters.max_time_in_seconds = 10
        return sv.status_name(sv.solve(m)), sv

    m, _, changed, _, _ = build()
    m.minimize(sum(changed))  # 1단계: 변경 작업 수
    status, sv = run(m)
    if status != "OPTIMAL":
        return status, None, None, None
    n = round(sv.objective_value)
    m, starts, changed, delays, choices = build()
    m.add(sum(changed) == n)
    m.minimize(sum(delays))  # 2단계: 달력 분 지연 (§7)
    _, sv = run(m)
    sol = {
        t.id: (sv.value(starts[t.id]), next((r for r, lit in choices[t.id] if sv.value(lit)), None))
        for t in tasks
    }
    return status, n, round(sv.objective_value), sol


def brute(w: World, tasks: list[T], ax: dict) -> tuple:
    mov = [t for t in tasks if ax.get(t.id, FIXED)[0] or ax.get(t.id, FIXED)[1]]
    fixed = [t for t in tasks if t not in mov]
    if not all(ok_one(w, t, *base(t)) for t in fixed) or not all(
        ok_pair(w, x, *base(x), y, *base(y)) for x, y in itertools.combinations(fixed, 2)
    ):
        return "INFEASIBLE", None, None, []
    options = []
    for t in mov:
        mt, mr, alts = ax[t.id]
        bs, br = base(t)
        starts = range(t.es, t.ls + 1) if mt else [bs]
        rs = [br] + [a for a in alts if a != br] if t.rtype and mr else [br]
        options.append(
            [
                (s, r)
                for s in starts
                for r in rs
                if ok_one(w, t, s, r) and all(ok_pair(w, t, s, r, f, *base(f)) for f in fixed)
            ]
        )
    best: tuple | None = None
    sols: list[dict] = []
    for combo in itertools.product(*options):
        if not all(
            ok_pair(w, mov[i], *combo[i], mov[j], *combo[j])
            for i, j in itertools.combinations(range(len(mov)), 2)
        ):
            continue
        key = (
            sum(1 for t, sr in zip(mov, combo, strict=True) if sr != base(t)),
            sum(max(0, s - base(t)[0]) for t, (s, _) in zip(mov, combo, strict=True)),
        )
        sol = {t.id: sr for t, sr in zip(mov, combo, strict=True)}
        if best is None or key < best:
            best, sols = key, [sol]
        elif key == best:
            sols.append(sol)
    if best is None:
        return "INFEASIBLE", None, None, []
    return "OPTIMAL", best[0], best[1], sols


def solve_request(
    w: World, others: list[T], req: T, try_res: dict | None = None, levels=("L0", "L1", "L2")
) -> dict[str, tuple]:
    tasks = sorted(others + [req], key=lambda t: t.id)
    found = conflicts(w, tasks)
    primary = next(c for c in found if req.id in c[1])
    print(f"  충돌 {found}")
    seen: dict[str, tuple] = {}
    out = {}
    for level in levels:
        ax = axes(scope(tasks, primary[1], req.unit, level), try_res or {})
        h = eff_hash(ax)
        a = cpsat(w, tasks, ax)
        b = brute(w, tasks, ax)
        agree = a[:3] == b[:3] and (a[3] is None or any(
            all(a[3][k] == v for k, v in s.items()) for s in b[3]
        ))  # fmt: skip
        same = next((f"={lv}" for lv, hv in seen.items() if hv == h), "")
        seen[level] = h
        out[level] = a
        line = f"  {level}{same}: {a[0]}"
        if a[3] is not None:
            by_id = {t.id: t for t in tasks}
            moved = {k: v for k, v in a[3].items() if v != base(by_id[k])}
            work = sum(w.work_minutes(base(by_id[k])[0], s) for k, (s, _) in moved.items())
            where = ", ".join(f"{k} → {w.clock(s)}({s}) {r or ''}" for k, (s, r) in moved.items())
            line += f" 변경 {a[1]} 지연 {a[2]}/근무 {work} [{where}] 최적해 {len(b[3])}개"
        line += " 전수 일치" if agree else f" 전수 불일치 {b[:3]}"
        print(line)
        if not agree:
            raise SystemExit("CP-SAT와 전수 열거가 다르다")
    return out


def apply(others: list[T], req: T, sol: dict) -> list[T]:
    return [
        replace(t, start=sol[t.id][0], res=sol[t.id][1]) if t.id in sol else t
        for t in others + [req]
    ]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    w, fixture, a, demos = load()
    print(f"원점 {w.clock(0)}, Horizon {w.horizon}분, 근무 구간 {list(w.cal)}")
    print("R0 충돌:", conflicts(w, fixture))
    print(f"\n§15 A ({a.id}):")
    solve_request(w, fixture, a)
    print("Beta (A 자원 축 확인 + SITE-CR-01):")
    solve_request(w, fixture, replace(a, mr=True), {"A": ["SITE-CR-01"]}, ("L0",))
    print("C 고정 후 A (L1·L2 = L0 hash):")
    frozen = [replace(t, mt=False) if t.id == "C" else t for t in fixture]
    solve_request(w, frozen, a)

    print("\nD 표 (R0 기준, 요청 하나씩):")
    for d in demos:
        print(f"{d.id} {d.unit} 요청 {w.clock(d.es)} 창 {w.clock(d.es)}~{w.clock(d.ls)}")
        solve_request(w, fixture, d)

    n4 = next(d for d in demos if d.id == "N4")
    print("\nN4 달력 없이 (비교):")
    solve_request(replace(w, cal=None), fixture, n4, levels=("L0",))

    print("\n누적: A(Alpha 확정) → N1 → N2 → N3 → N4")
    world = apply(fixture, a, solve_request(w, fixture, a, levels=("L1",))["L1"][3])
    for d in demos[:4]:
        print(d.id)
        world = apply(world, d, solve_request(w, world, d, levels=("L0",))["L0"][3])
    print("최종 충돌:", conflicts(w, sorted(world, key=lambda t: t.id)))


if __name__ == "__main__":
    main()
