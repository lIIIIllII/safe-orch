"""agent_run·agent_step·solver_job 기록과 조회.

상태 전이는 조건부 UPDATE로 한다. 종료 Run 부활·카운터 감소·완료 step 변경은 트리거도 막는다.
"""

import sqlite3
from typing import Any

from app.domain.models import AgentRun
from app.store.repos._rows import dumps, loads, rows

ACTIVE = ("RUNNING", "WAITING_HUMAN")
# 열린 Case를 이루는 agent_type: 메인 Run 하나 = Case 하나. 하위 Run은 메인의 Case를 쓴다.
CASE_AGENT_TYPES = ("MAIN",)


def insert_run(tx: sqlite3.Connection, site_id: str, run: AgentRun) -> None:
    tx.execute(
        "INSERT INTO agent_run (run_id, site_id, agent_type, case_id, parent_run_id,"
        " acting_actor_id, acting_unit_id, input_ref, exec_contract_version, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run.run_id,
            site_id,
            run.agent_type,
            run.case_id,
            run.parent_run_id,
            run.acting_actor_id,
            run.acting_unit_id,
            dumps(run.input_ref),
            run.exec_contract_version,
            run.status,
        ),
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> AgentRun | None:
    found = rows(conn, "SELECT * FROM agent_run WHERE run_id = ?", (run_id,))
    if not found:
        return None
    r = found[0]
    r.pop("site_id")
    r["input_ref"] = loads(r["input_ref"])
    return AgentRun(**r)


def reserve_step(
    tx: sqlite3.Connection,
    run_id: str,
    observed: tuple[int, int, int],
    goal: str,
    observation: dict[str, Any],
    available_actions: list[dict[str, Any]],
    seen_event_seq: int = 0,
) -> int | None:
    """Run이 RUNNING이면 새 step_no를 발급해 RESERVED step을 만들고 step·LLM 시도를 1씩 차감한다.

    observed = (context_version, plan_revision, wake_seq). 관찰한 wake_seq를 handled_wake_seq로,
    관찰에서 본 마지막 사건 순번을 last_event_seq로 기록한다. RUNNING이 아니면 None.
    """
    row = tx.execute(
        "UPDATE agent_run SET last_step_no = last_step_no + 1, steps_used = steps_used + 1,"
        " llm_attempts_used = llm_attempts_used + 1,"
        " handled_wake_seq = MAX(handled_wake_seq, ?), last_event_seq = MAX(last_event_seq, ?)"
        " WHERE run_id = ? AND status = 'RUNNING' RETURNING last_step_no, site_id",
        (observed[2], seen_event_seq, run_id),
    ).fetchone()
    if row is None:
        return None
    step_no, site_id = row
    tx.execute(
        "INSERT INTO agent_step (run_id, step_no, site_id, status, observed_context_version,"
        " observed_plan_revision, observed_wake_seq, goal, observation, available_actions)"
        " VALUES (?, ?, ?, 'RESERVED', ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            step_no,
            site_id,
            observed[0],
            observed[1],
            observed[2],
            goal,
            dumps(observation),
            dumps(available_actions),
        ),
    )
    return step_no


def get_step(conn: sqlite3.Connection, run_id: str, step_no: int) -> dict[str, Any] | None:
    found = list_steps(conn, run_id, step_no=step_no)
    return found[0] if found else None


def list_steps(
    conn: sqlite3.Connection, run_id: str, *, step_no: int | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM agent_step WHERE run_id = ?"
    params: tuple[Any, ...] = (run_id,)
    if step_no is not None:
        sql += " AND step_no = ?"
        params += (step_no,)
    found = rows(conn, sql + " ORDER BY step_no", params)
    for r in found:
        for col in (
            "observation",
            "available_actions",
            "action",
            "tool_result",
            "guard",
            "state_changes",
            "budget_remaining",
        ):
            r[col] = loads(r[col])
    return found


def complete_step(
    tx: sqlite3.Connection,
    run_id: str,
    step_no: int,
    *,
    action: dict[str, Any] | None,
    decision_summary: str | None,
    tool_result: dict[str, Any] | None,
    guard: dict[str, Any],
    state_changes: dict[str, Any],
    result_kind: str,
    budget_remaining: dict[str, Any],
    model_id: str | None,
    prompt_version: str | None,
    llm_attempts: int,
) -> None:
    cur = tx.execute(
        "UPDATE agent_step SET status = 'COMPLETED', action = ?, decision_summary = ?,"
        " tool_result = ?, guard = ?, state_changes = ?, result_kind = ?, budget_remaining = ?,"
        " model_id = ?, prompt_version = ?, llm_attempts = ?"
        " WHERE run_id = ? AND step_no = ? AND status = 'RESERVED'",
        (
            None if action is None else dumps(action),
            decision_summary,
            None if tool_result is None else dumps(tool_result),
            dumps(guard),
            dumps(state_changes),
            result_kind,
            dumps(budget_remaining),
            model_id,
            prompt_version,
            llm_attempts,
            run_id,
            step_no,
        ),
    )
    if cur.rowcount != 1:
        raise LookupError(f"step {run_id}:{step_no} is not RESERVED")


def abort_step(tx: sqlite3.Connection, run_id: str, step_no: int, reason: str) -> None:
    tx.execute(
        "UPDATE agent_step SET status = 'ABORTED', abort_reason = ?"
        " WHERE run_id = ? AND step_no = ? AND status = 'RESERVED'",
        (reason, run_id, step_no),
    )


def charge(tx: sqlite3.Connection, run_id: str, **amounts: float) -> None:
    """Budget 카운터를 더한다(감소 없음). 예: charge(tx, rid, solver_calls=1, solver_seconds=10)."""
    cols = {
        "llm_attempts": "llm_attempts_used",
        "human_rounds": "human_rounds_used",
        "solver_calls": "solver_calls_used",
        "solver_seconds": "solver_seconds_used",
        "agent_calls": "agent_calls_used",
    }
    for key, amount in amounts.items():
        if amount:
            col = cols[key]
            tx.execute(f"UPDATE agent_run SET {col} = {col} + ? WHERE run_id = ?", (amount, run_id))


def enter_wait(
    tx: sqlite3.Connection, run_id: str, wait_kind: str, wait_ref: str, observed_wake_seq: int
) -> bool:
    """대기 진입 재확인: 관찰 이후 새 변화(wake_seq)가 없을 때만 RUNNING → WAITING_HUMAN,
    wait_generation += 1. 변화가 있으면 False(호출한 쪽이 NEW_CHANGE_BEFORE_WAIT로 다시 관찰)."""
    cur = tx.execute(
        "UPDATE agent_run SET status = 'WAITING_HUMAN', wait_kind = ?, wait_ref = ?,"
        " wait_generation = wait_generation + 1"
        " WHERE run_id = ? AND status = 'RUNNING' AND wake_seq <= ?",
        (wait_kind, wait_ref, run_id, observed_wake_seq),
    )
    return cur.rowcount == 1


def end_run(
    tx: sqlite3.Connection,
    run_id: str,
    status: str,
    end_reason: str,
    from_statuses: tuple[str, ...] = ("RUNNING",),
) -> bool:
    """조건부 종료. from_statuses에 있을 때만 바꾼다."""
    marks = ", ".join("?" for _ in from_statuses)
    cur = tx.execute(
        "UPDATE agent_run SET status = ?, end_reason = ?, wait_kind = NULL, wait_ref = NULL"
        f" WHERE run_id = ? AND status IN ({marks})",
        (status, end_reason, run_id, *from_statuses),
    )
    return cur.rowcount == 1


# ── solver_job ────────────────────────────────────────────────


def insert_solver_job(
    tx: sqlite3.Connection, site_id: str, run_id: str, step_no: int, search_spec_id: str
) -> None:
    tx.execute(
        "INSERT INTO solver_job (run_id, step_no, site_id, search_spec_id, status)"
        " VALUES (?, ?, ?, ?, 'RESERVED')",
        (run_id, step_no, site_id, search_spec_id),
    )


def finish_solver_job(
    tx: sqlite3.Connection,
    run_id: str,
    step_no: int,
    status: str,
    solver_result_id: str | None = None,
) -> None:
    tx.execute(
        "UPDATE solver_job SET status = ?, solver_result_id = ?"
        " WHERE run_id = ? AND step_no = ? AND status = 'RESERVED'",
        (status, solver_result_id, run_id, step_no),
    )


def tried_search_keys(conn: sqlite3.Connection, site_id: str, case_id: str) -> set[str]:
    """이 Case에서 이미 시도한 실효 탐색 키 (RESERVED·REGISTERED). Case가 바뀌면 다시 계산할 수 있다 (CV-13)."""
    return {
        r["search_key"]
        for r in rows(
            conn,
            "SELECT s.search_key FROM solver_job j JOIN search_spec s"
            " ON s.search_spec_id = j.search_spec_id"
            " JOIN agent_run r ON r.run_id = j.run_id"
            " WHERE j.site_id = ? AND r.case_id = ? AND j.status IN ('RESERVED', 'REGISTERED')",
            (site_id, case_id),
        )
    }


def list_attempts(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """이 Run이 속한 Case의 Solver 시도 (Observation attempts). 같은 Case의 앞 Run 것도 넣는다:
    메인이 재계획을 다시 부르면 새 Run이고, 미시도 판정도 Case 단위다 (CV-13)."""
    found = rows(
        conn,
        "SELECT j.run_id, j.step_no, j.status AS job_status, s.scope_level, s.search_key,"
        " s.resource_alternatives, r.stage1, r.stage2, c.candidate_id FROM solver_job j"
        " JOIN search_spec s ON s.search_spec_id = j.search_spec_id"
        " JOIN agent_run a ON a.run_id = j.run_id"
        " LEFT JOIN solver_result r ON r.solver_result_id = j.solver_result_id"
        " LEFT JOIN candidate c ON c.solver_result_id = j.solver_result_id"
        " WHERE a.case_id = (SELECT case_id FROM agent_run WHERE run_id = ?)"
        " ORDER BY j.rowid",
        (run_id,),
    )
    out = []
    for r in found:
        s1, s2 = loads(r["stage1"]), loads(r["stage2"])
        out.append(
            {
                "step_no": r["step_no"],
                # 이 Run의 시도인가(아니면 같은 Case의 앞 Run)
                "this_run": r["run_id"] == run_id,
                "job_status": r["job_status"],
                "scope_level": r["scope_level"],
                "try_resources": loads(r["resource_alternatives"]),  # TRY 시도
                "search_key": r["search_key"],
                "stage1": None
                if s1 is None
                else {"status": s1["status"], "changed": s1["changed"]},
                "stage2": None if s2 is None else {"status": s2["status"], "delay": s2["delay"]},
                "candidate_id": r["candidate_id"],
            }
        )
    return out


def run_for_solver_result(conn: sqlite3.Connection, solver_result_id: str) -> str | None:
    """후보 → Run 연결: candidate.solver_result_id → solver_job."""
    row = conn.execute(
        "SELECT run_id FROM solver_job WHERE solver_result_id = ?", (solver_result_id,)
    ).fetchone()
    return None if row is None else row[0]


def has_open_case(conn: sqlite3.Connection, site_id: str) -> bool:
    """열린 Case = CASE_AGENT_TYPES Run이 RUNNING 또는 WAITING_HUMAN."""
    marks = ", ".join("?" for _ in CASE_AGENT_TYPES)
    return bool(
        conn.execute(
            f"SELECT 1 FROM agent_run WHERE site_id = ? AND agent_type IN ({marks})"
            " AND status IN ('RUNNING', 'WAITING_HUMAN') LIMIT 1",
            (site_id, *CASE_AGENT_TYPES),
        ).fetchone()
    )


def list_active_runs(conn: sqlite3.Connection, site_id: str) -> list[AgentRun]:
    """RUNNING·WAITING_HUMAN Run (생성 순)."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT run_id FROM agent_run WHERE site_id = ? AND status IN (?, ?) ORDER BY rowid",
            (site_id, *ACTIVE),
        )
    ]
    return [run for rid in ids if (run := get_run(conn, rid)) is not None]


def set_contract_version(tx: sqlite3.Connection, run_id: str, version: str) -> None:
    """사건 트랜잭션에서 만든 Run은 실행 계약 버전을 첫 호출 때 적는다."""
    tx.execute(
        "UPDATE agent_run SET exec_contract_version = ? WHERE run_id = ?"
        " AND exec_contract_version = ''",
        (version, run_id),
    )


def mark_restart(tx: sqlite3.Connection, run_id: str) -> int | None:
    """기동 복구: RUNNING Run의 예약만 된 step·SolverJob을 ABORTED로 두고 restart_count += 1 (ST-19).

    새 restart_count를 돌려준다. RUNNING이 아니면 None.
    """
    row = tx.execute(
        "UPDATE agent_run SET restart_count = restart_count + 1"
        " WHERE run_id = ? AND status = 'RUNNING' RETURNING restart_count",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    abort_reserved(tx, run_id, "RESTART")
    return row[0]


def abort_reserved(tx: sqlite3.Connection, run_id: str, reason: str) -> None:
    """RESERVED step과 solver_job을 ABORTED로 (Run ERROR·취소)."""
    tx.execute(
        "UPDATE agent_step SET status = 'ABORTED', abort_reason = ?"
        " WHERE run_id = ? AND status = 'RESERVED'",
        (reason, run_id),
    )
    tx.execute(
        "UPDATE solver_job SET status = 'ABORTED' WHERE run_id = ? AND status = 'RESERVED'",
        (run_id,),
    )
