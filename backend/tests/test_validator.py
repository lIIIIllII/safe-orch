"""Independent Validator. Scene 5와 T03–T07·T09·T29·T31."""

import ast
import inspect
import json
import sqlite3

import pytest
from conftest import add_task, free_alternative, make_task, take_snapshot, with_facts

from app.domain.hashes import candidate_hash
from app.domain.models import (
    Assignment,
    Candidate,
    Conflict,
    FieldRecord,
    Pin,
    Predecessor,
    Requirement,
)
from app.packs.loader import EVALUATORS, load_pack
from app.rules import engine
from app.rules.engine import BASIC_RULE_IDS, detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.records import (
    insert_search_spec,
    insert_validation,
    register_solver_outcome,
)
from app.validator import validator as validator_module
from app.validator.validator import BASIC_TO_CHECK, RULE_TYPE_TO_CHECK, validate

# ── 도우미 ─────────────────────────────────────────────────────


def _solve(pack, snap, level, conflict=None):
    if conflict is None:
        conflict = detect_conflicts(snap, snap.facts().check_assignments(), pack)[0]
    spec = build_search_spec(snap, conflict, "UA", level)
    cand = build_candidate(snap, spec, cpsat.solve(snap, spec, pack))
    assert cand is not None
    return spec, cand


def _reshape(cand, snap, *assignments, drop=(), extra=()):
    """배정만 바꾸고 candidate_hash는 다시 계산한 후보 (Scene 5 주입과 같은 방식)."""
    by_id = {a.task_id: a for a in cand.assignments}
    by_id.update({a.task_id: a for a in assignments})
    new = tuple(a for tid, a in sorted(by_id.items()) if tid not in drop) + tuple(extra)
    return _rehash(cand.model_copy(update={"assignments": new}), snap)


def _rehash(cand, snap):
    h = candidate_hash(
        cand.assignments,
        cand.base_plan_revision,
        cand.context_version,
        snap.snapshot_hash,
        cand.search_spec_hash,
        cand.pack_hash,
    )
    return cand.model_copy(update={"candidate_hash": h})


def _reconfirm(snap, assignments=None):
    facts = snap.facts()
    asg = tuple(facts.base_assignments().values()) if assignments is None else assignments
    return _rehash(
        Candidate(
            candidate_id="cand_reconfirm",
            snapshot_id=snap.snapshot_id,
            search_spec_id=None,
            search_spec_hash=None,
            solver_result_id=None,
            base_plan_revision=facts.plan_revision,
            context_version=facts.context_version,
            pack_hash=facts.pack_hash,
            assignments=asg,
            candidate_hash="",
            kind="RECONFIRM",
        ),
        snap,
    )


def _asg(task_id, start, end, resource_id=None):
    return Assignment(task_id=task_id, start=start, end=end, resource_id=resource_id)


def _bad(validation):
    """PASS가 아닌 check의 (check_id, reason_code, task_ids)."""
    return [
        (c.check_id, c.reason_code, c.task_ids) for c in validation.checks if c.status != "PASS"
    ]


def _failed_checks(validation):
    return sorted({c.check_id for c in validation.checks if c.status != "PASS"})


def _pin(snap, task_id):
    pin = Pin(
        pin_id=f"pin_{task_id}", task_id=task_id, pinned_by="supervisor", by_role="SUPERVISOR"
    )
    return with_facts(snap, pins=(*snap.facts().pins, pin))


def _retask(snap, task_id, **changes):
    tasks = tuple(
        t.model_copy(update=changes) if t.task_id == task_id else t for t in snap.facts().tasks
    )
    return with_facts(snap, tasks=tasks)


@pytest.fixture
def alpha(with_a):
    snap = take_snapshot(with_a)
    spec, cand = _solve(with_a, snap, "L1")
    return with_a, snap, spec, cand


@pytest.fixture
def beta(with_a):
    snap = free_alternative(take_snapshot(with_a))
    spec, cand = _solve(with_a, snap, "L0")
    return with_a, snap, spec, cand


# ── PASS ───────────────────────────────────────────────────────


def test_alpha_pass(alpha):
    pack, snap, spec, cand = alpha
    v = validate(snap, cand, spec, pack)
    assert v.status == "PASS"
    assert [(c.check_id, c.status) for c in v.checks] == [
        (f"C{i:02d}", "PASS") for i in range(1, 12)
    ]
    assert v.validation_id.startswith("val_") and v.candidate_id == cand.candidate_id
    a_fields = next(t for t in snap.facts().tasks if t.task_id == "A").fields
    assert {f.source_ref for f in a_fields.values()} == {"scenario:new_task"}


def test_beta_pass(beta):
    pack, snap, spec, cand = beta
    assert validate(snap, cand, spec, pack).status == "PASS"


def test_r0_reconfirm_pass(seeded):
    snap = take_snapshot(seeded)
    assert validate(snap, _reconfirm(snap), None, seeded).status == "PASS"


# ── Scene 5·T03·T04 ────────────────────────────────────────────


def test_scene5_missing_b_only_c02(alpha):
    pack, snap, spec, cand = alpha
    v = validate(snap, _reshape(cand, snap, drop=("B",)), spec, pack)
    assert v.status == "FAIL"
    assert _bad(v) == [("C02", "TASK_MISSING", ("B",))]


def test_scene5_a_duration_15_only_c03(alpha):
    pack, snap, spec, cand = alpha
    v = validate(snap, _reshape(cand, snap, _asg("A", 60, 75, "A-CR-01")), spec, pack)
    assert _bad(v) == [("C03", "DURATION", ("A",))]


def test_unknown_and_duplicate_task_only_c02(alpha):
    pack, snap, spec, cand = alpha
    bad = _reshape(cand, snap, extra=(_asg("Z", 0, 30), _asg("E", 45, 75)))
    v = validate(snap, bad, spec, pack)
    assert _bad(v) == [("C02", "TASK_DUPLICATE", ("E",)), ("C02", "TASK_UNKNOWN", ("Z",))]


def test_t03_duplicate_conflicting_values_not_used_elsewhere(alpha):
    """중복된 task의 배정은 C02만 보고, 다른 check에는 쓰지 않는다."""
    pack, snap, spec, cand = alpha
    bad = _reshape(cand, snap, extra=(_asg("B", 5, 20),))  # 길이·시간창 모두 틀린 두 번째 B
    assert _failed_checks(validate(snap, bad, spec, pack)) == ["C02"]


# ── T05 시간창·선후행 ──────────────────────────────────────────


def test_t05_window_c04(alpha):
    pack, snap, spec, cand = alpha
    v = validate(snap, _reshape(cand, snap, _asg("C", 91, 121, "A-CR-01")), spec, pack)
    assert _bad(v) == [("C04", "WINDOW", ("C",))]


def test_calendar_c04(seeded):
    """근무 구간 하나에 들어가지 않는 배정은 C04 CALENDAR. 시간창 안이어도 FAIL."""
    snap = with_facts(take_snapshot(seeded), work_intervals=((30, 480), (1440, 1920), (2880, 3360)))
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert v.status == "FAIL"
    assert _bad(v) == [("C04", "CALENDAR", ("B",)), ("C04", "CALENDAR", ("D",))]


def test_t05_precedence_c05(seeded):
    snap = _retask(take_snapshot(seeded), "E", predecessors=(Predecessor(task_id="D", min_lag=20),))
    v = validate(snap, _reconfirm(snap), None, seeded)  # E 45 < D 끝 30 + 20
    assert _bad(v) == [("C05", "PRECEDENCE", ("D", "E"))]


def test_predecessor_missing_c05(seeded):
    """선행 작업이 검사 대상 배정에 없으면 건너뛰지 않는다 (fail-closed)."""
    snap = _retask(take_snapshot(seeded), "E", predecessors=(Predecessor(task_id="X9"),))
    found = detect_conflicts(snap, snap.facts().check_assignments(), seeded)
    assert [(c.rule_id, c.task_ids) for c in found] == [("PREDECESSOR_MISSING", ("E",))]
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert v.status == "FAIL"
    assert _bad(v) == [("C05", "PREDECESSOR_MISSING", ("E",))]


def test_predecessor_dropped_from_candidate_c02_and_c05(seeded):
    """후보에서 선행 작업이 빠지면 C02 TASK_MISSING과 함께 C05 PREDECESSOR_MISSING이다."""
    snap = _retask(take_snapshot(seeded), "E", predecessors=(Predecessor(task_id="D"),))
    base = snap.facts().base_assignments()
    cand = _reconfirm(snap, tuple(a for tid, a in sorted(base.items()) if tid != "D"))
    v = validate(snap, cand, None, seeded)
    assert _bad(v) == [("C02", "TASK_MISSING", ("D",)), ("C05", "PREDECESSOR_MISSING", ("E",))]


def test_cpsat_predecessor_missing_infeasible(with_a):
    """선행 작업이 Snapshot에 없는 작업이 있으면 해를 내지 않는다(Validator C05와 같은 기준).

    평소 L1은 OPTIMAL(Alpha)이다. 범위 밖 작업(E, UB)의 누락도 모델 전체를 INFEASIBLE로 만든다.
    """
    snap = _retask(take_snapshot(with_a), "E", predecessors=(Predecessor(task_id="X9"),))
    conflict = next(
        c
        for c in detect_conflicts(snap, snap.facts().check_assignments(), with_a)
        if c.rule_id == "SEP-LIFT-BELOW"
    )
    r = cpsat.solve(snap, build_search_spec(snap, conflict, "UA", "L1"), with_a)
    assert (r.stage1["status"], r.chosen_stage) == ("INFEASIBLE", None)


# ── T06·C06 ────────────────────────────────────────────────────


def test_t06_out_of_spec_task_moved(alpha):
    pack, snap, spec, cand = alpha
    v = validate(snap, _reshape(cand, snap, _asg("E", 50, 80)), spec, pack)  # UB 작업
    assert _bad(v) == [("C06", "TIME_AXIS_NOT_ALLOWED", ("E",))]


def test_c06_resource_not_in_spec(alpha):
    """탐색 범위의 대안(서버가 채운 적격 자원)에 없는 자원으로 바꾼 후보는 걸린다."""
    pack, snap, spec, cand = alpha
    assert "B-CR-01" not in spec.resource_alternatives.get("A", ())  # UA가 쓸 수 없는 자원
    v = validate(snap, _reshape(cand, snap, _asg("A", 60, 90, "B-CR-01")), spec, pack)
    assert ("C06", "RESOURCE_NOT_IN_SPEC", ("A",)) in _bad(v)


def test_c06_reconfirm_change_fails(with_a):
    snap = take_snapshot(with_a)
    base = snap.facts().base_assignments()
    moved = tuple(_asg("C", 90, 120, "A-CR-01") if tid == "C" else a for tid, a in base.items())
    v = validate(snap, _reconfirm(snap, moved), None, with_a)
    assert v.status == "FAIL"
    assert ("C06", "TIME_AXIS_NOT_ALLOWED", ("C",)) in _bad(v)


def test_c06_task_pinned(with_a):
    """고정된 C를 옮긴 후보는 SearchSpec과 관계없이 걸린다 (AG-27)."""
    snap = _pin(take_snapshot(with_a), "C")
    conflict = detect_conflicts(snap, snap.facts().check_assignments(), with_a)[0]
    spec = build_search_spec(snap, conflict, "UA", "L1")
    alpha_like = _rehash(
        Candidate(
            candidate_id="cand_x",
            snapshot_id=snap.snapshot_id,
            search_spec_id=spec.search_spec_id,
            search_spec_hash=spec.hash,
            solver_result_id="sr_x",
            base_plan_revision=0,
            context_version=1,
            pack_hash=with_a.pack_hash,
            assignments=tuple(
                {
                    **snap.facts().base_assignments(),
                    "A": _asg("A", 60, 90, "A-CR-01"),
                    "C": _asg("C", 90, 120, "A-CR-01"),
                }.values()
            ),
            candidate_hash="",
            kind="REPLAN",
        ),
        snap,
    )
    bad = _bad(validate(snap, alpha_like, spec, with_a))
    assert ("C06", "TASK_PINNED", ("C",)) in bad


def test_c06_outside_acting_unit(alpha):
    pack, snap, spec, cand = alpha
    forged = spec.model_copy(update={"acting_unit_id": "UB"})
    bad = _bad(validate(snap, cand, forged, pack))
    assert ("C06", "OUTSIDE_ACTING_UNIT", ("A",)) in bad
    assert ("C06", "OUTSIDE_ACTING_UNIT", ("C",)) in bad
    assert ("C01", "SEARCH_SPEC_HASH_MISMATCH", ()) in bad


# ── T07·T09 자원 ───────────────────────────────────────────────


def test_t07_boundary_passes_overlap_fails(alpha):
    pack, snap, spec, cand = alpha  # Alpha: A 60–90, C 90–120 (경계)
    assert validate(snap, cand, spec, pack).status == "PASS"
    v = validate(snap, _reshape(cand, snap, _asg("C", 89, 119, "A-CR-01")), spec, pack)
    assert _bad(v) == [("C09", "CAP-RESOURCE", ("A", "C"))]


def test_t09_b_crane_c08(beta):
    pack, snap, spec, cand = beta
    v = validate(snap, _reshape(cand, snap, _asg("A", 60, 90, "B-CR-01")), spec, pack)
    bad = _bad(v)
    assert ("C08", "RESOURCE_AUTH", ("A",)) in bad
    assert v.status == "FAIL"


def test_c07_resource_missing_and_unknown(alpha):
    pack, snap, spec, cand = alpha
    v = validate(snap, _reshape(cand, snap, _asg("A", 60, 90)), spec, pack)
    assert ("C07", "RESOURCE_MISSING", ("A",)) in _bad(v)
    v = validate(snap, _reshape(cand, snap, _asg("A", 60, 90, "NO-SUCH")), spec, pack)
    assert ("C07", "RESOURCE_TYPE", ("A",)) in _bad(v)


# ── C01 ────────────────────────────────────────────────────────


def test_c01_references_and_hashes(alpha):
    pack, snap, spec, cand = alpha

    def c01(v):
        return sorted(r for c, r, _ in _bad(v) if c == "C01")

    assert c01(validate(snap, cand.model_copy(update={"candidate_hash": "x"}), spec, pack)) == [
        "CANDIDATE_HASH_MISMATCH"
    ]
    tampered = snap.model_copy(update={"snapshot_hash": "x"})
    assert "SNAPSHOT_HASH_MISMATCH" in c01(validate(tampered, cand, spec, pack))
    other = take_snapshot(pack)  # 같은 사실, 다른 snapshot_id
    assert c01(validate(other, cand, spec, pack)) == [
        "SEARCH_SPEC_REF_MISMATCH",
        "SNAPSHOT_REF_MISMATCH",
    ]
    stale = _rehash(cand.model_copy(update={"context_version": 0, "base_plan_revision": 1}), snap)
    assert c01(validate(snap, stale, spec, pack)) == [
        "CONTEXT_VERSION_MISMATCH",
        "PLAN_REVISION_MISMATCH",
    ]
    assert c01(validate(snap, cand, None, pack)) == ["SEARCH_SPEC_MISSING"]
    wrong_id = spec.model_copy(update={"search_spec_id": "ss_other"})
    assert c01(validate(snap, cand, wrong_id, pack)) == ["SEARCH_SPEC_REF_MISMATCH"]
    wrong_hash = spec.model_copy(update={"hash": "x"})
    assert c01(validate(snap, cand, wrong_hash, pack)) == ["SEARCH_SPEC_HASH_MISMATCH"]


def test_c01_reconfirm_with_spec_unexpected(alpha):
    pack, snap, spec, _ = alpha
    v = validate(snap, _reconfirm(snap), spec, pack)
    assert [c.reason_code for c in v.checks if c.check_id == "C01"] == ["SEARCH_SPEC_UNEXPECTED"]


# ── check 안의 순서 ────────────────────────────────────────────


def test_assignment_order_does_not_change_checks(with_a):
    snap = take_snapshot(with_a)
    moved = tuple(
        _asg(a.task_id, a.start + 1, a.end + 1, a.resource_id)
        for a in snap.facts().base_assignments().values()
    )
    forward = validate(snap, _reconfirm(snap, moved), None, with_a)
    backward = validate(snap, _reconfirm(snap, moved[::-1]), None, with_a)
    assert forward.checks == backward.checks
    assert len([c for c in forward.checks if c.check_id == "C06"]) > 1
    for check_id in {c.check_id for c in forward.checks}:
        items = [
            (c.task_ids, c.reason_code or "") for c in forward.checks if c.check_id == check_id
        ]
        assert items == sorted(items)


# ── T29 ────────────────────────────────────────────────────────


def test_t29_other_pack_hash_c01(alpha, pack_copy):
    pack, snap, spec, cand = alpha
    f = pack_copy / "site.yaml"  # 후보와 무관한 값(B-CR-01 가용 구간)만 바꾼다
    text = f.read_text(encoding="utf-8")
    b_line = next(line for line in text.splitlines() if "resource_id: B-CR-01" in line)
    f.write_text(text.replace(b_line, b_line.replace("[[0, 3360]]", "[[0, 3300]]")), "utf-8")
    other = load_pack(pack_copy)
    assert other.pack_hash != pack.pack_hash
    assert _bad(validate(snap, cand, spec, other)) == [("C01", "PACK_HASH_MISMATCH", ())]


def test_c07_resource_zone_and_requirement(seeded):
    """Rule Engine의 구역·요구 조건 사유는 기존 자원 검사(C07)로 잡는다."""
    snap = _retask(take_snapshot(seeded), "C", zone_id="D")  # A-CR-01은 B·C 구역만
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert ("C07", "RESOURCE_ZONE", ("C",)) in _bad(v)
    need = (Requirement(attribute="max_load", op="GTE", value=40),)  # A-CR-01은 25 t
    snap = _retask(take_snapshot(seeded), "C", resource_requirements=need)
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert ("C07", "RESOURCE_REQUIREMENT", ("C",)) in _bad(v)
    assert v.status == "INCOMPLETE"  # 확인 값(resource 필드)과도 달라졌다


def test_default_requirements_mismatch_c11(seeded):
    """작업 유형 기본 요구 조건은 서버가 도출한다. Snapshot 값이 Pack과 다르면 C11 (CV-11)."""
    snap = _retask(take_snapshot(seeded), "C", default_requirements=())
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert _bad(v) == [("C11", "DEFAULT_REQUIREMENTS_MISMATCH", ("C",))]


def test_t29_hazard_tags_mismatch_c11(seeded):
    snap = _retask(take_snapshot(seeded), "C", hazard_tags=())
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert v.status == "INCOMPLETE"
    assert _bad(v) == [("C11", "HAZARD_TAGS_MISMATCH", ("C",))]


# ── T31 ────────────────────────────────────────────────────────


def test_t31_time_fixed_resource_moved_pass(seeded):
    x = make_task(
        seeded,
        task_id="X",
        zone_id="C",
        earliest_start=60,
        latest_start=60,
        latest_end=90,
    )
    add_task(seeded, x)
    snap = free_alternative(take_snapshot(seeded))
    cap = next(
        c
        for c in detect_conflicts(snap, snap.facts().check_assignments(), seeded)
        if c.rule_id == "CAP-RESOURCE"
    )
    spec, cand = _solve(seeded, snap, "L0", conflict=cap)
    assert _asg("X", 60, 90, "SITE-CR-01") in cand.assignments
    v = validate(snap, cand, spec, seeded)
    assert v.status == "PASS"


# ── C11 ────────────────────────────────────────────────────────


def _set_field(snap, task_id, name, **changes):
    task = next(t for t in snap.facts().tasks if t.task_id == task_id)
    fields = dict(task.fields)
    fields[name] = FieldRecord(**{**fields[name].model_dump(), **changes})
    return _retask(snap, task_id, fields=fields)


def test_c11_proposed_field_incomplete(seeded):
    snap = _set_field(take_snapshot(seeded), "C", "zone_id", status="PROPOSED")
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert v.status == "INCOMPLETE"
    assert _bad(v) == [("C11", "FIELD_NOT_CONFIRMED", ("C",))]


def test_c11_value_mismatch_incomplete(seeded):
    snap = _set_field(
        take_snapshot(seeded),
        "C",
        "window",
        value={"earliest_start": 60, "latest_start": 90, "latest_end": 150},
    )
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert _bad(v) == [("C11", "CONFIRMED_VALUE_MISMATCH", ("C",))]


def test_c11_field_missing_and_unknown_work_type(seeded):
    snap = take_snapshot(seeded)
    c = next(t for t in snap.facts().tasks if t.task_id == "C")
    no_resource = {k: v for k, v in c.fields.items() if k != "resource"}
    snap = _retask(snap, "C", fields=no_resource)
    snap = _retask(snap, "E", work_type="WELDING")
    v = validate(snap, _reconfirm(snap), None, seeded)
    assert _bad(v) == [
        ("C11", "FIELD_MISSING", ("C",)),
        ("C11", "UNKNOWN_WORK_TYPE", ("E",)),
    ]


def test_fail_and_incomplete_stored_as_incomplete(seeded):
    snap = _set_field(take_snapshot(seeded), "C", "duration", status="PROPOSED")
    base = snap.facts().base_assignments()
    moved = tuple(_asg("E", 50, 80) if tid == "E" else a for tid, a in base.items())
    v = validate(snap, _reconfirm(snap, moved), None, seeded)
    assert {c.status for c in v.checks} == {"PASS", "FAIL", "INCOMPLETE"}
    assert v.status == "INCOMPLETE"


# ── Pack Rule 매핑 ─────────────────────────────────────────────


def test_rule_mapping_uses_type_not_rule_id(with_a):
    renamed = tuple(
        r.model_copy(update={"rule_id": "ZZ-LIFT"}) if r.rule_id == "SEP-LIFT-BELOW" else r
        for r in with_a.rules
    )
    renamed_cap = tuple(
        r.model_copy(update={"rule_id": "SEP-NOT-REALLY"}) if r.type == "CAPACITY" else r
        for r in renamed
    )
    pack = with_a.model_copy(update={"rules": renamed_cap})  # pack_hash는 그대로
    snap = take_snapshot(with_a)
    base = snap.facts().base_assignments()  # A 0–30: B 아래 인양
    overlap = tuple(_asg("C", 20, 50, "A-CR-01") if tid == "C" else a for tid, a in base.items())
    bad = _bad(validate(snap, _reconfirm(snap, overlap), None, pack))
    assert ("C10", "ZZ-LIFT", ("A", "B")) in bad
    assert ("C09", "SEP-NOT-REALLY", ("A", "C")) in bad


def test_mapping_covers_engine_and_loader():
    assert set(BASIC_TO_CHECK) == BASIC_RULE_IDS
    assert set(RULE_TYPE_TO_CHECK) == EVALUATORS


def test_basic_rule_ids_match_engine_literals():
    """engine.py에서 _conflict에 문자열 리터럴로 넘기는 rule_id = BASIC_RULE_IDS."""
    tree = ast.parse(inspect.getsource(engine))
    literals = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_conflict"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert set(literals) <= BASIC_RULE_IDS
    assert BASIC_RULE_IDS <= set(literals)


def test_unmapped_rule_fails_closed_c01(alpha, monkeypatch):
    pack, snap, spec, cand = alpha
    unknown = Conflict(
        rule_id="NEW-RULE", task_ids=("A", "C"), resource_id=None, zone_ids=(), interval=(0, 10)
    )
    monkeypatch.setattr(validator_module, "detect_conflicts", lambda *_: [unknown])
    v = validate(snap, cand, spec, pack)
    assert v.status == "FAIL"
    assert _bad(v) == [("C01", "UNMAPPED_RULE", ("A", "C"))]


# ── 등록·불변 ──────────────────────────────────────────────────


def test_insert_validation_immutable(alpha):
    pack, snap, spec, _ = alpha
    result = cpsat.solve(snap, spec, pack)
    stored_cand = build_candidate(snap, spec, result)
    v = validate(snap, stored_cand, spec, pack)
    with db.write() as tx:
        insert_search_spec(tx, pack.site_id, spec)
        register_solver_outcome(tx, snap, result, stored_cand)
        insert_validation(tx, pack.site_id, v)
    with db.read() as conn:
        row = conn.execute(
            "SELECT validation_id, candidate_id, status, checks FROM validation"
        ).fetchone()
    assert row[:3] == (v.validation_id, stored_cand.candidate_id, "PASS")
    assert [c["check_id"] for c in json.loads(row[3])] == [f"C{i:02d}" for i in range(1, 12)]
    for sql in ("UPDATE validation SET status = 'FAIL'", "DELETE FROM validation"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable: validation"), db.write() as tx:
            tx.execute(sql)
