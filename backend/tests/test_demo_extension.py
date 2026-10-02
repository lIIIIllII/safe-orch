"""시연 확장: 3일 Horizon·근무 달력·새 충돌·요청 철회 (부록 A.20).

D 표(N1–N5)와 §15 값(L0·Alpha·Beta)을 확장 fixture에서 재현하고, 근무 분 지연, meta·scenario API,
철회(F-2)를 확인한다. 기대값의 독립 근거는 scripts/verify_demo_values.py(전수 열거 + CP-SAT)다.
"""

import uuid

import pytest
from closing import ClosingModel, close_to_escalation
from conftest import take_snapshot, with_facts
from fastapi.testclient import TestClient
from scripted import ScriptedChatModel, solve

from app.commands.approval import ApproveRequest, WaiveRequest, approve_and_commit, waive
from app.commands.task_request import (
    TaskRequestForm,
    TaskWithdraw,
    submit_task_request,
    withdraw_task_request,
)
from app.coordinator import transitions
from app.coordinator.dispatcher import run_until_idle
from app.domain.calendar import (
    fits_work_interval,
    has_work_slot,
    start_domain,
    work_delay,
    work_minutes,
)
from app.main import app
from app.packs.loader import pack_dir
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks
from scripts import verify_demo_values as verify

SITE = "YARD-01"
CAL = ((0, 480), (1440, 1920), (2880, 3360))
FORM_FIELDS = (
    "task_id",
    "work_type",
    "zone_id",
    "duration",
    "earliest_start",
    "latest_start",
    "latest_end",
    "required_resource_type",
    "requested_resource_id",
)

# D 표: 충돌, acting unit, L0 결과 (상태, 변경 수, 달력 지연, 근무 지연, 새 시작, 자원)
EXPECTED = {
    "N1": ([("CAP-RESOURCE", ("K", "N1"))], "UA", ("OPTIMAL", 1, 60, 60, 1560, "SITE-GC-01")),
    "N2": ([("SEP-HOT-FLAM", ("N2", "P"))], "UA", ("OPTIMAL", 1, 45, 45, 1575, None)),
    "N3": (
        [("CAP-RESOURCE", ("N3", "Q")), ("SEP-LIFT-BELOW", ("M", "N3"))],
        "UB",
        ("OPTIMAL", 1, 120, 120, 3000, "SITE-GC-01"),
    ),
    "N4": ([("SEP-HOT-FLAM", ("N4", "W"))], "UA", ("OPTIMAL", 1, 1140, 180, 2880, None)),
    "N5": ([("SEP-LIFT-BELOW", ("K", "N5"))], "UB", ("INFEASIBLE", None, None, None, None, None)),
}


def _key():
    return uuid.uuid4().hex


def _demo(pack, task_id):
    return next(d for d in pack.demo_requests if d.task_id == task_id)


def _submit(pack, task_id):
    d = _demo(pack, task_id)
    form = TaskRequestForm(**{k: getattr(d, k) for k in FORM_FIELDS})
    out = submit_task_request(pack, d.requester, _key(), form)
    assert out.status == "APPLIED", out.reason_codes
    return d


def _site(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id)


def _runs():
    with db.read() as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")]
        return [get_run(conn, rid) for rid in ids]


def _factory(*replies):
    return lambda: ScriptedChatModel(list(replies))


# ── 근무 달력 순수 함수 ────────────────────────────────────────


def test_calendar_functions():
    assert fits_work_interval(450, 480, CAL) and not fits_work_interval(470, 500, CAL)
    assert not fits_work_interval(480, 1440, CAL)
    assert start_domain(120, CAL) == [[0, 360], [1440, 1800], [2880, 3240]]
    assert start_domain(500, CAL) == []
    assert has_work_slot(1740, 3060, 3180, 120, CAL)  # N4: 당일은 없고 다음 날 있음
    assert not has_work_slot(500, 1400, 1500, 30, CAL)  # 밤에만 걸친 시간창
    assert not has_work_slot(0, 0, 100, 30, ((10, 480),))
    assert work_minutes(1740, 2880, CAL) == 180
    # N4: 10/13 14:00 → 10/14 09:00 = 달력 1140분, 근무 180분 (A.20)
    assert (max(0, 2880 - 1740), work_delay(1740, 2880, CAL)) == (1140, 180)
    assert work_delay(2880, 1740, CAL) == 0  # 앞당김은 지연 0
    assert work_delay(1500, 1560, CAL) == 60


# ── 확장 fixture에서 §15 값과 R0 ───────────────────────────────


def test_extended_r0_has_no_conflict(seeded):
    snap = take_snapshot(seeded)
    assert snap.facts().work_intervals == CAL
    assert detect_conflicts(snap, snap.facts().check_assignments(), seeded) == []


def test_section15_values_on_extended_fixture(with_a):
    """L0 INFEASIBLE, Alpha 변경 2·지연 90, Beta 변경 1·지연 60 (test_solver와 같은 값, 확장 fixture)."""
    snap = take_snapshot(with_a)
    conflict = detect_conflicts(snap, snap.facts().check_assignments(), with_a)[0]
    l0 = cpsat.solve(snap, build_search_spec(snap, conflict, "UA", "L0"), with_a)
    assert l0.stage1["status"] == "INFEASIBLE"
    l1 = cpsat.solve(snap, build_search_spec(snap, conflict, "UA", "L1"), with_a)
    assert (l1.stage1["changed"], l1.stage2["delay"]) == (2, 90)
    placed = {a["task_id"]: (a["start"], a["resource_id"]) for a in l1.solution}
    assert (placed["A"], placed["C"]) == ((60, "A-CR-01"), (90, "A-CR-01"))
    beta_facts = snap.facts().model_copy(
        update={
            "tasks": tuple(
                t.model_copy(update={"movable": t.movable.model_copy(update={"resource": True})})
                if t.task_id == "A"
                else t
                for t in snap.facts().tasks
            )
        }
    )
    beta = with_facts(snap, tasks=beta_facts.tasks)
    spec = build_search_spec(beta, conflict, "UA", "L0", {"A": ["SITE-CR-01"]})
    r = cpsat.solve(beta, spec, with_a)
    assert (r.stage1["changed"], r.stage2["delay"]) == (1, 60)
    assert {a["task_id"]: (a["start"], a["resource_id"]) for a in r.solution}["A"] == (
        60,
        "SITE-CR-01",
    )


@pytest.mark.parametrize(("new_es", "start", "delay"), [(60, 60, 15), (75, 75, 30)])
def test_gamma_delta_expected_values(new_es, start, delay):
    """Scene 4 사실 수정: R1(Beta) + E earliest_start → Gamma(10:00)·Delta(10:15), 변경 1 (A.25)."""
    w, fixture, a, _ = verify.load(pack_dir("shipyard"))
    r1 = verify.r1_world(w, fixture, a)
    assert {t.id: (t.start, t.res) for t in r1 if t.id in ("A", "C")} == {
        "A": (60, "SITE-CR-01"),
        "C": (60, "A-CR-01"),
    }
    r = verify.expected_fact(w, r1, "E", new_es)["L0"]
    assert r["conflict"] == ("WINDOW", ("E",))
    assert (r["status"], r["changed"], r["delay"], r["work_delay"]) == ("OPTIMAL", 1, delay, delay)
    assert r["moved"] == {"E": [start, None]}


# ── D 표: N1–N5 (요청 하나씩, R0 기준) ─────────────────────────


@pytest.mark.parametrize("task_id", sorted(EXPECTED))
def test_demo_request_expected_values(seeded, task_id):
    pack = seeded
    d = _submit(pack, task_id)
    conflicts_exp, unit_exp, (status, changed, delay, wdelay, start, res) = EXPECTED[task_id]
    snap = take_snapshot(pack)
    facts = snap.facts()
    conflicts = detect_conflicts(snap, facts.check_assignments(), pack)
    assert [(c.rule_id, c.task_ids) for c in conflicts] == conflicts_exp
    cause = {"kind": "FORM", "task_id": task_id, "actor_id": d.requester}
    unit, primary = transitions.choose_acting(facts, conflicts, cause)
    assert (unit, (primary.rule_id, primary.task_ids)) == (unit_exp, conflicts_exp[0])
    base = facts.base_assignments()[task_id].start
    # 상대 작업이 모두 다른 Unit의 고정 작업이라 L1·L2로 넓혀도 같은 결과다
    for level in ("L0", "L1", "L2"):
        r = cpsat.solve(snap, build_search_spec(snap, primary, unit, level), pack)
        assert r.stage1["status"] == status, level
        if status != "OPTIMAL":
            assert r.solution is None
            continue
        placed = {a["task_id"]: a for a in r.solution}
        assert (r.stage1["changed"], r.stage2["delay"]) == (changed, delay), level
        assert (placed[task_id]["start"], placed[task_id]["resource_id"]) == (start, res)
        assert work_delay(base, placed[task_id]["start"], facts.work_intervals) == wdelay
        moved = [a["task_id"] for a in r.solution if a["start"] != base_of(facts, a["task_id"])]
        assert moved == [task_id], level


def base_of(facts, task_id):
    return facts.base_assignments()[task_id].start


def test_n4_without_calendar_would_be_night(seeded):
    """달력이 없으면 W 종료 + 15분 = 10/13 17:15 야간 배치. 달력이 다음 날 09:00으로 민다."""
    _submit(seeded, "N4")
    snap = take_snapshot(seeded)
    no_cal = with_facts(snap, work_intervals=((0, 3360),))
    conflict = detect_conflicts(no_cal, no_cal.facts().check_assignments(), seeded)[0]
    r = cpsat.solve(no_cal, build_search_spec(no_cal, conflict, "UA", "L0"), seeded)
    assert {a["task_id"]: a["start"] for a in r.solution}["N4"] == 1935


def test_cpsat_fixed_task_outside_calendar_infeasible(with_a):
    """고정 작업이 달력을 어기면 INFEASIBLE (Rule Engine CALENDAR와 같은 판정, A.12·A.20)."""
    snap = take_snapshot(with_a)
    shifted = with_facts(snap, work_intervals=((30, 480), (1440, 1920), (2880, 3360)))
    found = detect_conflicts(shifted, shifted.facts().check_assignments(), with_a)
    assert {("CALENDAR", ("B",)), ("CALENDAR", ("D",))} <= {(c.rule_id, c.task_ids) for c in found}
    conflict = next(c for c in found if c.rule_id == "SEP-LIFT-BELOW")
    r = cpsat.solve(shifted, build_search_spec(shifted, conflict, "UA", "L1"), with_a)
    assert r.stage1["status"] == "INFEASIBLE"


# ── 시스템 경로: 하나씩 확정 (A 장면 → N1–N4) ──────────────────


def _approve_pending(pack, waive_tasks=()):
    with db.read() as conn:
        [run] = [r for r in _runs() if r.status == "WAITING_HUMAN"]
        cand_id = run.wait_ref
        [v] = list_validations(conn, pack.site_id, cand_id)
    assert v.status == "PASS"
    if waive_tasks:
        body = WaiveRequest(candidate_id=cand_id, task_ids=waive_tasks, comment="시연 수용")
        assert waive(pack, "supervisor", _key(), body).status == "APPLIED"
    approve = ApproveRequest(
        candidate_id=cand_id,
        validation_id=v.validation_id,
        expected_context_version=_site(pack).context_version,
    )
    out = approve_and_commit(pack, "supervisor", _key(), approve)
    assert out.status == "APPLIED", out.reason_codes
    return cand_id


def test_sequence_alpha_then_n1_to_n4(seeded):
    """앞 요청을 확정한 상태에서도 D 표 값이 같다. 이동이 Consent 범위 안이라 WAIVE 없이 승인된다."""
    pack = seeded
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a))
    run_until_idle(pack, model_factory=_factory(solve("L0"), solve("L1")))
    _approve_pending(pack, waive_tasks=("C",))  # Alpha → R1
    for task_id in ("N1", "N2", "N3", "N4"):
        _submit(pack, task_id)
        run_until_idle(pack, model_factory=_factory(solve("L0", "상대가 다른 Unit 고정 작업")))
        start = EXPECTED[task_id][2][4]
        with TestClient(app) as client:
            state = client.get(f"/api/sites/{SITE}/state", headers={"X-Actor": "supervisor"})
        cand = next(c for c in state.json()["candidates"] if c["display_status"] == "OPEN")
        assert cand["consultation"]["status"] == "COMPLETE"
        [change] = cand["changes"]
        assert (change["task_id"], change["after"]["start"]) == (task_id, start)
        _approve_pending(pack)
    assert _site(pack).plan_revision == 5
    snap = take_snapshot(pack)
    assert detect_conflicts(snap, snap.facts().check_assignments(), pack) == []


# ── 근무 분 지연: state의 changes와 Solver 요약 ────────────────


def test_state_reports_calendar_and_work_delay(seeded):
    _submit(seeded, "N4")
    run_until_idle(seeded, model_factory=_factory(solve("L0")))
    with TestClient(app) as client:
        res = client.get(f"/api/sites/{SITE}/state", headers={"X-Actor": "supervisor"})
    [cand] = res.json()["candidates"]
    [change] = cand["changes"]
    assert (change["task_id"], change["before"]["start"], change["after"]["start"]) == (
        "N4",
        1740,
        2880,
    )
    assert (change["delay"], change["work_delay"]) == (1140, 180)
    assert (cand["solver"]["stage2"]["delay"], cand["solver"]["stage2"]["work_delay"]) == (
        1140,
        180,
    )


# ── 폼: 근무시간 밖 ────────────────────────────────────────────


def _form(pack, task_id, **changes):
    d = _demo(pack, task_id)
    return TaskRequestForm(**{**{k: getattr(d, k) for k in FORM_FIELDS}, **changes})


def test_form_rejects_window_without_work_slot(seeded):
    # 10/12 17:00–10/13 08:00에만 시작 가능한 시간창: 근무 구간 안 시작이 없다
    form = _form(seeded, "N2", earliest_start=480, latest_start=1380, latest_end=1440)
    out = submit_task_request(seeded, "planner_a", _key(), form)
    assert (out.status, out.reason_codes) == ("REJECTED", ("WINDOW_OUTSIDE_WORK_HOURS",))


def test_form_accepts_night_start_and_replans_calendar(seeded):
    """요청 시작만 근무시간 밖이면 접수하고 CALENDAR 충돌로 재계획한다 (A.20)."""
    form = _form(seeded, "N2", earliest_start=1380, latest_start=1620, latest_end=1680)
    assert submit_task_request(seeded, "planner_a", _key(), form).status == "APPLIED"
    snap = take_snapshot(seeded)
    found = detect_conflicts(snap, snap.facts().check_assignments(), seeded)
    assert ("CALENDAR", ("N2",)) in [(c.rule_id, c.task_ids) for c in found]
    run_until_idle(seeded, model_factory=_factory(solve("L0")))
    [run] = _runs()
    assert run.status == "WAITING_HUMAN"
    with db.read() as conn:
        sol = conn.execute(
            "SELECT assignments FROM candidate WHERE candidate_id = ?", (run.wait_ref,)
        ).fetchone()[0]
    assert '"task_id":"N2"' in sol.replace(" ", "")


# ── F-2: 철회 ──────────────────────────────────────────────────


def test_withdraw_unblocks_later_requests(seeded):
    """N5 이관 → N1이 INFEASIBLE(N5가 고정 상수로 남음) → N5 철회 → N1이 정상 해결."""
    pack = seeded
    _submit(pack, "N5")
    # 이관은 열린 조회·질문이 모두 닫힌 뒤에만(A.30): 그 뒤는 조회·질문(거절)을 거쳐 이관한다
    model = ClosingModel([solve("L0"), solve("L2")])
    run_until_idle(pack, model_factory=lambda: model)
    close_to_escalation(pack, _runs()[0].run_id, model)
    [n5_run] = _runs()
    assert (n5_run.status, n5_run.acting_unit_id) == ("ESCALATED", "UB")

    _submit(pack, "N1")
    model = ClosingModel([solve("L0"), solve("L2")])
    run_until_idle(pack, model_factory=lambda: model)
    close_to_escalation(pack, _runs()[-1].run_id, model)
    n1_run = _runs()[-1]
    assert n1_run.status == "ESCALATED"
    with db.read() as conn:
        statuses = [
            r[0]
            for r in conn.execute(
                "SELECT json_extract(r.stage1, '$.status') FROM solver_result r"
                " JOIN solver_job j ON j.solver_result_id = r.solver_result_id"
                " WHERE j.run_id = ?",
                (n1_run.run_id,),
            )
        ]
    assert statuses == ["INFEASIBLE", "INFEASIBLE"]

    out = withdraw_task_request(pack, "planner_b", _key(), TaskWithdraw(task_id="N5"))
    assert out.status == "APPLIED"
    assert out.result_refs == {"task_id": "N5", "revision": 2, "queued": False}
    with db.read() as conn:
        n5 = next(t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == "N5")
        audit = conn.execute("SELECT command FROM audit ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert (n5.revision, n5.lifecycle, audit) == (2, "NEEDS_INFO", "WITHDRAW_TASK_REQUEST")

    # 철회 뒤 RECHECK: 남은 요청 N1의 Unit(UA)이 재계획한다 (cause N5는 충돌에 없다)
    run_until_idle(pack, model_factory=_factory(solve("L0")))
    run = _runs()[-1]
    assert (run.status, run.acting_unit_id, run.input_ref["cause"]["kind"]) == (
        "WAITING_HUMAN",
        "UA",
        "WITHDRAW",
    )
    with db.read() as conn:
        asg = conn.execute(
            "SELECT assignments FROM candidate WHERE candidate_id = ?", (run.wait_ref,)
        ).fetchone()[0]
    assert '{"end":1620,"resource_id":"SITE-GC-01","start":1560,"task_id":"N1"}' in asg.replace(
        " ", ""
    )


def test_withdraw_stales_open_run(seeded):
    _submit(seeded, "N1")
    run_until_idle(seeded, model_factory=_factory(solve("L0")))
    [run] = _runs()
    assert run.status == "WAITING_HUMAN"
    out = withdraw_task_request(seeded, "planner_a", _key(), TaskWithdraw(task_id="N1"))
    assert out.status == "APPLIED"
    [stale] = _runs()
    assert (stale.status, stale.end_reason) == ("STALE", "WITHDRAW:N1")


def test_withdraw_permissions_and_targets(seeded):
    pack = seeded
    _submit(pack, "N5")  # 담당 planner_b

    def withdraw(actor, task_id):
        out = withdraw_task_request(pack, actor, _key(), TaskWithdraw(task_id=task_id))
        return out.status, out.reason_codes

    assert withdraw("planner_a", "N5") == ("REJECTED", ("NOT_AUTHORIZED",))
    assert withdraw("reporter", "N5") == ("REJECTED", ("NOT_AUTHORIZED",))
    assert withdraw("planner_b", "B") == ("REJECTED", ("TASK_IN_PLAN",))  # Plan에 있는 작업
    assert withdraw("supervisor", "X9") == ("REJECTED", ("TASK_NOT_FOUND",))
    ctx = _site(pack).context_version
    assert withdraw("supervisor", "N5") == ("APPLIED", ())
    assert _site(pack).context_version == ctx + 1
    assert withdraw("supervisor", "N5") == ("REJECTED", ("TASK_NOT_FOUND",))  # 이미 철회


# ── 선행 작업 참조 (부록 A.22) ─────────────────────────────────


def test_form_rejects_withdrawn_predecessor(seeded):
    """철회된 작업(NEEDS_INFO)은 선행으로 지정할 수 없다. 다른 사유와 함께 모은다."""
    pack = seeded
    _submit(pack, "N1")
    withdraw = withdraw_task_request(pack, "planner_a", _key(), TaskWithdraw(task_id="N1"))
    assert withdraw.status == "APPLIED"
    form = _form(pack, "N2", predecessors=({"task_id": "N1"},), zone_id="Z9")
    out = submit_task_request(pack, "planner_a", _key(), form)
    assert (out.status, set(out.reason_codes)) == (
        "REJECTED",
        {"PREDECESSOR_NOT_FOUND", "UNKNOWN_ZONE"},
    )
    # 현재 READY 작업은 선행으로 지정할 수 있다
    form = _form(pack, "N2", predecessors=({"task_id": "B"},))
    assert submit_task_request(pack, "planner_a", _key(), form).status == "APPLIED"


def test_withdraw_rejects_task_with_successors(seeded):
    """후속 요청이 있으면 TASK_HAS_SUCCESSORS(단독, 권한 검사 다음). 후속을 먼저 철회하면 된다."""
    pack = seeded
    _submit(pack, "N1")
    form = _form(pack, "N2", predecessors=({"task_id": "N1"},))
    assert submit_task_request(pack, "planner_a", _key(), form).status == "APPLIED"

    def withdraw(actor, task_id):
        out = withdraw_task_request(pack, actor, _key(), TaskWithdraw(task_id=task_id))
        return out.status, out.reason_codes

    assert withdraw("planner_b", "N1") == ("REJECTED", ("NOT_AUTHORIZED",))
    ctx = _site(pack).context_version
    assert withdraw("planner_a", "N1") == ("REJECTED", ("TASK_HAS_SUCCESSORS",))
    assert _site(pack).context_version == ctx
    assert withdraw("planner_a", "N2") == ("APPLIED", ())
    assert withdraw("planner_a", "N1") == ("APPLIED", ())


def test_withdraw_rejects_task_with_queued_successor(seeded):
    """대기열(QUEUED) 후속 요청도 후속 작업으로 본다."""
    pack = seeded
    _submit(pack, "N1")
    run_until_idle(pack, model_factory=_factory(solve("L0")))
    assert _runs()[-1].status == "WAITING_HUMAN"  # 열린 Case → 새 요청은 대기열
    form = _form(pack, "N2", predecessors=({"task_id": "N1"},))
    out = submit_task_request(pack, "planner_a", _key(), form)
    assert out.result_refs["queued"] is True
    out = withdraw_task_request(pack, "planner_a", _key(), TaskWithdraw(task_id="N1"))
    assert out.reason_codes == ("TASK_HAS_SUCCESSORS",)


# ── API: meta·scenario·withdraw ────────────────────────────────


@pytest.fixture
def client(seeded):
    with TestClient(app) as c:
        yield c


def test_meta_api(client, seeded):
    res = client.get(f"/api/sites/{SITE}/meta", headers={"X-Actor": "planner_a"})
    assert res.status_code == 200, res.text
    meta = res.json()
    assert meta["work_types"]["LIFTING"] == {
        "display_name": "인양",
        "hazard_tags": ["LIFTING"],
        "critical_fields": ["zone_id", "duration", "window", "resource"],
    }
    assert {r["rule_id"]: (r["type"], r["display_name"]) for r in meta["rules"]} == {
        "SEP-LIFT-BELOW": ("SEPARATION", "인양–하부 작업 분리"),
        "SEP-HOT-FLAM": ("SEPARATION", "화기–인화성 작업 분리(15분)"),
        "CAP-RESOURCE": ("CAPACITY", "자원 중복 배정 금지"),
    }
    assert (meta["timezone"], meta["horizon_start_utc"], meta["horizon_minutes"]) == (
        "Asia/Seoul",
        "2026-10-12T00:00:00Z",
        3360,
    )
    assert meta["work_intervals"] == [[0, 480], [1440, 1920], [2880, 3360]]
    assert meta["zones"] == ["B", "C", "D", "D2", "F", "G", "G2", "H"]
    assert ["G", "G2", "ADJACENT"] in [
        [r["zone_a"], r["zone_b"], r["relation"]] for r in meta["zone_relations"]
    ]
    gc = next(r for r in meta["resources"] if r["resource_id"] == "SITE-GC-01")
    assert (gc["resource_type"], gc["allowed_unit_ids"]) == ("GANTRY", ["UA", "UB"])
    assert client.get(f"/api/sites/{SITE}/meta").status_code == 401
    assert client.get("/api/sites/NOPE/meta", headers={"X-Actor": "planner_a"}).status_code == 404


def test_dev_scenario_api(client, seeded):
    res = client.get("/api/dev/scenario", headers={"X-Actor": "supervisor"})
    assert res.status_code == 200, res.text
    body = res.json()
    requests = body["task_requests"]
    assert [(r["form"]["task_id"], r["requester"]) for r in requests] == [
        ("A", "planner_a"),
        ("N1", "planner_a"),
        ("N2", "planner_a"),
        ("N3", "planner_b"),
        ("N4", "planner_a"),
        ("N5", "planner_b"),
    ]
    assert requests[0]["form"] == {
        "task_id": "A",
        "work_type": "LIFTING",
        "zone_id": "B",
        "duration": 30,
        "earliest_start": 0,
        "latest_start": 60,
        "latest_end": 90,
        "required_resource_type": "CRANE",
        "requested_resource_id": "A-CR-01",
    }
    assert all(r["label"] for r in requests)
    assert body["event_reports"][0]["body"] == {
        "event_type": "DELAY",
        "text": "도장 준비 15분 늦어져 10시부터",
        "target_task_id": None,
    }
    # 폼 본문 그대로 보낼 수 있다
    n1 = requests[1]
    res = client.post(
        f"/api/sites/{SITE}/task-requests",
        json=n1["form"],
        headers={"X-Actor": n1["requester"], "Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text


def test_dev_scenario_requires_demo_mode(seeded, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    with TestClient(app) as c:
        res = c.get("/api/dev/scenario", headers={"X-Actor": "supervisor"})
    assert res.status_code == 404


def test_withdraw_api(client, seeded):
    _submit(seeded, "N5")

    def post(actor, task_id):
        return client.post(
            f"/api/tasks/{task_id}/withdraw",
            json={"comment": "시연 철회"},
            headers={"X-Actor": actor, "Idempotency-Key": _key()},
        )

    assert post("planner_a", "N5").status_code == 403
    res = post("planner_b", "B")
    assert (res.status_code, res.json()["reason_codes"]) == (409, ["TASK_IN_PLAN"])
    assert post("planner_b", "X9").status_code == 404
    res = post("planner_b", "N5")
    assert res.status_code == 200 and res.json()["result_refs"]["revision"] == 2


# ── 2차: 현장 목록·거절 시연값 (A.20 2차) ──────────────────────


def test_sites_api_without_actor(client, seeded):
    res = client.get("/api/sites")
    assert res.status_code == 200, res.text
    [site] = res.json()["sites"]
    assert (site["site_id"], site["pack"]) == (seeded.site_id, seeded.name)
    supervisors = [a["actor_id"] for a in site["actors"] if "SUPERVISOR" in a["roles"]]
    assert supervisors == ["supervisor"]


def test_dev_scenario_rejections(client, seeded):
    res = client.get("/api/dev/scenario", headers={"X-Actor": "supervisor"})
    [rej] = res.json()["rejections"]
    assert rej["body"] == {
        "reason_code": "TASK_IMMOVABLE",
        "target_task_ids": ["C"],
        "axes": ["TIME", "RESOURCE"],
        "comment": "작업발판 연계 공정 확정",
    }


def test_demo_rejection_target_must_exist(pack_copy):
    import yaml

    from app.packs.loader import PackError, load_pack

    f = pack_copy / "scenario.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    data["demo_rejections"][0]["target_task_ids"] = ["X9"]
    f.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(PackError, match="undefined task 'X9'"):
        load_pack(pack_copy)


def test_steps_api_reports_work_delay_without_storing(client, seeded):
    """GET /runs/{rid}/steps의 SOLVE step에 근무 분 지연을 조회 시 붙인다. AgentStep에는 없다 (A.20)."""
    _submit(seeded, "N4")
    run_until_idle(seeded, model_factory=_factory(solve("L0")))
    [run] = _runs()
    res = client.get(f"/api/runs/{run.run_id}/steps", headers={"X-Actor": "supervisor"})
    assert res.status_code == 200, res.text
    [step] = res.json()
    assert step["tool_result"]["stage2"] == {"status": "OPTIMAL", "delay": 1140, "work_delay": 180}
    with db.read() as conn:
        stored = conn.execute(
            "SELECT tool_result FROM agent_step WHERE run_id = ?", (run.run_id,)
        ).fetchone()[0]
    assert "work_delay" not in stored
