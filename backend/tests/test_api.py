"""FastAPI API. TestClient와 스크립트 모델."""

import sqlite3
import uuid
from contextlib import contextmanager

import pytest
from conftest import add_run
from fastapi.testclient import TestClient
from scripted import ScriptedChatModel, solve

from app.api import dev
from app.commands import service
from app.config import get_settings
from app.coordinator.dispatcher import run_until_idle
from app.main import app
from app.store import db
from app.store.repos.runs import list_steps, reserve_step

SITE = "YARD-01"
GATE_SCRIPT = [solve("L0"), solve("L1")]


def _h(actor="supervisor", key=True):
    headers = {"X-Actor": actor} if actor else {}
    if key:
        headers["Idempotency-Key"] = key if isinstance(key, str) else uuid.uuid4().hex
    return headers


def _form(pack, **changes):
    data = pack.new_task.model_dump(
        mode="json", exclude={"requested", "unit_id", "owner_actor_id", "movable"}
    )
    return {**data, **changes}


@pytest.fixture
def client(seeded):
    with TestClient(app) as c:
        yield c


def _state(client, actor="supervisor"):
    res = client.get(f"/api/sites/{SITE}/state", headers=_h(actor, key=False))
    assert res.status_code == 200, res.text
    return res.json()


def _gates(state):
    return {t["task_id"]: (t["gate"], t["reasons"]) for t in state["tasks"]}


def _alpha_ready(client, pack):
    """폼 A(API) → 워커(스크립트 모델: 메인 → Replanning → 협의 Run이 답 대기) → Alpha 검토 대기."""
    res = client.post(f"/api/sites/{SITE}/task-requests", json=_form(pack), headers=_h("planner_a"))
    assert res.status_code == 200, res.text
    run_until_idle(pack, model_factory=lambda: ScriptedChatModel(list(GATE_SCRIPT)))
    # Supervisor가 안을 고른다(API). Supervisor가 아니면 고르지 못하고, 고른 안만 협의가 나간다 (AG-28)
    [first] = _state(client)["review_queue"]
    found = next(c for c in _state(client)["candidates"] if c["candidate_id"] == first)
    assert (found["chosen"], found["approaches"][0]["approach"]) == (False, "MIN_CHANGE")
    body = {"validation_id": found["validation"]["validation_id"]}
    url = f"/api/candidates/{first}/choose"
    assert client.post(url, json=body, headers=_h("planner_a")).status_code == 403
    assert client.post(url, json=body, headers=_h()).status_code == 200
    run_until_idle(pack, model_factory=lambda: ScriptedChatModel(list(GATE_SCRIPT)))
    state = _state(client)
    [alpha] = state["review_queue"]
    cand = next(c for c in state["candidates"] if c["candidate_id"] == alpha)
    return state, cand


def _waive_c(client, cand):
    res = client.post(
        f"/api/consultations/{cand['candidate_id']}/waive",
        json={"task_ids": ["C"], "comment": "작업발판 일정 확인됨"},
        headers=_h(),
    )
    assert res.status_code == 200, res.text


def _approve(client, cand, expected, key=True):
    return client.post(
        f"/api/candidates/{cand['candidate_id']}/approve",
        json={
            "validation_id": cand["validation"]["validation_id"],
            "expected_context_version": expected,
        },
        headers=_h(key=key),
    )


def _event(client, source="rep-1", target=None):
    res = client.post(
        f"/api/sites/{SITE}/events",
        json={
            "source_event_id": source,
            "event_type": "DELAY",
            "text": "도장 준비 지연",
            "target_task_id": target,
        },
        headers=_h("reporter"),
    )
    assert res.status_code == 200, res.text
    return res.json()


# ── 공통 ───────────────────────────────────────────────────────


def test_actor_required_and_unknown(client, seeded):
    res = client.get(f"/api/sites/{SITE}/state")
    assert res.status_code == 401 and res.json()["reason_codes"] == ["ACTOR_REQUIRED"]
    res = client.post(f"/api/sites/{SITE}/task-requests", json=_form(seeded), headers=_h(None))
    assert res.status_code == 401
    res = client.get(f"/api/sites/{SITE}/state", headers={"X-Actor": "mallory"})
    assert (res.status_code, res.json()["reason_codes"]) == (401, ["UNKNOWN_ACTOR"])


def test_idempotency_key_required(client, seeded):
    res = client.post(
        f"/api/sites/{SITE}/task-requests", json=_form(seeded), headers=_h("planner_a", key=False)
    )
    body = res.json()
    assert (res.status_code, body["reason_codes"]) == (400, ["IDEMPOTENCY_KEY_REQUIRED"])
    assert set(body) >= {
        "status",
        "reason_codes",
        "context_version",
        "plan_revision",
        "result_refs",
    }
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM command_result").fetchone()[0] == 0


def test_unknown_field_is_422_in_section12_shape(client, seeded):
    res = client.post(
        f"/api/sites/{SITE}/task-requests",
        json=_form(seeded, approved=True),
        headers=_h("planner_a"),
    )
    body = res.json()
    assert res.status_code == 422
    assert (body["status"], body["reason_codes"], body["context_version"]) == (
        "REJECTED",
        ["INVALID_BODY"],
        0,
    )
    assert body["detail"] and body["result_refs"] == {}


def test_status_code_mapping(client, seeded, monkeypatch):
    url = f"/api/sites/{SITE}/task-requests"
    first = client.post(url, json=_form(seeded), headers=_h("planner_a", key="k-1"))
    again = client.post(url, json=_form(seeded), headers=_h("planner_a", key="k-1"))
    assert (first.status_code, first.json()["status"]) == (200, "APPLIED")
    assert (again.status_code, again.json()["status"]) == (200, "REPLAYED")
    forbidden = client.post(url, json=_form(seeded, task_id="F"), headers=_h("supervisor"))
    assert (forbidden.status_code, forbidden.json()["reason_codes"]) == (403, ["NOT_AUTHORIZED"])
    replay_forbidden = client.post(
        url, json=_form(seeded, task_id="F"), headers=_h("supervisor", key="k-403")
    )
    replay_again = client.post(
        url, json=_form(seeded, task_id="F"), headers=_h("supervisor", key="k-403")
    )
    assert replay_forbidden.status_code == replay_again.status_code == 403
    exists = client.post(url, json=_form(seeded, task_id="B"), headers=_h("planner_a"))
    assert (exists.status_code, exists.json()["reason_codes"]) == (409, ["TASK_ID_EXISTS"])
    mismatch = client.post(url, json=_form(seeded, duration=20), headers=_h("planner_a", key="k-1"))
    assert (mismatch.status_code, mismatch.json()["reason_codes"]) == (
        409,
        ["IDEMPOTENCY_MISMATCH"],
    )
    missing = client.post(
        "/api/candidates/cand_nope/approve",
        json={"validation_id": "v", "expected_context_version": 0},
        headers=_h(),
    )
    assert (missing.status_code, missing.json()["reason_codes"]) == (404, ["CANDIDATE_NOT_FOUND"])
    wrong_site = client.post(
        "/api/sites/OTHER/task-requests", json=_form(seeded), headers=_h("planner_a")
    )
    assert (wrong_site.status_code, wrong_site.json()["reason_codes"]) == (404, ["SITE_NOT_FOUND"])

    @contextmanager
    def busy():
        raise db.StoreBusyError("database is locked")
        yield

    monkeypatch.setattr(service.db, "write", busy)
    res = client.post(url, json=_form(seeded, task_id="G"), headers=_h("planner_a"))
    assert (res.status_code, res.headers.get("Retry-After")) == (503, "1")
    assert res.json()["status"] == "RETRYABLE_ERROR"


# ── API로 게이트 경로 ──────────────────────────────────────────


def test_gate_path_via_api(client, seeded, main_on):
    state, alpha = _alpha_ready(client, seeded)
    assert alpha["display_status"] == "OPEN" and alpha["validation"]["status"] == "PASS"
    assert {i["task_id"]: i["item_status"] for i in alpha["consultation"]["items"]} == {
        "A": "COVERED",
        "C": "PENDING",
    }
    assert alpha["solver"]["scope_level"] == "L1" and alpha["run_id"]
    assert state["runs"][0]["status"] == "WAITING_HUMAN"

    _waive_c(client, alpha)
    res = _approve(client, alpha, state["site"]["context_version"])
    assert (res.status_code, res.json()["plan_revision"]) == (200, 1)

    # 승인 결과로 메인이 깨어나 통지를 부르고 끝낸다
    run_until_idle(seeded, model_factory=lambda: ScriptedChatModel(list(GATE_SCRIPT)))
    after = _state(client)
    assert (
        after["plan"]["plan_revision"] == 1
        and after["plan"]["candidate_id"] == alpha["candidate_id"]
    )
    assert {r["agent_type"]: r["status"] for r in after["runs"]} == {
        "MAIN": "SUCCEEDED",
        "REPLANNING": "SUCCEEDED",
        "COORDINATION": "SUCCEEDED",
    }
    assert {g for g, _ in _gates(after).values()} == {"ALLOW"}
    committed = next(c for c in after["candidates"] if c["candidate_id"] == alpha["candidate_id"])
    assert committed["display_status"] == "COMMITTED" and after["review_queue"] == []

    run = client.get(f"/api/runs/{alpha['run_id']}", headers=_h(key=False)).json()
    steps = client.get(f"/api/runs/{alpha['run_id']}/steps", headers=_h(key=False)).json()
    assert run["status"] == "SUCCEEDED" and [s["step_no"] for s in steps] == [1, 2, 3]
    assert client.get("/api/runs/run_nope", headers=_h(key=False)).status_code == 404


# ── Gate·후보 표시 ─────────────────────────────────────────────


def test_gate_allow_stale_and_hold(client, seeded):
    assert {g for g, _ in _gates(_state(client)).values()} == {"ALLOW"}  # R0, context 0
    client.post(f"/api/sites/{SITE}/task-requests", json=_form(seeded), headers=_h("planner_a"))
    gates = _gates(_state(client))
    assert gates["A"] == ("STALE", ["NOT_IN_PLAN", "CONTEXT_CHANGED"])
    assert gates["B"] == ("STALE", ["CONTEXT_CHANGED"])
    hold_id = _event(client, target="E")["result_refs"]["hold_id"]
    gates = _gates(_state(client))
    assert gates["E"] == ("HOLD", [f"HOLD:{hold_id}", "CONTEXT_CHANGED"])  # 겹치면 HOLD
    assert gates["B"][0] == "STALE"
    site_hold = _event(client, source="rep-2")["result_refs"]["hold_id"]
    assert all(g == "HOLD" for g, _ in _gates(_state(client)).values())
    assert site_hold


def test_stale_candidate_display(client, seeded, main_on):
    _, alpha = _alpha_ready(client, seeded)
    _event(client)
    state = _state(client)
    stale = next(c for c in state["candidates"] if c["candidate_id"] == alpha["candidate_id"])
    assert stale["display_status"] == "STALE"
    assert (
        stale["validation"]["display_status"] == "STALE" and stale["validation"]["status"] == "PASS"
    )
    assert stale["consultation"]["status"] == "CANCELLED" and state["review_queue"] == []
    # 신고는 열린 전문 Agent Run만 무효로 만든다. 메인은 남는다
    assert {r["agent_type"]: r["status"] for r in state["runs"]} == {
        "MAIN": "WAITING_HUMAN",
        "REPLANNING": "SUCCEEDED",
        "COORDINATION": "STALE",
    }


# ── Event·Hold·T18 ─────────────────────────────────────────────


def test_t18_event_hold_list_and_release_via_api(client, seeded, main_on):
    state, alpha = _alpha_ready(client, seeded)
    _waive_c(client, alpha)
    expected = state["site"]["context_version"]  # 검토 화면을 연 시점
    event = _event(client)
    holds = _state(client)["holds"]
    assert [(h["hold_id"], h["scope"], h["event_type"]) for h in holds] == [
        (event["result_refs"]["hold_id"], "SITE", "DELAY")
    ]
    res = _approve(client, alpha, expected)
    assert (res.status_code, res.json()["reason_codes"]) == (409, ["STALE_CONTEXT", "HOLD_ACTIVE"])

    ctx = _state(client)["site"]["context_version"]
    res = client.post(
        f"/api/holds/{event['result_refs']['hold_id']}/release",
        json={"resolution": "NO_CHANGE", "expected_context_version": ctx},
        headers=_h(),
    )
    assert res.status_code == 200 and _state(client)["holds"] == []
    bad = client.post(
        "/api/holds/h/release",
        json={"resolution": "MAYBE", "expected_context_version": 0},
        headers=_h(),
    )
    assert bad.status_code == 422


# ── cancel ─────────────────────────────────────────────────────


def test_cancel_waiting_run(client, seeded, main_on):
    state, _ = _alpha_ready(client, seeded)
    runs = {r["agent_type"]: r["run_id"] for r in state["runs"]}
    url = f"/api/runs/{runs['MAIN']}/cancel"
    assert client.post(url, headers=_h("planner_a")).status_code == 403
    res = client.post(url, headers=_h())
    assert (
        res.status_code == 200 and res.json()["result_refs"]["previous_status"] == "WAITING_HUMAN"
    )
    run = client.get(f"/api/runs/{runs['MAIN']}", headers=_h(key=False)).json()
    assert (run["status"], run["end_reason"]) == ("CANCELLED", "CANCELLED_BY:supervisor")
    # 메인을 취소하면 답을 기다리던 하위 Run도 같이 끝난다
    child = client.get(f"/api/runs/{runs['COORDINATION']}", headers=_h(key=False)).json()
    assert (child["status"], child["end_reason"]) == ("CANCELLED", "PARENT_ENDED")
    again = client.post(url, headers=_h())
    assert (again.status_code, again.json()["reason_codes"]) == (409, ["RUN_NOT_ACTIVE"])
    missing = client.post("/api/runs/run_nope/cancel", headers=_h())
    assert (missing.status_code, missing.json()["reason_codes"]) == (404, ["RUN_NOT_FOUND"])


def test_cancel_aborts_reserved_step(client, with_a):
    add_run(with_a, "run_dead", input_ref={})
    with db.write() as tx:  # 그래프 실행 중 프로세스가 죽어 RESERVED로 남은 step
        reserve_step(tx, "run_dead", (1, 0, 0), "goal", {}, [])
    res = client.post("/api/runs/run_dead/cancel", headers=_h())
    assert res.status_code == 200
    with db.read() as conn:
        [step] = list_steps(conn, "run_dead")
    assert (step["status"], step["abort_reason"]) == ("ABORTED", "CANCELLED")


# ── /dev/reset ─────────────────────────────────────────────────


def _reset(client, confirm="RESET safe_orch", query=""):
    return client.post(f"/api/dev/reset{query}", json={"confirm": confirm}, headers=_h(key=False))


def test_reset_requires_demo_mode(seeded, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    with TestClient(app) as c:
        res = _reset(c)
    assert res.status_code == 404


def test_reset_confirm_and_pack(client):
    assert (_reset(client, "yes").status_code, _reset(client, "yes").json()["reason_codes"]) == (
        400,
        ["CONFIRM_REQUIRED"],
    )
    res = _reset(client, query="?pack=smart_factory")
    assert (res.status_code, res.json()["reason_codes"]) == (400, ["PACK_NOT_SUPPORTED"])


def test_reset_returns_to_r0_and_restarts_worker(seeded, monkeypatch):
    monkeypatch.setenv("DISPATCH_WORKER", "true")
    monkeypatch.setenv("DISPATCH_POLL_S", "0.05")
    get_settings.cache_clear()
    with TestClient(app) as c:
        _leave_records(c, seeded)
        old = app.state.worker
        res = _reset(c, query="?pack=shipyard")
        new = app.state.worker
        assert res.status_code == 200 and res.json()["context_version"] == 0
        assert new is not old and new.alive and not old.alive
        state = _state(c)
        assert (state["site"]["context_version"], state["plan"]["plan_revision"]) == (0, 0)
        assert "A" not in {t["task_id"] for t in state["tasks"]}
        assert state["candidates"] == [] and state["runs"] == []
        with db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM command_result").fetchone()[0] == 0
            assert conn.execute("SELECT command FROM audit").fetchall() == [("SEED",)]


def _leave_records(client, pack):
    """워커가 켜진 앱에 기록을 남긴다(불변 테이블·FK가 있는 상태에서 reset을 확인)."""
    res = client.post(f"/api/sites/{SITE}/task-requests", json=_form(pack), headers=_h("planner_a"))
    assert res.status_code == 200
    _event(client)


def test_reset_worker_busy(seeded, monkeypatch):
    monkeypatch.setenv("DISPATCH_WORKER", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(dev, "RESET_WORKER_WAIT_S", 0.1)
    with TestClient(app) as c:
        client_form = c.post(
            f"/api/sites/{SITE}/task-requests", json=_form(seeded), headers=_h("planner_a")
        )
        worker = app.state.worker
        assert worker._lock.acquire(timeout=5)  # 워커가 job을 처리 중인 것처럼
        try:
            res = _reset(c)
        finally:
            worker._lock.release()
        assert (res.status_code, res.json()["reason_codes"]) == (409, ["WORKER_BUSY"])
        assert app.state.worker is worker and worker.alive
        assert client_form.status_code == 200 and _state(c)["site"]["context_version"] == 1


def test_rebuild_schema_is_atomic_on_failure(seeded, monkeypatch):
    def broken():
        return [
            "CREATE TABLE schema_meta (schema_version INTEGER NOT NULL)",
            "CREATE TABLE broken (",
        ]

    monkeypatch.setattr(db, "schema_statements", broken)
    with pytest.raises(sqlite3.OperationalError), db.write() as tx:
        db.rebuild_schema(tx)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM site").fetchone()[0] == 1
