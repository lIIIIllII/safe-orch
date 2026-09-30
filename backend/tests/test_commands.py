"""Command Service·Consultation (설계서 §9·§10, 부록 A.14). 게이트 경로, 기본안 B, T01·T11–T15·T18–T23·T35·T45·T51."""

import sqlite3
import threading
import uuid

import pytest
from conftest import take_snapshot

from app.commands import events as events_module
from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    reject_candidate,
    waive,
)
from app.commands.consultation import build_consultation
from app.commands.events import EventReport, HoldRelease, receive_event, release_hold_command
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.domain.consultation import build_items
from app.domain.hashes import candidate_hash
from app.domain.ids import new_id
from app.domain.models import Assignment, Candidate, Consent
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.commands import get_command_result
from app.store.repos.consents import insert_consent
from app.store.repos.consultations import consultation_view, list_review_queue
from app.store.repos.dispatch import list_jobs
from app.store.repos.plans import get_current_plan
from app.store.repos.records import (
    insert_candidate,
    insert_search_spec,
    insert_validation,
    register_solver_outcome,
)
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks
from app.validator.validator import validate

# ── 도우미 ─────────────────────────────────────────────────────


def _key():
    return uuid.uuid4().hex


def _form_a(pack, **changes):
    data = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    data.update(changes)
    return TaskRequestForm(**data)


def _submit_a(pack, **changes):
    out = submit_task_request(pack, "planner_a", _key(), _form_a(pack, **changes))
    assert out.status == "APPLIED", out
    return out


def _site(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id)


def _count(table):
    with db.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _jobs(pack):
    with db.read() as conn:
        return [(j["kind"], j["dedupe_key"]) for j in list_jobs(conn, pack.site_id)]


def _solve(pack, snap, level, conflict=None):
    """SearchSpec 저장 → Solver → 결과 등록(후보가 있으면 VALIDATE 등록)."""
    if conflict is None:
        conflict = detect_conflicts(snap, snap.facts().check_assignments(), pack)[0]
    spec = build_search_spec(snap, conflict, "UA", level)
    with db.write() as tx:
        insert_search_spec(tx, pack.site_id, spec)
    result = cpsat.solve(snap, spec, pack)
    cand = build_candidate(snap, spec, result)
    with db.write() as tx:
        register_solver_outcome(tx, snap, result, cand)
    return spec, result, cand


def _validate_and_consult(pack, snap, spec, cand):
    v = validate(snap, cand, spec, pack)
    with db.write() as tx:
        insert_validation(tx, pack.site_id, v)
        items = (
            build_consultation(tx, pack.site_id, cand.candidate_id) if v.status == "PASS" else ()
        )
    return v, items


def _view(pack, cand):
    with db.read() as conn:
        return consultation_view(conn, pack.site_id, cand.candidate_id)


def _queue(pack):
    with db.read() as conn:
        return list_review_queue(conn, pack.site_id)


def _approve(pack, cand, v, *, key=None, actor="supervisor", expected=None):
    if expected is None:
        expected = _site(pack).context_version
    body = ApproveRequest(
        candidate_id=cand.candidate_id,
        validation_id=v.validation_id,
        expected_context_version=expected,
    )
    return approve_and_commit(pack, actor, key or _key(), body)


def _waive(pack, cand, task_ids=("C",), comment="작업발판 일정 확인됨", actor="supervisor"):
    body = WaiveRequest(candidate_id=cand.candidate_id, task_ids=task_ids, comment=comment)
    return waive(pack, actor, _key(), body)


def _reject(pack, cand, v, reason, targets=(), axes=(), comment=""):
    body = RejectRequest(
        candidate_id=cand.candidate_id,
        validation_id=v.validation_id,
        reason_code=reason,
        target_task_ids=targets,
        axes=axes,
        comment=comment,
    )
    return reject_candidate(pack, "supervisor", _key(), body)


def _event(
    pack, source="rep-1", text="도장 준비 15분 늦어져 10시부터", target=None, actor="reporter"
):
    body = EventReport(source_event_id=source, event_type="DELAY", text=text, target_task_id=target)
    return receive_event(pack, actor, _key(), body)


def _release(pack, hold_id, resolution="NO_CHANGE", expected=None):
    if expected is None:
        expected = _site(pack).context_version
    body = HoldRelease(hold_id=hold_id, resolution=resolution, expected_context_version=expected)
    return release_hold_command(pack, "supervisor", _key(), body)


@pytest.fixture
def alpha(seeded):
    """폼 A → L1 Alpha 등록 → PASS → Consultation(A COVERED, C PENDING)."""
    _submit_a(seeded)
    snap = take_snapshot(seeded)
    spec, _, cand = _solve(seeded, snap, "L1")
    v, _ = _validate_and_consult(seeded, snap, spec, cand)
    assert v.status == "PASS"
    return seeded, snap, spec, cand, v


@pytest.fixture
def alpha_waived(alpha):
    pack, _, _, cand, _ = alpha
    assert _waive(pack, cand).status == "APPLIED"
    return alpha


# ── 게이트 경로 (우선순위 문서 "두 개의 경로") ───────────────────


def test_gate_path(seeded):
    pack = seeded
    form = _submit_a(pack)
    ctx1 = _site(pack).context_version
    assert form.context_version == ctx1 == 1
    assert ("RECHECK", "RECHECK:ctx1") in _jobs(pack)

    snap = take_snapshot(pack)
    conflicts = detect_conflicts(snap, snap.facts().check_assignments(), pack)
    assert [(c.rule_id, c.task_ids) for c in conflicts] == [("SEP-LIFT-BELOW", ("A", "B"))]

    _, l0, none = _solve(pack, snap, "L0", conflicts[0])
    assert l0.stage1["status"] == "INFEASIBLE" and none is None
    assert not [k for k, _ in _jobs(pack) if k == "VALIDATE"]

    spec, _, alpha = _solve(pack, snap, "L1", conflicts[0])
    assert ("VALIDATE", f"VALIDATE:{alpha.candidate_id}") in _jobs(pack)
    v, items = _validate_and_consult(pack, snap, spec, alpha)
    assert v.status == "PASS"
    assert {i.task_id: i.base_status for i in items} == {"A": "COVERED", "C": "PENDING"}
    assert _view(pack, alpha).status == "OPEN"
    assert _queue(pack) == [alpha.candidate_id]  # OPEN도 검토 대기 (A.14)

    blocked = _approve(pack, alpha, v)
    assert (blocked.status, blocked.reason_codes) == ("REJECTED", ("CONSULTATION_INCOMPLETE",))

    assert _waive(pack, alpha).status == "APPLIED"
    assert _view(pack, alpha).item_status == {"A": "COVERED", "C": "WAIVED"}

    ok = _approve(pack, alpha, v)
    assert ok.status == "APPLIED" and ok.plan_revision == 1
    with db.read() as conn:
        plan = get_current_plan(conn, pack.site_id)
    assert plan.plan_revision == 1 and plan.candidate_id == alpha.candidate_id
    assert plan.assignments == alpha.assignments
    assert plan.committed_context_version == ctx1
    assert _view(pack, alpha).status == "COMPLETE"  # COMMITTED를 STALE보다 먼저 본다
    assert _queue(pack) == []


# ── 기본안 B: 구조화 거절 → 제약 → 재탐색 ──────────────────────


def test_plan_b_reject_immovable_c_then_l1_infeasible(alpha):
    pack, _, _, cand, v = alpha
    before = _site(pack).context_version
    out = _reject(pack, cand, v, "TASK_IMMOVABLE", ("C",), ("TIME", "RESOURCE"), "작업발판 연계")
    assert out.status == "APPLIED" and len(out.result_refs["constraint_ids"]) == 1
    assert _site(pack).context_version == before + 1
    assert _view(pack, cand).status == "CANCELLED"

    snap = take_snapshot(pack)
    [fc] = snap.facts().constraints
    assert (fc.task_id, fc.frozen_axes, fc.source_type) == ("C", ("RESOURCE", "TIME"), "DECISION")
    spec, result, none = _solve(pack, snap, "L1")
    assert spec.axes["C"].time is False and spec.axes["C"].resource is False
    assert result.stage1["status"] == "INFEASIBLE" and none is None  # T17 일부


def test_reject_without_constraint_keeps_context_and_blocks_approval(alpha_waived):
    pack, _, _, cand, v = alpha_waived
    before = _site(pack).context_version
    out = _reject(pack, cand, v, "PREFERENCE", comment="오전 중 인양은 피하고 싶다")
    assert out.status == "APPLIED" and out.result_refs["constraint_ids"] == []
    assert _site(pack).context_version == before
    assert _view(pack, cand).status == "CANCELLED" and _queue(pack) == []
    assert _approve(pack, cand, v).reason_codes == ("CANDIDATE_REJECTED",)


def test_reject_task_immovable_requires_target_and_axes(alpha):
    pack, _, _, cand, v = alpha
    assert _reject(pack, cand, v, "TASK_IMMOVABLE").reason_codes == ("TARGET_REQUIRED",)
    assert _reject(pack, cand, v, "TASK_IMMOVABLE", ("C",)).reason_codes == ("TARGET_REQUIRED",)
    assert _reject(pack, cand, v, "NOPE").reason_codes == ("INVALID_REASON_CODE",)
    assert _reject(pack, cand, v, "OTHER", ("Z",), ("TIME",)).reason_codes == ("TASK_NOT_FOUND",)
    assert _count("feedback_constraint") == 0 and _count("decision") == 0


# ── T01·폼 ─────────────────────────────────────────────────────


def test_t01_form_missing_critical_field_rejected(seeded):
    audit = _count("audit")
    out = submit_task_request(
        seeded, "planner_a", "k-t01", _form_a(seeded, requested_resource_id=None)
    )
    assert out.status == "REJECTED" and "FIELD_MISSING" in out.reason_codes
    with db.read() as conn:
        assert "A" not in {t.task_id for t in list_current_tasks(conn, seeded.site_id, seeded)}
        assert get_command_result(conn, "k-t01")["status"] == "REJECTED"
    assert _site(seeded).context_version == 0
    assert _count("consent") == 0 and _count("audit") == audit and _jobs(seeded) == []


def test_form_records_confirmed_fields_movable_and_consents(seeded):
    out = _submit_a(seeded, hazard_tags=("NONE",))
    source_ref = f"form:{out.result_refs['form_id']}"
    snap = take_snapshot(seeded)
    a = snap.facts().task_map()["A"]
    assert (a.unit_id, a.owner_actor_id, a.lifecycle, a.revision) == ("UA", "planner_a", "READY", 1)
    assert a.hazard_tags == ("LIFTING",)  # 입력 태그는 버리고 도출
    assert (a.movable.time, a.movable.resource) == (True, False)
    assert set(a.fields) == {"zone_id", "duration", "window", "resource"}
    assert {(f.status, f.source_ref) for f in a.fields.values()} == {("CONFIRMED", source_ref)}
    consents = {c.axis: c.scope for c in snap.facts().consents}
    assert consents == {
        "TIME": {"start_min": 0, "start_max": 60},
        "RESOURCE": {"resource_ids": ["A-CR-01"]},
    }
    with db.read() as conn:
        row = conn.execute("SELECT command, actor_id FROM audit ORDER BY audit_id DESC").fetchone()
    assert row == ("SUBMIT_TASK_REQUEST", "planner_a")


@pytest.mark.parametrize(
    ("actor", "changes", "reason"),
    [
        ("supervisor", {}, "NOT_AUTHORIZED"),
        ("planner_a", {"task_id": "B"}, "TASK_ID_EXISTS"),
        ("planner_a", {"work_type": "FLYING"}, "UNKNOWN_WORK_TYPE"),
        ("planner_a", {"zone_id": "Q"}, "UNKNOWN_ZONE"),
        ("planner_a", {"latest_end": 20}, "INVALID_WINDOW"),
        ("planner_a", {"requested_resource_id": "X-CR-99"}, "UNKNOWN_RESOURCE"),
        ("planner_a", {"required_resource_type": "TRUCK"}, "RESOURCE_TYPE_MISMATCH"),
        ("planner_a", {"requested_resource_id": "B-CR-01"}, "RESOURCE_NOT_AUTHORIZED"),
        ("planner_a", {"predecessors": [{"task_id": "Z"}]}, "PREDECESSOR_NOT_FOUND"),
    ],
)
def test_form_rejections(seeded, actor, changes, reason):
    out = submit_task_request(seeded, actor, _key(), _form_a(seeded, **changes))
    assert out.status == "REJECTED" and reason in out.reason_codes
    assert _site(seeded).context_version == 0 and _count("consent") == 0


# ── T11–T13 ────────────────────────────────────────────────────


def test_t11_non_pass_candidate_cannot_be_approved(alpha):
    pack, snap, spec, cand, _ = alpha
    short = tuple(
        a.model_copy(update={"end": a.start + 15}) if a.task_id == "A" else a
        for a in cand.assignments
    )
    bad = cand.model_copy(
        update={
            "candidate_id": new_id("cand"),
            "assignments": short,
            "candidate_hash": candidate_hash(
                short,
                cand.base_plan_revision,
                cand.context_version,
                snap.snapshot_hash,
                cand.search_spec_hash,
                cand.pack_hash,
            ),
        }
    )
    with db.write() as tx:
        insert_candidate(tx, pack.site_id, bad)
    v_bad, _ = _validate_and_consult(pack, snap, spec, bad)
    assert v_bad.status == "FAIL"
    assert "VALIDATION_NOT_PASS" in _approve(pack, bad, v_bad).reason_codes
    assert "VALIDATION_NOT_PASS" in _reject(pack, bad, v_bad, "OTHER").reason_codes
    assert _count("plan") == 1


def test_t12_second_candidate_on_same_base_is_stale_plan(alpha_waived):
    pack, snap, _, first, v1 = alpha_waived
    spec2, _, second = _solve(pack, snap, "L1")
    v2, _ = _validate_and_consult(pack, snap, spec2, second)
    assert _waive(pack, second).status == "APPLIED"
    assert _approve(pack, first, v1).status == "APPLIED"
    out = _approve(pack, second, v2)
    assert out.status == "REJECTED" and out.reason_codes == ("STALE_PLAN",)


def test_t13_hold_blocks_approval_until_every_hold_released(alpha_waived):
    pack, _, _, cand, v = alpha_waived
    h1 = _event(pack, "rep-1").result_refs["hold_id"]
    _event(pack, "rep-2", text="도장 또 늦어진대요")
    assert "HOLD_ACTIVE" in _approve(pack, cand, v).reason_codes
    released = _release(pack, h1)
    assert released.status == "APPLIED" and released.result_refs["recheck"] is False
    assert "HOLD_ACTIVE" in _approve(pack, cand, v).reason_codes


# ── T14–T15 Event ──────────────────────────────────────────────


def test_t14_event_failure_leaves_nothing(seeded, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(events_module, "insert_hold", boom)
    audit = _count("audit")
    with pytest.raises(RuntimeError, match="forced"):
        _event(seeded)
    assert _site(seeded).context_version == 0
    assert (_count("event"), _count("hold"), _count("command_result")) == (0, 0, 0)
    assert _count("audit") == audit


def test_t15_source_event_resend_and_body_mismatch(seeded):
    first = _event(seeded)
    assert first.status == "APPLIED" and first.context_version == 1
    again = _event(seeded)  # 새 멱등 키, 같은 source_event_id·본문
    assert again.status == "REPLAYED" and again.result_refs == first.result_refs
    other = _event(seeded, text="다른 내용")
    assert (other.status, other.reason_codes) == ("REJECTED", ("SOURCE_BODY_MISMATCH",))
    assert (_count("event"), _count("hold"), _site(seeded).context_version) == (1, 1, 1)


def test_event_scope_hold_snapshot_and_roles(seeded):
    task_hold = _event(seeded, "rep-1", target="E").result_refs["hold_id"]
    site_hold = _event(seeded, "rep-2", target="Z").result_refs["hold_id"]
    holds = {h.hold_id: (h.scope, h.task_id) for h in take_snapshot(seeded).facts().holds}
    assert holds == {task_hold: ("TASK", "E"), site_hold: ("SITE", None)}
    assert _event(seeded, "rep-3", actor="planner_a").reason_codes == ("NOT_AUTHORIZED",)
    assert _event(seeded, "rep-4", actor="supervisor").status == "APPLIED"


def test_hold_release_no_change_only(seeded):
    hold_id = _event(seeded).result_refs["hold_id"]
    ctx = _site(seeded).context_version
    assert _release(seeded, hold_id, expected=ctx - 1).reason_codes == ("STALE_CONTEXT",)
    assert _release(seeded, hold_id, "FACT_CONFIRMED").reason_codes == ("RESOLUTION_NOT_SUPPORTED",)
    assert _release(seeded, "hold_nope").reason_codes == ("HOLD_NOT_FOUND",)
    out = _release(seeded, hold_id)
    assert out.status == "APPLIED" and out.context_version == ctx + 1
    assert out.result_refs["recheck"] is True
    assert ("RECHECK", f"RECHECK:ctx{ctx + 1}") in _jobs(seeded)
    assert take_snapshot(seeded).facts().holds == ()
    assert _release(seeded, hold_id).reason_codes == ("HOLD_NOT_ACTIVE",)


# ── T18–T20 경쟁·응답 유실 ─────────────────────────────────────


def test_t18_event_before_approval(alpha_waived):
    pack, _, _, cand, v = alpha_waived
    expected = _site(pack).context_version  # 검토 화면을 연 시점
    _event(pack)
    out = _approve(pack, cand, v, expected=expected)
    assert (out.status, out.reason_codes) == ("REJECTED", ("STALE_CONTEXT", "HOLD_ACTIVE"))
    assert _count("plan") == 1


def test_t18_event_before_approval_two_threads(alpha_waived):
    """두 연결(스레드)과 barrier로 'Event 커밋 → 승인' 순서를 고정한다."""
    pack, _, _, cand, v = alpha_waived
    expected = _site(pack).context_version
    barrier = threading.Barrier(2, timeout=10)
    out, errors = {}, []

    def reporter():
        try:
            out["event"] = _event(pack)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            db.close()
            barrier.wait()

    def supervisor():
        try:
            barrier.wait()
            out["approve"] = _approve(pack, cand, v, expected=expected)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            db.close()

    threads = [threading.Thread(target=reporter), threading.Thread(target=supervisor)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    assert out["event"].status == "APPLIED"
    assert out["approve"].reason_codes == ("STALE_CONTEXT", "HOLD_ACTIVE")
    assert _count("plan") == 1


def test_t19_approval_before_event_keeps_plan(alpha_waived):
    pack, _, _, cand, v = alpha_waived
    assert _approve(pack, cand, v).status == "APPLIED"
    assert _event(pack).status == "APPLIED"
    with db.read() as conn:
        assert get_current_plan(conn, pack.site_id).candidate_id == cand.candidate_id
    again = _approve(pack, cand, v)  # 새 키: 2단계에서 기존 Plan 반환
    assert again.status == "REPLAYED" and again.result_refs == {"plan_revision": 1}
    assert _count("plan") == 2 and _site(pack).plan_revision == 1


def test_t20_lost_approval_response_retry_same_key(alpha_waived):
    pack, _, _, cand, v = alpha_waived
    first = _approve(pack, cand, v, key="k-approve")
    retry = _approve(pack, cand, v, key="k-approve")
    assert first.status == "APPLIED" and retry.status == "REPLAYED"
    assert retry.result_refs == first.result_refs and retry.plan_revision == 1
    assert _count("plan") == 2
    with db.read() as conn:
        n = conn.execute("SELECT COUNT(*) FROM decision WHERE type = 'APPROVE'").fetchone()[0]
    assert n == 1


# ── T21–T23 협의 ───────────────────────────────────────────────


def test_t21_pending_item_blocks_approval(alpha):
    pack, _, _, cand, v = alpha
    out = _approve(pack, cand, v)
    assert out.reason_codes == ("CONSULTATION_INCOMPLETE",)


def test_t22_waive_rules(alpha):
    pack, _, _, cand, v = alpha
    assert _waive(pack, cand, comment="  ").reason_codes == ("COMMENT_REQUIRED",)
    assert _waive(pack, cand, ("A",)).reason_codes == ("ITEM_NOT_WAIVABLE",)  # COVERED
    assert _waive(pack, cand, ("E",)).reason_codes == ("ITEM_NOT_FOUND",)
    assert _waive(pack, cand, actor="planner_a").reason_codes == ("NOT_AUTHORIZED",)
    assert _waive(pack, cand).status == "APPLIED"
    assert _waive(pack, cand).reason_codes == ("ITEM_NOT_WAIVABLE",)  # 이미 WAIVED
    assert _approve(pack, cand, v).status == "APPLIED"


def test_t23_movability_consent_does_not_extend(seeded):
    _submit_a(seeded)
    with db.write() as tx:
        insert_consent(
            tx,
            seeded.site_id,
            Consent(
                consent_id=new_id("cns"),
                task_id="A",
                task_revision=1,
                owner_actor_id="planner_a",
                axis="RESOURCE",
                scope={"resource_ids": ["SITE-CR-01"]},
                source_ref="proposal:test",
            ),
            1,
        )
    facts = take_snapshot(seeded).facts()
    base = facts.base_assignments()

    def status(start, resource):
        a = Assignment(task_id="A", start=start, end=start + 30, resource_id=resource)
        cand = Candidate(
            candidate_id="cand_t23",
            snapshot_id="snap",
            search_spec_id=None,
            search_spec_hash=None,
            solver_result_id=None,
            base_plan_revision=0,
            context_version=1,
            pack_hash=facts.pack_hash,
            assignments=tuple(a if tid == "A" else x for tid, x in base.items()),
            candidate_hash="",
            kind="RECONFIRM",
        )
        return {i.task_id: i.base_status for i in build_items(facts, cand)}.get("A")

    assert status(0, "A-CR-01") is None  # 변경 없음
    assert status(60, "SITE-CR-01") == "COVERED"
    assert status(60, "B-CR-01") == "PENDING"  # 다른 자원
    assert status(61, "SITE-CR-01") == "PENDING"  # 범위 밖 시간


# ── T35 ────────────────────────────────────────────────────────


def test_t35_event_during_consultation_cancels(alpha):
    pack, _, _, cand, v = alpha
    assert _queue(pack) == [cand.candidate_id]
    expected = _site(pack).context_version
    _event(pack)
    assert _view(pack, cand).status == "CANCELLED" and _queue(pack) == []
    out = _approve(pack, cand, v, expected=expected)
    assert {"STALE_CONTEXT", "HOLD_ACTIVE"} <= set(out.reason_codes)
    assert _waive(pack, cand).reason_codes == ("STALE_CONTEXT",)


# ── 거절이면 도메인 변경 없음 (SAVEPOINT, 부록 A.14) ──────────


def test_rejecting_handler_writes_are_rolled_back(seeded):
    from app.commands.service import Body, Result, run_command
    from app.store.repos.site import bump_context_version

    class Fake(Body):
        x: int = 1

    def write_then_reject(tx, ctx, body):
        bump_context_version(tx, ctx.site_id)
        r = Result()
        r.reject("FAKE_REASON")
        return r

    audit = _count("audit")
    out = run_command(seeded, "FAKE", "planner_a", "k-fake", Fake(), write_then_reject)
    assert (out.status, out.reason_codes, out.context_version) == ("REJECTED", ("FAKE_REASON",), 0)
    assert _site(seeded).context_version == 0 and _count("audit") == audit
    with db.read() as conn:
        assert get_command_result(conn, "k-fake")["status"] == "REJECTED"


# ── T45 멱등 키 ────────────────────────────────────────────────


def test_t45_same_key_other_body_or_command(seeded):
    first = submit_task_request(seeded, "planner_a", "k1", _form_a(seeded))
    other_body = submit_task_request(seeded, "planner_a", "k1", _form_a(seeded, duration=20))
    other_actor = submit_task_request(seeded, "planner_b", "k1", _form_a(seeded))
    body = EventReport(source_event_id="rep-1", event_type="DELAY", text="x")
    other_cmd = receive_event(seeded, "planner_a", "k1", body)
    for out in (other_body, other_actor, other_cmd):
        assert (out.status, out.reason_codes) == ("REJECTED", ("IDEMPOTENCY_MISMATCH",))
    with db.read() as conn:
        stored = get_command_result(conn, "k1")
    assert stored["status"] == "APPLIED" and stored["response"] == first.model_dump(mode="json")
    assert _site(seeded).context_version == 1 and _count("event") == 0


# ── T51 불변·전이 ──────────────────────────────────────────────

NEW_IMMUTABLE = (
    "command_result",
    "decision",
    "feedback_constraint",
    "event",
    "consent",
    "consultation",
)


@pytest.mark.parametrize("table", NEW_IMMUTABLE)
@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
def test_t51_new_immutable_tables(alpha, table, op):
    pack, _, _, cand, v = alpha
    _reject(pack, cand, v, "TASK_IMMOVABLE", ("C",), ("TIME",))
    _event(pack)
    sql = f"UPDATE {table} SET site_id = site_id" if op == "UPDATE" else f"DELETE FROM {table}"
    with pytest.raises(sqlite3.IntegrityError, match=f"immutable: {table}"), db.write() as tx:
        tx.execute(sql)
    assert _count(table) >= 1


def test_t51_hold_only_active_to_released(seeded):
    hold_id = _event(seeded).result_refs["hold_id"]
    with pytest.raises(sqlite3.IntegrityError, match="hold: no delete"), db.write() as tx:
        tx.execute("DELETE FROM hold")
    with pytest.raises(sqlite3.IntegrityError, match="hold: only"), db.write() as tx:
        tx.execute("UPDATE hold SET scope = 'TASK', task_id = 'E'")
    assert _release(seeded, hold_id).status == "APPLIED"
    with pytest.raises(sqlite3.IntegrityError, match="hold: only"), db.write() as tx:
        tx.execute("UPDATE hold SET status = 'ACTIVE', resolution = NULL")


def test_dispatch_dedupe_and_pending_resume_unique(seeded):
    from app.store.repos.dispatch import register_job

    sid = seeded.site_id
    with db.write() as tx:
        assert register_job(tx, sid, "RECHECK", "RECHECK:ctx9") is True
        assert register_job(tx, sid, "RECHECK", "RECHECK:ctx9") is False
        assert register_job(
            tx, sid, "RESUME_RUN", "RESUME_RUN:r1:1", run_id="r1", wait_generation=1
        )
        # 다른 세대여도 Run당 PENDING RESUME은 1개
        assert not register_job(
            tx, sid, "RESUME_RUN", "RESUME_RUN:r1:2", run_id="r1", wait_generation=2
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.write() as tx:
        tx.execute(
            "INSERT INTO dispatch_job (site_id, kind, payload, dedupe_key)"
            " VALUES (?, 'RESUME_RUN', '{}', 'x')",
            (sid,),
        )
