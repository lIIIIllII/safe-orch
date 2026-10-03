"""Main Observation과 Available Actions 계산.

읽기 전용이다. 매 step DB 사실을 다시 관찰한다: 사건, 충돌 그룹과 그룹별 Unit·이전 결과, Hold, 후보·검증·협의,
하위 Run 결과, 이 Case의 열린 일, 지금 받아들여지는 호출. 계산은 app.agents.casefacts에 있다.
메인의 acting_unit_id는 권한 판정에 쓰지 않으므로 관찰에도 넣지 않는다.
"""

import sqlite3
from dataclasses import dataclass

from app.agents import casefacts
from app.agents import observe as common
from app.agents.observe import budget_remaining, last_guard, recent_steps
from app.agents.specs import main as spec
from app.packs.loader import LoadedPack
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site


@dataclass(frozen=True)
class Observation(common.Observation):
    """Main 관찰. 본 사건의 마지막 순번을 step 예약 때 Run에 적는다."""

    seen_event_seq: int = 0


def build_observation(conn: sqlite3.Connection, pack: LoadedPack, run_id: str) -> Observation:
    run = get_run(conn, run_id)
    site = get_site(conn, pack.site_id)
    if run is None or site is None:
        raise LookupError(f"run {run_id} or site not found")
    steps = [s for s in list_steps(conn, run_id) if s["status"] == "COMPLETED"]
    facts = casefacts.build(conn, pack, run)
    # 서버만 아는 유효성 사실: 사전 확인으로 물을 수 있는 need와 물을 수 없는 사유
    hidden = {k: facts.pop(k) for k in ("ask_needs", "ask_refusals")}
    data = {
        "run": {"run_id": run.run_id, "agent_type": run.agent_type, "goal": spec.GOAL},
        "versions": {
            "context_version": site.context_version,
            "plan_revision": site.plan_revision,
            "wake_seq": run.wake_seq,
        },
        **facts,
        "last_guard": last_guard(steps),
        "recent_steps": recent_steps(steps),
        "budget_remaining": budget_remaining(run, spec.SPEC),
    }
    data["open_skills"] = spec.open_skills(data)
    return Observation(
        run=run,
        versions=(site.context_version, site.plan_revision, run.wake_seq),
        data=data,
        available=spec.available_actions(data),
        spec=spec.SPEC,
        hidden=hidden,
        seen_event_seq=max((e["seq"] for e in data["events"]), default=0),
    )
