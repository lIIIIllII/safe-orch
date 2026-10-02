"""FACT_UPDATE 제안의 origin별 규칙 (부록 A.25·A.29 3). app 내부 모듈을 import하지 않는다.

확인자·Hold 조건·바꿀 수 있는 필드·TIME Consent·확인 뒤 Run 처리는 origin으로만 갈린다.
- EVENT: Event Response가 지연 신고로 만든다. Supervisor 확인, 그 Event의 Hold ACTIVE 필요,
  earliest_start만, 확정이 ER Run을 끝낸다(SUCCEEDED).
- OWNER: Replanning이 작업 담당자에게 시간창을 넓힐지 묻는다. 담당자 확인, Hold 조건 없음,
  latest_start·latest_end, 새 TIME Consent, 제안을 만든 Run을 깨운다.
"""

from dataclasses import dataclass
from typing import Any, Literal

Origin = Literal["EVENT", "OWNER"]


@dataclass(frozen=True)
class OriginRule:
    confirmer: Literal["SUPERVISOR", "OWNER"]
    fields: frozenset[str]
    needs_hold: bool
    new_time_consent: bool
    ends_run: bool


RULES: dict[str, OriginRule] = {
    "EVENT": OriginRule("SUPERVISOR", frozenset({"earliest_start"}), True, False, True),
    "OWNER": OriginRule("OWNER", frozenset({"latest_start", "latest_end"}), False, True, False),
}


def origin(payload: dict[str, Any]) -> str:
    """origin이 없는 옛 행은 EVENT다(A.29 이전 ER 제안)."""
    return str(payload.get("origin", "EVENT"))


def rule(payload: dict[str, Any]) -> OriginRule:
    return RULES[origin(payload)]


def changes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """바꿀 필드 목록 [{field, old_value, new_value}]. EVENT는 field·old_value·new_value 한 쌍이다."""
    if "changes" in payload:
        return [dict(c) for c in payload["changes"]]
    return [{k: payload[k] for k in ("field", "old_value", "new_value")}]
