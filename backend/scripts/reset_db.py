"""로컬 DB 초기화 (부록 A.3). backend에서 실행한다.

    uv run python -m scripts.reset_db [--pack shipyard]

확인 문구를 정확히 입력해야만 DB 파일과 -journal·-wal·-shm을 지우고 init_db + seed한다.
확인을 건너뛰는 옵션은 없다. Hold·Plan을 포함한 모든 기록이 지워진다.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from app.config import get_settings
from app.packs.loader import PackError, load_pack, pack_dir
from app.store import db
from app.store.repos.seed import seed_pack

CONFIRM_PHRASE = "RESET safe_orch"
SUFFIXES = ("", "-journal", "-wal", "-shm")


def main(argv: list[str] | None = None, input_fn: Callable[[str], str] = input) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m scripts.reset_db")
    parser.add_argument("--pack", default=settings.pack)
    args = parser.parse_args(argv)

    path = settings.db_path
    try:
        pack = load_pack(pack_dir(args.pack))  # 지우기 전에 Pack부터 검증한다
    except PackError as e:
        print(e, file=sys.stderr)
        return 1

    print(f"DB: {path}")
    print(f"Pack: {pack.name} (site {pack.site_id}, pack_hash {pack.pack_hash})")
    print("Hold·Plan을 포함한 모든 기록을 지우고 다시 만듭니다.")
    answer = input_fn(f"계속하려면 '{CONFIRM_PHRASE}'를 입력하세요: ")
    if answer != CONFIRM_PHRASE:
        print("확인 문구가 일치하지 않아 취소했습니다. DB는 그대로입니다.")
        return 1

    db.close()
    for suffix in SUFFIXES:
        f = Path(f"{path}{suffix}")
        try:
            f.unlink(missing_ok=True)
        except PermissionError:
            print(
                f"{f} 파일이 다른 프로세스에 열려 있습니다. 서버(uvicorn)를 끄고 다시 실행하세요.",
                file=sys.stderr,
            )
            return 2

    db.init_db()
    with db.write() as tx:
        seed_pack(tx, pack)
    db.close()
    print(f"완료: schema_version {db.SCHEMA_VERSION}, site {pack.site_id} seed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
