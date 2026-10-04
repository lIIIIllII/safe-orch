"""Schedule Review Action 실행기.

ToolGateway.execute 안에서만 불린다(도구 실행 경로는 하나). 모든 Action은 tx 하나다:
begin_step(활성·차감·STALE_OBSERVATION·재관찰·허용 판정) → 효과 → step 완료.
SUBMIT_BUNDLES는 묶음이 최소 묶음의 합인지만 검사하고(모든 최소 묶음이 정확히 한 번, 없는 ID 없음),
통과하면 그때의 Snapshot과 함께 묶음안을 불변 기록으로 남기고 같은 tx에서 Run을 끝낸다 (AG-36·ST-07). 걸리면
거절하고 Run은 계속된다. 서버는 묶음을 대신 정하지 않는다. RETURN_RESULT는 막힘(BLOCKED)뿐이다.
배치·점수 계산, 고정·값 고치기, 사람에게 묻는 함수는 없다.
"""

import sqlite3
from typing import Any

from app.agents.observe import Observation
from app.agents.specs import schedule_review as spec
from app.agents.tool_gateway import ACCEPTED, REJECTED, ToolGateway, _Parsed
from app.agents.types import GatewayResult, StepMeta
from app.domain.bundles import check_bundles
from app.domain.ids import new_id
from app.store import db
from app.store.repos.bundles import insert_bundle_plan
from app.store.repos.site import get_site
from app.store.repos.snapshots import create_snapshot


class ScheduleReviewExecutor:
    def __init__(self, gateway: ToolGateway):
        self.pack = gateway.pack
        # 공통 도우미 (ToolGateway)
        self._complete = gateway._complete
        self.begin_step = gateway.begin_step
        self.return_result = gateway.return_result

    def run(self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed) -> GatewayResult:
        action = parsed.action
        assert action is not None
        with db.write() as tx:
            rejected, obs = self.begin_step(
                tx, run_id, step_no, meta, parsed, lambda o: self._permitted(o, action)
            )
            if rejected is not None:
                return rejected
            assert obs is not None
            if isinstance(action, spec.SubmitBundles):
                return self._submit(tx, run_id, step_no, meta, parsed, obs, action)
            assert isinstance(action, spec.ReturnResult)
            return self.return_result(tx, run_id, step_no, meta, parsed, {})

    def _permitted(self, obs: Observation, action: Any) -> bool:
        available = obs.available
        if isinstance(action, spec.ReturnResult):
            return action.status in available.get("RETURN_RESULT", {}).get("status", [])
        return "SUBMIT_BUNDLES" in available

    def _submit(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.SubmitBundles,
    ) -> GatewayResult:
        groups = {g["group_id"]: g for g in obs.data["groups"]}
        asked = [b.group_ids for b in action.bundles]
        broken = check_bundles(list(groups), asked)
        if broken:
            self._complete(
                tx,
                run_id,
                step_no,
                meta,
                parsed,
                verdict=REJECTED,
                reason="BUNDLES_INVALID",
                result_kind="REJECTED",
                tool_result={"violations": broken},
            )
            return GatewayResult("REJECTED", "BUNDLES_INVALID")
        site = get_site(tx, self.pack.site_id)
        assert site is not None
        bundles = []
        for i, b in enumerate(action.bundles, start=1):
            members = [groups[gid] for gid in b.group_ids]
            alone = [g["group_id"] for g in members if g["human_only"]]
            bundles.append(
                {
                    "bundle_id": f"B{i}",
                    "group_ids": list(b.group_ids),
                    "task_ids": sorted({t for g in members for t in g["task_ids"]}),
                    # 사람만 풀 수 있는 최소 묶음. 모두 그러면 이 묶음은 풀이로 보내지 않는다
                    "human_only_group_ids": alone,
                    "human_only": len(alone) == len(members),
                    "quoted_note": b.note,  # 모델 문장(인용)
                }
            )
        snapshot = create_snapshot(tx, self.pack.site_id, self.pack)
        bundle_plan_id = new_id("bp")
        insert_bundle_plan(
            tx,
            self.pack.site_id,
            bundle_plan_id,
            snapshot.snapshot_id,
            obs.run.case_id,
            run_id,
            step_no,
            site.context_version,
            site.plan_revision,
            {
                "groups": obs.data["groups"],
                "relations": obs.data["relations"],
                "tasks": obs.data["tasks"],
                "bundles": bundles,
                "quoted_opinion": action.opinion,  # 모델 문장(인용)
            },
        )
        # 메인에게 돌려주는 결과: 묶음안의 ID와 묶음 구조(서버 값만. 메모와 의견은 넣지 않는다, ST-25)
        result = {
            "status": "DONE",
            "paths": [],
            "bundle_plan": {
                "bundle_plan_id": bundle_plan_id,
                "bundles": [{k: v for k, v in b.items() if k != "quoted_note"} for b in bundles],
            },
        }
        outcome = GatewayResult("DONE", None, "SUCCEEDED", f"BUNDLES_SUBMITTED:{bundle_plan_id}")
        self._complete(
            tx,
            run_id,
            step_no,
            meta,
            parsed,
            verdict=ACCEPTED,
            reason=None,
            result_kind="DONE",
            tool_result=result,
            state_changes={"bundle_plan_id": bundle_plan_id, "snapshot_id": snapshot.snapshot_id},
            end=outcome,
        )
        return outcome
