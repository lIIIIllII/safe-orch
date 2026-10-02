import shutil

import httpx
import pytest

from app.config import Settings, get_settings
from app.domain.canonical import canonical_hash
from app.domain.models import AgentRun, Snapshot, Task
from app.packs.loader import confirmed_fields, load_pack, pack_dir
from app.store import db
from app.store.repos.runs import insert_run
from app.store.repos.seed import seed_pack
from app.store.repos.site import bump_context_version
from app.store.repos.snapshots import create_snapshot
from app.store.repos.tasks import insert_task_revision

# 테스트는 저장소 루트의 .env를 읽지 않는다. 개발자 .env가 테스트 결과를 바꾸지 못하게 한다 (A.28).
Settings.model_config["env_file"] = None

# 테스트 기준 설정 (A.28). 환경변수가 기본값보다 우선하므로, 셸에 어떤 값이 있어도 여기 값으로 고정한다.
# DB_PATH는 테스트마다 다르므로 temp_db가 정한다. 다른 값이 필요한 테스트는 fixture에서 바꾼다.
TEST_ENV = {
    # 실제 API를 부르지 않는다 (A.17)
    "OPENAI_API_KEY": "",
    "OPENAI_MODEL": "",
    "OPENAI_TEMPERATURE": "",
    "OPENAI_SEED": "",
    "OPENAI_REASONING_EFFORT": "",
    "DEMO_MODE": "true",
    "PACK": "shipyard",
    "DISPATCH_WORKER": "false",  # 테스트는 run_until_idle로 직접 돌린다
    "LANGSMITH_TRACING": "false",
    # Agent 자동 시작은 끈다. 켜는 테스트는 coordination_on·event_response_on을 쓴다 (A.24·A.25·A.28)
    "COORDINATION_ENABLED": "false",
    "EVENT_RESPONSE_ENABLED": "false",
}


@pytest.fixture(autouse=True)
def test_env(monkeypatch):
    """테스트 기준 설정을 한 곳에서 정한다 (A.28)."""
    for name, value in TEST_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """테스트는 실제 API를 부르지 않는다 (부록 A.17).

    OpenAI 설정은 test_env가 비운다. 여기서는 실제 네트워크 전송 계층만 막는다.
    TestClient는 자체 transport를 쓰므로 막히지 않는다.
    """

    def blocked(*args, **kwargs):
        raise RuntimeError("network disabled in tests")

    async def blocked_async(*args, **kwargs):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """테스트마다 임시 DB 파일을 쓴다."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    db.close()
    db.init_db()
    yield path
    db.close()
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def pack():
    return load_pack(pack_dir("shipyard"))


@pytest.fixture
def seeded(pack):
    """임시 DB에 shipyard Pack을 seed한다."""
    with db.write() as tx:
        seed_pack(tx, pack)
    return pack


@pytest.fixture
def pack_copy(tmp_path):
    """shipyard Pack을 임시 폴더에 복사한다. 테스트가 파일을 고쳐 변형 Pack을 만든다."""
    dst = tmp_path / "shipyard"
    shutil.copytree(pack_dir("shipyard"), dst)
    return dst


@pytest.fixture
def use_db_path(monkeypatch):
    """DB_PATH를 다른 파일로 바꾼다 (init_db 전의 빈 DB나 옛 DB를 시험할 때)."""

    def switch(path):
        db.close()
        monkeypatch.setenv("DB_PATH", str(path))
        get_settings.cache_clear()
        return get_settings().db_path

    return switch


def make_task(pack, *, revision=1, source_ref="scenario:new_task", **overrides):
    """scenario의 신규 작업 A를 기본값으로 Task를 만든다. fields는 CONFIRMED (부록 A.10)."""
    data = pack.new_task.model_dump(exclude={"requested"})
    data.update(overrides)
    critical = pack.work_types[data["work_type"]].critical_fields
    return Task(
        **data,
        revision=revision,
        lifecycle="READY",
        hazard_tags=pack.hazard_tags(data["work_type"]),
        fields=confirmed_fields(data, critical, source_ref),
    )


def add_task(pack, task):
    """작업 추가·변경 = 새 revision INSERT + context_version +1 (한 트랜잭션)."""
    with db.write() as tx:
        insert_task_revision(tx, pack.site_id, task)
        return bump_context_version(tx, pack.site_id)


def add_run(pack, run_id="run_test", **changes):
    """테스트용 Replanning Run (RUNNING). 3b 전에는 START_RUN 핸들러 대신 이것으로 만든다."""
    data = {
        "run_id": run_id,
        "agent_type": "REPLANNING",
        "case_id": f"case_{run_id}",
        "acting_actor_id": "planner_a",
        "acting_unit_id": "UA",
        "input_ref": {},
        "exec_contract_version": "test",
        "status": "RUNNING",
        **changes,
    }
    with db.write() as tx:
        insert_run(tx, pack.site_id, AgentRun(**data))
    return run_id


def take_snapshot(pack):
    with db.write() as tx:
        return create_snapshot(tx, pack.site_id, pack)


def with_facts(snapshot, **updates):
    """메모리에서 사실을 바꾼 Snapshot (DB에는 저장하지 않음)."""
    content = snapshot.facts().model_copy(update=updates).model_dump(mode="json")
    return Snapshot(
        snapshot_id=snapshot.snapshot_id, snapshot_hash=canonical_hash(content), content=content
    )


@pytest.fixture
def with_a(seeded):
    """R0 + 신규 작업 A(revision 1)."""
    add_task(seeded, make_task(seeded))
    return seeded


@pytest.fixture
def coordination_on(monkeypatch):
    """COORDINATION_ENABLED를 켠다(기본안 A, 부록 A.24). 테스트 기준값은 꺼짐(test_env)."""
    from app.config import get_settings

    monkeypatch.setenv("COORDINATION_ENABLED", "true")
    get_settings.cache_clear()
    yield
    # 환경변수는 monkeypatch가 test_env 값(false)으로 되돌린다. 캐시만 비운다
    get_settings.cache_clear()


@pytest.fixture
def event_response_on(monkeypatch):
    """EVENT_RESPONSE_ENABLED를 켠다(부록 A.25). 테스트 기준값은 꺼짐(test_env)."""
    from app.config import get_settings

    monkeypatch.setenv("EVENT_RESPONSE_ENABLED", "true")
    get_settings.cache_clear()
    yield
    # 환경변수는 monkeypatch가 test_env 값(false)으로 되돌린다. 캐시만 비운다
    get_settings.cache_clear()


# ── Replanning 이관 불변식 (부록 A.30) ─────────────────────────

ESCALATE = "ESCALATE_NO_SOLUTION"
NEEDS_SOLVER = {
    "SOLVE_WITH_SCOPE",
    "TRY_ALTERNATIVE_RESOURCE",
    "LIST_ASSIGNABLE_RESOURCES",
    "ASK_TASK_OWNER",
    "ASK_WINDOW_CHANGE",
}


def replanning_invariant_violations() -> list[str]:
    """이 DB의 모든 Replanning AgentStep에서 A.30 불변식을 검사한다. 위반 설명 목록.

    ① ESCALATE_NO_SOLUTION이 바인딩되었으면 도구는 그것 하나뿐이다.
    ② 관찰의 남은 Solver 호출이 0이면 SOLVE·TRY·LIST·ASK·시간창 질문이 없다.
    """
    import json
    import sqlite3

    try:
        with db.read() as conn:
            rows = conn.execute(
                "SELECT s.run_id, s.step_no, s.available_actions, s.observation FROM agent_step s"
                " JOIN agent_run r ON r.run_id = s.run_id WHERE r.agent_type = 'REPLANNING'"
            ).fetchall()
    except sqlite3.OperationalError:  # 스키마 없는 DB(초기화 전·옛 DB 시험)
        return []
    out = []
    for run_id, step_no, tools, observation in rows:
        names = [t["function"]["name"] for t in json.loads(tools or "[]")]
        if ESCALATE in names and names != [ESCALATE]:
            out.append(f"{run_id}#{step_no}: ESCALATE with others {names}")
        budget = (json.loads(observation or "{}") or {}).get("budget_remaining") or {}
        if budget.get("solver_calls", 1) <= 0 and NEEDS_SOLVER & set(names):
            out.append(f"{run_id}#{step_no}: solver 0 but {names}")
    return out


@pytest.fixture(autouse=True)
def replanning_invariants(temp_db):
    """모든 테스트가 만든 Replanning 관찰에서 A.30 불변식을 확인한다(temp_db보다 먼저 정리된다)."""
    yield
    violations = replanning_invariant_violations()
    assert not violations, violations


@pytest.fixture
def solver_limit(monkeypatch):
    """Replanning Solver 호출 한도를 줄인다. 다 쓰면 이관만 남는다(A.30 3: 조회·질문도 닫힌다).

    이관이 테스트 대상이거나, 다른 Action을 진행하면 새 후보 대기가 생겨 테스트 대상이 바뀌는 경우에 쓴다.
    """
    from app.agents.specs import replanning as spec

    def set_limit(n: int) -> None:
        monkeypatch.setitem(spec.SPEC.budget, "solver_calls", n)

    return set_limit
