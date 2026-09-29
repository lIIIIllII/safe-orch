import shutil

import pytest

from app.config import get_settings
from app.domain.canonical import canonical_hash
from app.domain.models import Snapshot, Task
from app.packs.loader import confirmed_fields, load_pack, pack_dir
from app.store import db
from app.store.repos.seed import seed_pack
from app.store.repos.site import bump_context_version
from app.store.repos.snapshots import create_snapshot
from app.store.repos.tasks import insert_task_revision


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
