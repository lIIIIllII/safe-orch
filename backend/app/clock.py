"""현장의 지금. 서버에서 지금을 읽는 곳은 여기 하나다 (ST-17).

설정 SITE_NOW가 있으면 그 시각으로 고정하고, 없으면 실제 시계다. 분 단위로 자른 UTC를 돌려준다.
저장하지 않고 hash·스냅샷에 넣지 않으며, 판정·계산(접수 검증·Solver·Validator·Gate)에는 쓰지 않는다 (ST-17).
"""

from datetime import UTC, datetime

from app.config import get_settings


def site_now() -> datetime:
    now = get_settings().site_now or datetime.now(UTC)
    return now.replace(second=0, microsecond=0)
