"""골든 테스트: 실행 계층 일반화 전후로 Replanning 기록과 모델 입력이 같은지 확인한다 (부록 A.23).

리팩터링 전 코드에서 값을 만들어 고정했다. 대상은 Run 행, AgentStep 행(created_at 제외), Gateway
CommandResult(created_at 제외), 모델이 받은 입력(System·Human 메시지, 바인딩한 도구, bind 인자)이다.
uuid4 ID와 hash는 실행마다 달라지므로 등장 순서대로 치환한 뒤 hash한다.
p8(현장 문구를 Pack에서 받음)은 렌더링한 System이 p7과 글자까지 같고 기록의 prompt_version 라벨만
다르다. 그래서 라벨만 p7로 되돌려 hash한다(A.23 2단계).
p9(A.29 시간창 질문)는 의도한 변경이다. 이 경로들에서는 시간창 질문이 열리지 않으므로(자원 경로가 열려 있거나
사람 확인 라운드 전) 바인딩한 도구는 같고, System의 A.29 문구(prompt.to_p7)·빈 window_options·라벨
(prompt_version, exec_contract_version)만 다르다. 그것만 되돌려 같은 hash인지 확인한다(골든 값은 그대로).
p10(A.30 이관은 다른 Action이 모두 닫혔을 때만)도 의도한 변경이다. System의 A.30 문구(prompt.to_p9)와,
다른 도구가 열린 step에서 빠진 ESCALATE_NO_SOLUTION 도구만 다르다. 모델 입력 tools와 AgentStep
available_actions의 빠진 자리(목록 끝)에 p9 ESCALATE 스키마를 다시 붙이고(붙인 곳에는 다른 도구가 하나
이상 있었음을 확인), 라벨을 되돌려 같은 hash인지 확인한다.
"""

import json
import re
import uuid

import httpx
import openai
from langchain_core.messages import AIMessage
from scripted import ScriptedChatModel, call, escalate, solve

from app.agents.prompts import replanning as prompt
from app.agents.specs import replanning as spec
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
from app.store.repos._rows import dumps
from app.store.repos.records import list_validations
from app.store.repos.site import get_site

PREFIXED_ID = re.compile(r"(?<![0-9a-z_])([a-z]+)_[0-9a-f]{32}(?![0-9a-f])")
BARE_ID = re.compile(r"(?<![0-9a-f_])[0-9a-f]{32}(?![0-9a-f])")
HASH = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
PROMPT_LABEL = ('"replanning-p10"', '"replanning-p7"')  # (현재, 골든을 만든 버전)
CONTRACT_LABEL = ('"replanning-a30"', '"replanning-d5"')  # exec_contract_version (A.29·A.30)
ESCALATE = "ESCALATE_NO_SOLUTION"
# A.29에서 Observation에 더한 키. 이 경로에서는 늘 빈 목록이다(모델 입력은 공백 없이, DB는 기본 구분자)
EMPTY_WINDOW_OPTIONS = ('\\"window_options\\":[],', '\\"window_options\\": [], ')

# 리팩터링 전 코드(부록 A.22 커밋 10898a7 기준)에서 만든 값
GOLDEN = {
    "plan_b": "67a18e61f6648f9827b0fdee51ce3cb3fdb6e225554886761e6a939452b0220d",
    "rejections": "c7bb35a4782135b222cbc90acb95c6eb45030d0e12dd3bb7bffb13752c36b73e",
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


def with_escalate(tools: list[dict], added: list[int]) -> list[dict]:
    """A.30 이전에는 언제나 끝에 있던 ESCALATE 도구를 다시 붙인다. 붙인 곳에는 다른 도구가 있어야 한다."""
    if any(t["function"]["name"] == ESCALATE for t in tools):
        return tools
    assert tools, "ESCALATE만 빠진 빈 도구 목록은 없다(다른 도구가 하나 이상 있어야 한다)"
    added.append(len(tools))
    # docstring을 바꾸지 않았으므로 지금 스키마가 p9 스키마와 같다 (A.30 문구는 OPENS·규칙 줄만)
    return [*tools, spec.tool_schemas({ESCALATE: {}})[0]]


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

    def inputs(self, added: list[int]) -> list[dict]:
        return [
            {
                "messages": [
                    [
                        type(m).__name__,
                        prompt.to_p7(prompt.to_p9(m.content))
                        if type(m).__name__ == "SystemMessage"
                        else m.content,
                    ]
                    for m in c["messages"]
                ],
                "tools": with_escalate(c["tools"], added),
                "kwargs": c["kwargs"],
            }
            for model in self.models
            for c in model.calls
        ]


def _table(conn, sql: str) -> list[dict]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [{k: v for k, v in zip(cols, r) if k != "created_at"} for r in cur.fetchall()]


def _dump(rec: Recorder, added: list[int] | None = None) -> dict:
    added = [] if added is None else added
    with db.read() as conn:
        runs = _table(conn, "SELECT * FROM agent_run ORDER BY rowid")
        steps = _table(conn, "SELECT * FROM agent_step ORDER BY rowid")
        results = _table(
            conn, "SELECT * FROM command_result WHERE actor_id LIKE 'run:%' ORDER BY rowid"
        )
    for s in steps:  # 저장 형식(_rows.dumps) 그대로 다시 쓴다
        s["available_actions"] = dumps(with_escalate(json.loads(s["available_actions"]), added))
    return {"runs": runs, "steps": steps, "command_results": results, "model": rec.inputs(added)}


def _digest(rec: Recorder) -> str:
    added: list[int] = []
    text = json.dumps(_dump(rec, added), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # A.30: 다른 도구가 열린 step에서는 ESCALATE가 빠진다(이 경로들에서 모든 step이 그렇다)
    assert added and all(n >= 1 for n in added)
    for current, golden in (PROMPT_LABEL, CONTRACT_LABEL):
        assert current in text and golden not in text
        text = text.replace(current, golden)
    # 모든 관찰에 빈 window_options가 있다(그 밖의 값이면 되돌리지 못해 hash가 달라진다)
    assert all(form in text for form in EMPTY_WINDOW_OPTIONS)
    for form in EMPTY_WINDOW_OPTIONS:
        text = text.replace(form, "")
    assert "window_options" not in text
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
