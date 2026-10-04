"""재계획은 주체 Unit 없이 현장의 충돌 전체를 한 번에 푼다 (AG-24).

범위는 Unit을 가리지 않는다: 고정되지 않은 작업은 누구 작업이든 움직일 수 있다. 다른 사람의 작업을 담당자
확인 없이 바꾸지 않는 것(안전 원칙 8)은 협의 항목과 승인 조건이 지킨다. Pack 그대로의 R0(고정 없음)에서 본다.
"""

import uuid

from conftest import add_task, choose, make_task, take_snapshot, with_facts
from scripted import Router, solve

from app.commands.approval import ApproveRequest, approve_and_commit
from app.commands.messages import ReplyRequest, reply_message
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.consultation import build_items
from app.domain.groups import conflict_groups
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.consultations import consultation_view, list_review_queue
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import get_run
from app.store.repos.site import get_site
from app.validator.validator import validate

GANTRY = {"zone_id": "F", "required_resource_type": "GANTRY", "requested_resource_id": "SITE-GC-01"}


def _key():
    return uuid.uuid4().hex


def _conflicts(pack, snap):
    return detect_conflicts(snap, snap.facts().check_assignments(), pack)


def _moved(snap, result):
    base = snap.facts().base_assignments()
    return sorted(
        a["task_id"]
        for a in result.solution
        if (a["start"], a["resource_id"])
        != (base[a["task_id"]].start, base[a["task_id"]].resource_id)
    )


def _narrow_k(task):
    """K의 가능 범위를 10/13 09:00–10:00 시작으로 좁힌다(확인 기록도 같은 값으로)."""
    if task.task_id != "K":
        return task
    window = {"earliest_start": 1440, "latest_start": 1500, "latest_end": 1620}
    fields = dict(task.fields)
    fields["window"] = fields["window"].model_copy(update={"value": window})
    return task.model_copy(update={**window, "fields": fields})


def test_one_search_moves_tasks_of_two_units(seeded_real):
    """UA의 X가 제자리를 지키면 UB의 K가 밀리고, 밀린 K 때문에 UA의 Z도 밀려야 풀린다.
    한 Unit의 작업만 움직여서는 어느 범위로도 풀리지 않는 장면이다."""
    pack = seeded_real
    # X: 10/13 09:00–10:00만 가능. Z: 10/13 11:00부터 가능한 60분 작업(기준 11:00)
    x = make_task(
        pack,
        task_id="X",
        duration=60,
        earliest_start=1440,
        latest_start=1440,
        latest_end=1500,
        **GANTRY,
    )
    z = make_task(
        pack,
        task_id="Z",
        duration=60,
        earliest_start=1560,
        latest_start=1800,
        latest_end=1860,
        **GANTRY,
    )
    add_task(pack, x)
    add_task(pack, z)
    snap = take_snapshot(pack)
    # K(UB, 09:00–11:00)는 그날 10:00까지만 시작을 늦출 수 있다
    snap = with_facts(snap, tasks=tuple(_narrow_k(t) for t in snap.facts().tasks))
    facts = snap.facts()
    units = {t.task_id: t.unit_id for t in facts.tasks}
    assert (units["X"], units["K"], units["Z"]) == ("UA", "UB", "UA")
    conflicts = _conflicts(pack, snap)
    assert [(c.rule_id, c.task_ids) for c in conflicts] == [("CAP-RESOURCE", ("K", "X"))]

    # 충돌 당사자만으로는 해가 없다: K가 갈 자리를 Z가 막고 있다
    l0 = build_search_spec(snap, conflicts, "L0")
    assert cpsat.solve(snap, l0, pack).stage1["status"] == "INFEASIBLE"
    # 같은 자원의 작업까지 넓히면 두 Unit의 작업이 같이 움직인다
    l1 = build_search_spec(snap, conflicts, "L1")
    result = cpsat.solve(snap, l1, pack)
    assert result.stage1["status"] == "OPTIMAL"
    assert _moved(snap, result) == ["K", "Z"]
    assert {units[t] for t in _moved(snap, result)} == {"UA", "UB"}
    candidate = build_candidate(snap, l1, result)
    assert validate(snap, candidate, l1, pack).status == "PASS"
    # 바뀐 작업마다 그 작업의 담당자에게 협의 항목이 생긴다
    owners = {i.task_id: (i.owner_actor_id, i.base_status) for i in build_items(facts, candidate)}
    assert owners == {"K": ("planner_b", "PENDING"), "Z": ("planner_a", "PENDING")}


def test_one_search_resolves_two_conflict_groups(seeded_real):
    """엮이지 않은 충돌 그룹이 둘이면 한 그룹만 푸는 탐색은 다른 그룹의 충돌이 상수로 남아 해가 없다.
    충돌 전체를 범위로 잡으면 한 번에 풀린다."""
    pack = seeded_real
    # 그룹 1: X(UA)–K(UB), 10/13 같은 장비·같은 시간. 그룹 2: Y(UB)–Q(UA), 10/14 같은 장비·같은 시간
    add_task(
        pack,
        make_task(
            pack,
            task_id="X",
            duration=60,
            earliest_start=1500,
            latest_start=1740,
            latest_end=1800,
            **GANTRY,
        ),
    )
    add_task(
        pack,
        make_task(
            pack,
            task_id="Y",
            duration=60,
            earliest_start=2910,
            latest_start=3240,
            latest_end=3300,
            unit_id="UB",
            owner_actor_id="planner_b",
            **GANTRY,
        ),
    )
    snap = take_snapshot(pack)
    conflicts = _conflicts(pack, snap)
    groups = conflict_groups(conflicts)
    assert [g.task_ids for g in groups] == [("K", "X"), ("Q", "Y")]

    one = build_search_spec(snap, list(groups[0].conflicts), "L0")
    assert cpsat.solve(snap, one, pack).stage1["status"] == "INFEASIBLE"

    spec = build_search_spec(snap, conflicts, "L0")
    assert list(spec.axes) == ["K", "Q", "X", "Y"]
    result = cpsat.solve(snap, spec, pack)
    assert (result.stage1["status"], result.stage1["changed"]) == ("OPTIMAL", 2)
    candidate = build_candidate(snap, spec, result)
    assert validate(snap, candidate, spec, pack).status == "PASS"
    assert detect_conflicts(snap, candidate.assignments, pack) == []


def test_other_units_changed_task_needs_its_owners_answer(seeded_real, main_on):
    """UA의 요청이 제자리를 지켜야 해서 UB의 K가 움직이는 안: K의 변경은 K 담당자의 협의 항목이 되고,
    그 사람의 답 없이는 승인되지 않는다. 요청자의 답은 효력이 없다."""
    pack = seeded_real
    form = TaskRequestForm(
        task_id="N1",
        work_type="LIFTING",
        duration=60,
        earliest_start=1500,  # 10/13 10:00에만 시작할 수 있다
        latest_start=1500,
        latest_end=1560,
        **GANTRY,
    )
    assert submit_task_request(pack, "planner_a", _key(), form).status == "APPLIED"
    router = Router(replanning=[solve("L0")])
    run_until_idle(pack, model_factory=router.factory())

    with db.read() as conn:
        [cand_id] = list_review_queue(conn, pack.site_id)
        candidate = get_candidate(conn, pack.site_id, cand_id)
        view = consultation_view(conn, pack.site_id, cand_id)
        [run_id] = [
            r[0]
            for r in conn.execute("SELECT run_id FROM agent_run WHERE agent_type = 'REPLANNING'")
        ]
        run = get_run(conn, run_id)
    assert (run.acting_unit_id, run.acting_actor_id) == (None, None)
    placed = {a.task_id: a.start for a in candidate.assignments}
    assert placed["N1"] == 1500 and placed["K"] != 1440  # 요청자(UA)가 아니라 UB의 K가 움직였다
    assert [(i.task_id, i.owner_actor_id) for i in view.items] == [("K", "planner_b")]
    assert view.item_status == {"K": "PENDING"}

    def approve():
        with db.read() as conn:
            validation = list_validations(conn, pack.site_id, cand_id)[-1]
            site = get_site(conn, pack.site_id)
        body = ApproveRequest(
            candidate_id=cand_id,
            validation_id=validation.validation_id,
            expected_context_version=site.context_version,
        )
        return approve_and_commit(pack, "supervisor", _key(), body)

    assert approve().reason_codes == ("CONSULTATION_INCOMPLETE",)
    # Supervisor가 고르면 협의가 K의 담당자에게 간다
    choose(pack, cand_id)
    run_until_idle(pack, model_factory=router.factory())
    with db.read() as conn:
        [(message_id, to_actor)] = conn.execute(
            "SELECT message_id, to_actor_id FROM message WHERE type = 'CHANGE_REQUEST'"
        ).fetchall()
    assert to_actor == "planner_b"
    # 지정된 사람의 답만 효력이 있다
    by_requester = reply_message(
        pack, "planner_a", _key(), ReplyRequest(message_id=message_id, decision="ACCEPT")
    )
    assert by_requester.reason_codes == ("NOT_AUTHORIZED",)
    assert approve().reason_codes == ("CONSULTATION_INCOMPLETE",)
    by_owner = reply_message(
        pack, "planner_b", _key(), ReplyRequest(message_id=message_id, decision="ACCEPT")
    )
    assert by_owner.status == "APPLIED"
    assert approve().status == "APPLIED"
