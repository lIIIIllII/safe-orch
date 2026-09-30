"""dispatch 워커 (설계서 §3.2·§11.4, 부록 A.15). 스레드 1개, job_id 순서.

처리하는 kind는 RECHECK·VALIDATE·BUILD_CONSULTATION뿐이다. START_RUN·RESUME_RUN·CONTINUE_RUN은
claim하지 않고 PENDING으로 두며 순서를 막지 않는다(3단계·D5).
실패하면 롤백하고 attempts < 3이면 PENDING, 3이면 FAILED. 재시작 복구는 CLAIMED → PENDING만 한다.
"""

import logging
import threading

from app.coordinator.transitions import HANDLERS
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.dispatch import claim_next, mark_failed_attempt, requeue_claimed

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
HANDLED_KINDS = tuple(HANDLERS)


def process_next(pack: LoadedPack) -> int | None:
    """job 1건을 처리하고 job_id를 반환한다. 처리할 job이 없으면 None."""
    with db.write() as tx:
        job = claim_next(tx, pack.site_id, HANDLED_KINDS)
    if job is None:
        return None
    try:
        HANDLERS[job["kind"]](pack, job)
    except Exception as e:  # noqa: BLE001 — 핸들러 예외는 job 실패로 기록한다
        with db.write() as tx:
            status = mark_failed_attempt(tx, job["job_id"], repr(e), MAX_ATTEMPTS)
        log.warning("dispatch job %s (%s) failed → %s: %r", job["job_id"], job["kind"], status, e)
    return job["job_id"]


def run_until_idle(pack: LoadedPack, max_jobs: int = 100) -> list[int]:
    """처리할 job이 없을 때까지 돌린다(테스트용). 처리한 job_id 목록."""
    done = []
    while len(done) < max_jobs:
        job_id = process_next(pack)
        if job_id is None:
            break
        done.append(job_id)
    return done


def requeue_claimed_jobs(pack: LoadedPack) -> int:
    with db.write() as tx:
        return requeue_claimed(tx, pack.site_id)


class DispatchWorker:
    """워커 스레드 1개. 비어 있으면 poll_s마다 다시 본다."""

    def __init__(self, pack: LoadedPack, poll_s: float = 0.5):
        self.pack = pack
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="dispatch-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            requeue_claimed_jobs(self.pack)
            while not self._stop.is_set():
                try:
                    job_id = process_next(self.pack)
                except db.StoreBusyError:
                    job_id = None
                if job_id is None:
                    self._stop.wait(self.poll_s)
        except Exception:
            log.exception("dispatch worker stopped")
        finally:
            db.close()
