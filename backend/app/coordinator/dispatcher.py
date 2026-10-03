"""dispatch 워커. 스레드 1개, job_id 순서.

처리하는 kind는 RECHECK·VALIDATE·BUILD_CONSULTATION, 그리고 model_factory가 있으면 START_RUN·
RESUME_RUN·CONTINUE_RUN이다. model_factory가 없으면 이 셋은 claim하지 않고 PENDING으로 두며 순서를
막지 않는다.
실패하면 롤백하고 attempts < 3이면 PENDING, 3이면 FAILED. 기동 복구는 CLAIMED → PENDING과, RUNNING으로
남은 Run의 CONTINUE_RUN 등록이다 (ST-19).
"""

import logging
import threading

from app.agents.runtime import ModelFactory
from app.coordinator.transitions import (
    HANDLERS,
    continue_run,
    recover_running_runs,
    resume_run,
    start_run,
)
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.dispatch import claim_next, mark_failed_attempt, requeue_claimed

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
HANDLED_KINDS = tuple(HANDLERS)
RUN_KINDS = {"START_RUN": start_run, "RESUME_RUN": resume_run, "CONTINUE_RUN": continue_run}


def process_next(pack: LoadedPack, model_factory: ModelFactory | None = None) -> int | None:
    """job 1건을 처리하고 job_id를 반환한다. 처리할 job이 없으면 None."""
    kinds = HANDLED_KINDS + (tuple(RUN_KINDS) if model_factory is not None else ())
    with db.write() as tx:
        job = claim_next(tx, pack.site_id, kinds)
    if job is None:
        return None
    try:
        if job["kind"] in RUN_KINDS:
            assert model_factory is not None
            RUN_KINDS[job["kind"]](pack, job, model_factory)
        else:
            HANDLERS[job["kind"]](pack, job)
    except Exception as e:  # noqa: BLE001 — 핸들러 예외는 job 실패로 기록한다
        with db.write() as tx:
            status = mark_failed_attempt(tx, job["job_id"], repr(e), MAX_ATTEMPTS)
        log.warning("dispatch job %s (%s) failed → %s: %r", job["job_id"], job["kind"], status, e)
    return job["job_id"]


def run_until_idle(
    pack: LoadedPack, max_jobs: int = 100, model_factory: ModelFactory | None = None
) -> list[int]:
    """처리할 job이 없을 때까지 돌린다(테스트용). 처리한 job_id 목록."""
    done = []
    while len(done) < max_jobs:
        job_id = process_next(pack, model_factory)
        if job_id is None:
            break
        done.append(job_id)
    return done


def requeue_claimed_jobs(pack: LoadedPack) -> int:
    with db.write() as tx:
        return requeue_claimed(tx, pack.site_id)


def recover_on_startup(pack: LoadedPack) -> None:
    """기동 때 한 번: CLAIMED job을 되돌리고, RUNNING으로 남은 Run을 이어 가게 한다."""
    requeue_claimed_jobs(pack)
    recover_running_runs(pack)


class DispatchWorker:
    """워커 스레드 1개. 비어 있으면 poll_s마다 다시 본다."""

    def __init__(
        self, pack: LoadedPack, poll_s: float = 0.5, model_factory: ModelFactory | None = None
    ):
        self.pack = pack
        self.poll_s = poll_s
        self.model_factory = model_factory
        self._stop = threading.Event()
        self._lock = threading.Lock()  # job 1건을 처리하는 동안 잡는다 (/dev/reset이 기다린다)
        self._thread = threading.Thread(target=self._run, name="dispatch-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def quiesce(self, timeout: float) -> bool:
        """처리 중인 job이 끝나길 timeout까지 기다린 뒤 새 job을 막고 멈춤을 예약한다.

        True면 호출한 쪽이 release_and_join()을 불러야 한다. False면 아무것도 바꾸지 않았다.
        """
        if not self._lock.acquire(timeout=timeout):
            return False
        self._stop.set()
        return True

    def release_and_join(self, timeout: float = 10) -> None:
        self._lock.release()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        """예외가 나도 스레드를 끝내지 않는다. 로그를 남기고 poll_s만큼 쉰 뒤 계속 돈다."""
        requeued = False
        try:
            while not self._stop.is_set():
                try:
                    with self._lock:
                        if self._stop.is_set():
                            break
                        if not requeued:
                            recover_on_startup(self.pack)
                            requeued = True
                        job_id = process_next(self.pack, self.model_factory)
                except Exception:
                    log.exception("dispatch worker loop error")
                    job_id = None
                if job_id is None:
                    self._stop.wait(self.poll_s)
        finally:
            db.close()
