import shutil

import httpx
import pytest

from app.config import Settings, get_settings
from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.domain.models import AgentRun, Pin, Snapshot, Task
from app.packs.loader import confirmed_fields, load_pack, pack_dir
from app.store import db
from app.store.repos.pins import insert_pin
from app.store.repos.runs import insert_run
from app.store.repos.seed import seed_pack
from app.store.repos.site import bump_context_version
from app.store.repos.snapshots import create_snapshot
from app.store.repos.tasks import insert_task_revision

# 테스트는 저장소 루트의 .env를 읽지 않는다. 개발자 .env가 테스트 결과를 바꾸지 못하게 한다.
Settings.model_config["env_file"] = None

# 테스트 기준 설정. 환경변수가 기본값보다 우선하므로, 셸에 어떤 값이 있어도 여기 값으로 고정한다.
# DB_PATH는 테스트마다 다르므로 temp_db가 정한다. 다른 값이 필요한 테스트는 fixture에서 바꾼다.
TEST_ENV = {
    # 실제 API를 부르지 않는다
    "OPENAI_API_KEY": "",
    "OPENAI_MODEL": "",
    "OPENAI_TEMPERATURE": "",
    "OPENAI_SEED": "",
    "OPENAI_REASONING_EFFORT": "",
    "DEMO_MODE": "true",
    "PACK": "shipyard",
    "DISPATCH_WORKER": "false",  # 테스트는 run_until_idle로 직접 돌린다
    "LANGSMITH_TRACING": "false",
    # 사건 → 메인 자동 시작은 끈다. 켜는 테스트는 main_on을 쓴다
    "MAIN_AUTO_START": "false",
    # 현장의 지금을 고정한다(Horizon 원점). 실제 시계로 돌면 관찰이 실행마다 달라진다 (ST-17)
    "SITE_NOW": "2026-10-12T09:00+09:00",
}


@pytest.fixture(autouse=True)
def test_env(monkeypatch):
    """테스트 기준 설정을 한 곳에서 정한다."""
    for name, value in TEST_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """테스트는 실제 API를 부르지 않는다.

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


# 서버 로직 테스트의 기준 상태: 좁은 시간창 + 일곱 작업 고정. Pack의 plan_r0(모든 시간창이 Horizon 전체,
# 고정 없음)와 따로 둔다. 시연 데이터가 바뀌어도 로직 테스트의 기대값은 그대로다.
LEGACY_WINDOWS = {  # task_id: (earliest_start, latest_start, latest_end)
    "B": (0, 0, 60),
    "C": (60, 90, 120),
    "D": (0, 0, 30),
    "E": (45, 120, 150),
    "K": (1440, 1440, 1560),
    "P": (1500, 1500, 1560),
    "W": (1680, 1680, 1920),
    "Q": (2910, 2910, 2970),
    "M": (2910, 2910, 3000),
}
LEGACY_PINNED = ("B", "D", "K", "M", "P", "Q", "W")


def _legacy_task(task):
    es, ls, le = LEGACY_WINDOWS[task.task_id]
    window = {"earliest_start": es, "latest_start": ls, "latest_end": le}
    fields = dict(task.fields)
    if "window" in fields:
        fields["window"] = fields["window"].model_copy(update={"value": window})
    return task.model_copy(update={**window, "fields": fields})


@pytest.fixture(scope="session")
def real_pack():
    """Pack 파일 그대로(시연 데이터)."""
    return load_pack(pack_dir("shipyard"))


@pytest.fixture(scope="session")
def pack(real_pack):
    """기준 상태의 Pack: plan_r0 작업의 시간창만 LEGACY_WINDOWS로 바꾼 것."""
    return real_pack.model_copy(update={"tasks": tuple(_legacy_task(t) for t in real_pack.tasks)})


def pin_tasks(pack, task_ids, actor_id=None, by_role="OWNER"):
    """고정 기록을 직접 넣는다(명령을 거치지 않으므로 context는 그대로). actor_id가 없으면 담당자."""
    owners = {t.task_id: t.owner_actor_id for t in pack.tasks}
    owners[pack.new_task.task_id] = pack.new_task.owner_actor_id
    with db.write() as tx:
        version = tx.execute("SELECT context_version FROM site").fetchone()[0]
        for tid in task_ids:
            pin = Pin(
                pin_id=new_id("pin"),
                task_id=tid,
                pinned_by=actor_id or owners[tid],
                by_role=by_role,
            )
            insert_pin(tx, pack.site_id, pin, "2026-10-12T00:00:00+00:00", version)


@pytest.fixture
def seeded(pack):
    """임시 DB에 기준 상태를 seed한다: shipyard Pack + 옛 시간창 + 일곱 작업 고정."""
    with db.write() as tx:
        seed_pack(tx, pack)
    pin_tasks(pack, LEGACY_PINNED)
    return pack


@pytest.fixture
def seeded_real(real_pack):
    """임시 DB에 Pack 파일 그대로 seed한다(고정 없음)."""
    with db.write() as tx:
        seed_pack(tx, real_pack)
    return real_pack


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
    """scenario의 신규 작업 A를 기본값으로 Task를 만든다. fields는 CONFIRMED."""
    data = pack.new_task.model_dump(exclude={"requested"})
    data.update(overrides)
    data.pop("default_requirements", None)  # 서버가 작업 유형에서 도출한다
    data.pop("default_demands", None)
    critical = pack.work_types[data["work_type"]].critical_fields
    return Task(
        **data,
        revision=revision,
        lifecycle="READY",
        hazard_tags=pack.hazard_tags(data["work_type"]),
        default_requirements=pack.default_requirements(data["work_type"]),
        default_demands=pack.default_demands(data["work_type"]),
        fields=confirmed_fields(data, critical, source_ref),
    )


def add_task(pack, task):
    """작업 추가·변경 = 새 revision INSERT + context_version +1 (한 트랜잭션)."""
    with db.write() as tx:
        insert_task_revision(tx, pack.site_id, task)
        return bump_context_version(tx, pack.site_id)


def add_run(pack, run_id="run_test", **changes):
    """테스트용 Replanning Run (RUNNING). START_RUN 핸들러를 거치지 않고 직접 만든다."""
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


def choose(pack, candidate_id=None):
    """Supervisor가 안을 고른다(주지 않으면 검토 대기 첫 후보). 고른 안만 협의가 나간다 (AG-29)."""
    from app.commands.approval import ChooseRequest, choose_candidate
    from app.store.repos.consultations import list_review_queue
    from app.store.repos.records import list_validations

    with db.read() as conn:
        cid = candidate_id or list_review_queue(conn, pack.site_id)[0]
        validation = [v for v in list_validations(conn, pack.site_id, cid) if v.status == "PASS"][
            -1
        ]
    body = ChooseRequest(candidate_id=cid, validation_id=validation.validation_id)
    out = choose_candidate(pack, "supervisor", new_id("key"), body)
    assert out.status == "APPLIED", out.reason_codes
    return cid


@pytest.fixture
def with_a(seeded):
    """R0 + 신규 작업 A(revision 1)."""
    add_task(seeded, make_task(seeded))
    return seeded


@pytest.fixture
def main_on(monkeypatch):
    """MAIN_AUTO_START를 켠다: 사건이 생기면 메인이 뜬다. 테스트 기준값은 꺼짐(test_env)."""
    from app.config import get_settings

    monkeypatch.setenv("MAIN_AUTO_START", "true")
    get_settings.cache_clear()
    yield
    # 환경변수는 monkeypatch가 test_env 값(false)으로 되돌린다. 캐시만 비운다
    get_settings.cache_clear()
