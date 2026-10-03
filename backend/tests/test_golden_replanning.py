"""골든 테스트: Replanning 기록과 모델 입력이 고정한 값과 같은지 확인한다.

대상은 Run 행, AgentStep 행(created_at 제외), Gateway CommandResult(created_at 제외), 모델이 받은
입력(System·Human 메시지, 바인딩한 도구, bind 인자)이다.
uuid4 ID와 hash는 실행마다 달라지므로 등장 순서대로 치환한 뒤 hash한다.
자원 모델 확장 ①(replanning-p10, 자원 조회 제외 사유 모양)에서 다시 만들었다. step 순서·Action·결과는 그 전과 같다.
"""

import json
import re
import uuid

import httpx
import openai
from langchain_core.messages import AIMessage
from scripted import ScriptedChatModel, call, escalate, solve

from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    approve_and_commit,
    reject_candidate,
)
from app.commands.messages import ReplyRequest, reply_message
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.canonical import canonical_hash
from app.store import db
from app.store.repos.records import list_validations
from app.store.repos.site import get_site

PREFIXED_ID = re.compile(r"(?<![0-9a-z_])([a-z]+)_[0-9a-f]{32}(?![0-9a-f])")
BARE_ID = re.compile(r"(?<![0-9a-f_])[0-9a-f]{32}(?![0-9a-f])")
HASH = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
# 자원 모델 확장 ① 뒤 다시 만든 값
GOLDEN = {
    "plan_b": "6b0ea1b3fb784a5e6e2e8ca68f709bcd54bacf3d57c9fa3cedb3dbdd2726be42",
    "rejections": "734d6e2653031e524a7c4b2b8bfce56babed23f8683313899cb9f261f3197dca",
}


def normalize(text: str) -> str:
    """uuid4 ID(접두어 포함·tool call id)와 64 hex hash를 등장 순서대로 치환한다."""
    seen: dict[str, str] = {}

    def token(kind: str, value: str) -> str:
        if value not in seen:
            seen[value] = f"<{kind}#{len(seen)}>"
        return seen[value]

    text = PREFIXED_ID.sub(lambda m: token(m.group(1), m.group(0)), text)
    text = HASH.sub(lambda m: token("hash", m.group(0)), text)
    return BARE_ID.sub(lambda m: token("id", m.group(0)), text)


class Recorder:
    """ScriptedChatModel 팩토리. 만든 모델을 모두 모아 모델 입력을 기록한다."""

    def __init__(self):
        self.models: list[ScriptedChatModel] = []

    def factory(self, *replies):
        def make():
            model = ScriptedChatModel(list(replies))
            self.models.append(model)
            return model

        return make

    def inputs(self) -> list[dict]:
        return [
            {
                "messages": [[type(m).__name__, m.content] for m in c["messages"]],
                "tools": c["tools"],
                "kwargs": c["kwargs"],
            }
            for model in self.models
            for c in model.calls
        ]


def _table(conn, sql: str) -> list[dict]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [{k: v for k, v in zip(cols, r) if k != "created_at"} for r in cur.fetchall()]


def _dump(rec: Recorder) -> dict:
    with db.read() as conn:
        runs = _table(conn, "SELECT * FROM agent_run ORDER BY rowid")
        steps = _table(conn, "SELECT * FROM agent_step ORDER BY rowid")
        results = _table(
            conn, "SELECT * FROM command_result WHERE actor_id LIKE 'run:%' ORDER BY rowid"
        )
    return {"runs": runs, "steps": steps, "command_results": results, "model": rec.inputs()}


def _digest(rec: Recorder) -> str:
    text = json.dumps(_dump(rec), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical_hash(normalize(text))


def _key():
    return uuid.uuid4().hex


def _validation_id(pack, cand_id):
    with db.read() as conn:
        [v] = list_validations(conn, pack.site_id, cand_id)
    return v.validation_id


def _submit_a(pack):
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    out = submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a))
    assert out.status == "APPLIED"


def _run():
    with db.read() as conn:
        return conn.execute(
            "SELECT run_id, status, wait_ref FROM agent_run ORDER BY rowid DESC LIMIT 1"
        ).fetchone()


def test_normalize_replaces_ids_in_order():
    a, b = "run_" + "a" * 32, "cand_" + "b" * 32
    h = "c" * 64
    text = f"{a} {b} {a} form:form_{'d' * 32} {h} {'e' * 32}"
    assert normalize(text) == "<run#0> <cand#1> <run#0> form:<form#2> <hash#3> <id#4>"


def test_golden_plan_b(seeded):
    """기본안 B 전체: Alpha → 거절(C 고정) → 재개 → LIST → ASK → 수락 → TRY → Beta → 승인."""
    pack, rec = seeded, Recorder()
    _submit_a(pack)
    run_until_idle(pack, model_factory=rec.factory(solve("L0"), solve("L1")))
    _, _, alpha = _run()
    x = pack.demo_rejections[0]
    body = RejectRequest(
        candidate_id=alpha,
        validation_id=_validation_id(pack, alpha),
        reason_code=x.reason_code,
        target_task_ids=x.target_task_ids,
        axes=x.axes,
        comment=x.comment,
    )
    assert reject_candidate(pack, "supervisor", _key(), body).status == "APPLIED"
    ask = call(
        "ASK_TASK_OWNER",
        "자원 축 확인",
        task_id="A",
        axis="RESOURCE",
        allowed_values=["SITE-CR-01"],
        question="SITE-CR-01을 써도 되나요?",
    )
    listing = call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A")
    run_until_idle(pack, model_factory=rec.factory(listing, ask))
    _, status, message_id = _run()
    assert status == "WAITING_HUMAN"
    reply = ReplyRequest(message_id=message_id, decision="ACCEPT", comment="좋습니다")
    assert reply_message(pack, "planner_a", _key(), reply).status == "APPLIED"
    try_beta = call(
        "TRY_ALTERNATIVE_RESOURCE", "SITE-CR-01 시도", task_id="A", resource_id="SITE-CR-01"
    )
    run_until_idle(pack, model_factory=rec.factory(try_beta))
    _, _, beta = _run()
    with db.read() as conn:
        ctx = get_site(conn, pack.site_id).context_version
    approve = ApproveRequest(
        candidate_id=beta,
        validation_id=_validation_id(pack, beta),
        expected_context_version=ctx,
    )
    assert approve_and_commit(pack, "supervisor", _key(), approve).status == "APPLIED"
    assert _run()[1] == "SUCCEEDED"
    assert _digest(rec) == GOLDEN["plan_b"]


def test_golden_gateway_rejections(seeded):
    """L0(INFEASIBLE) → MALFORMED → L0 다시(ACTION_NOT_AVAILABLE) → LLM_ERROR → MALFORMED_TWICE."""
    pack, rec = seeded, Recorder()
    _submit_a(pack)

    def conn_error():
        raise openai.APIConnectionError(request=REQ)

    replies = (
        solve("L0"),
        AIMessage(content="L1로 하겠습니다"),
        solve("L0"),
        conn_error,
        conn_error,
        AIMessage(content="", tool_calls=[*solve("L1").tool_calls, *solve("L2").tool_calls]),
        escalate(),
    )
    run_until_idle(pack, model_factory=rec.factory(*replies))
    assert _run()[1] == "ESCALATED"
    steps = _dump(rec)["steps"]
    assert [(s["result_kind"], json.loads(s["guard"])["reason_code"]) for s in steps] == [
        ("CONTINUE", None),
        ("REJECTED", "MALFORMED"),
        ("REJECTED", "ACTION_NOT_AVAILABLE"),
        ("REJECTED", "LLM_ERROR"),
        ("DONE", "MALFORMED"),
    ]
    assert _digest(rec) == GOLDEN["rejections"]
