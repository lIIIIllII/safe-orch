"""agent_run·agent_step·solver_job 기록과 조회 (설계서 §5.1·§11.2–§11.4, 부록 A.16).

상태 전이는 조건부 UPDATE로 한다. 종료 Run 부활·카운터 감소·완료 step 변경은 트리거도 막는다.
"""

import sqlite3
from typing import Any

from app.domain.models import AgentRun
from app.store.repos._rows import dumps, loads, rows

ACTIVE = ("RUNNING", "WAITING_HUMAN")


def insert_run(tx: sqlite3.Connection, site_id: str, run: AgentRun) -> None:
    tx.execute(
        "INSERT INTO agent_run (run_id, site_id, agent_type, case_id, acting_actor_id,"
        " acting_unit_id, input_ref, exec_contract_version, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run.run_id,
            site_id,
            run.agent_type,
            run.case_id,
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
) -> int | None:
    """Run이 RUNNING이면 새 step_no를 발급해 RESERVED step을 만들고 step·LLM 시도를 1씩 차감한다.

    observed = (context_version, plan_revision, wake_seq). RUNNING이 아니면 None.
    """
    row = tx.execute(
        "UPDATE agent_run SET last_step_no = last_step_no + 1, steps_used = steps_used + 1,"
        " llm_attempts_used = llm_attempts_used + 1"
        " WHERE run_id = ? AND status = 'RUNNING' RETURNING last_step_no, site_id",
        (run_id,),
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
    }
    for key, amount in amounts.items():
        if amount:
            col = cols[key]
            tx.execute(f"UPDATE agent_run SET {col} = {col} + ? WHERE run_id = ?", (amount, run_id))


def enter_wait(tx: sqlite3.Connection, run_id: str, wait_kind: str, wait_ref: str) -> bool:
    """RUNNING → WAITING_HUMAN, wait_generation += 1. wake_seq 재확인은 D5."""
    cur = tx.execute(
        "UPDATE agent_run SET status = 'WAITING_HUMAN', wait_kind = ?, wait_ref = ?,"
        " wait_generation = wait_generation + 1 WHERE run_id = ? AND status = 'RUNNING'",
        (wait_kind, wait_ref, run_id),
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


def tried_spec_hashes(conn: sqlite3.Connection, site_id: str) -> set[str]:
    """site에서 이미 시도한 실효 SearchSpec hash (RESERVED·REGISTERED, 부록 A.16)."""
    return {
        r["hash"]
        for r in rows(
            conn,
            "SELECT s.hash FROM solver_job j JOIN search_spec s"
            " ON s.search_spec_id = j.search_spec_id"
            " WHERE j.site_id = ? AND j.status IN ('RESERVED', 'REGISTERED')",
            (site_id,),
        )
    }


def list_attempts(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """이 Run의 Solver 시도 (Observation attempts)."""
    found = rows(
        conn,
        "SELECT j.step_no, j.status AS job_status, s.scope_level, s.hash AS spec_hash,"
        " r.stage1, r.stage2, c.candidate_id FROM solver_job j"
        " JOIN search_spec s ON s.search_spec_id = j.search_spec_id"
        " LEFT JOIN solver_result r ON r.solver_result_id = j.solver_result_id"
        " LEFT JOIN candidate c ON c.solver_result_id = j.solver_result_id"
        " WHERE j.run_id = ? ORDER BY j.step_no",
        (run_id,),
    )
    out = []
    for r in found:
        s1, s2 = loads(r["stage1"]), loads(r["stage2"])
        out.append(
            {
                "step_no": r["step_no"],
                "job_status": r["job_status"],
                "scope_level": r["scope_level"],
                "spec_hash": r["spec_hash"],
                "stage1": None
                if s1 is None
                else {"status": s1["status"], "changed": s1["changed"]},
                "stage2": None if s2 is None else {"status": s2["status"], "delay": s2["delay"]},
                "candidate_id": r["candidate_id"],
            }
        )
    return out


def run_for_solver_result(conn: sqlite3.Connection, solver_result_id: str) -> str | None:
    """후보 → Run 연결: candidate.solver_result_id → solver_job (부록 A.16)."""
    row = conn.execute(
        "SELECT run_id FROM solver_job WHERE solver_result_id = ?", (solver_result_id,)
    ).fetchone()
    return None if row is None else row[0]


def has_open_case(conn: sqlite3.Connection, site_id: str) -> bool:
    """열린 Case = REPLANNING Run이 RUNNING 또는 WAITING_HUMAN (부록 A.16)."""
    return bool(
        conn.execute(
            "SELECT 1 FROM agent_run WHERE site_id = ? AND agent_type = 'REPLANNING'"
            " AND status IN ('RUNNING', 'WAITING_HUMAN') LIMIT 1",
            (site_id,),
        ).fetchone()
    )


def stale_active_runs(tx: sqlite3.Connection, site_id: str, end_reason: str) -> list[str]:
    """RUNNING·WAITING_HUMAN Run을 모두 STALE로 (Event 접수, §10). 바꾼 run_id 목록."""
    return [
        r[0]
        for r in tx.execute(
            "UPDATE agent_run SET status = 'STALE', end_reason = ?, wait_kind = NULL,"
            " wait_ref = NULL WHERE site_id = ? AND status IN ('RUNNING', 'WAITING_HUMAN')"
            " RETURNING run_id",
            (end_reason, site_id),
        ).fetchall()
    ]
