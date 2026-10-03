"""스킬 층 (AG-01·AG-02·AG-19). 순수 모듈이다: store·commands·solver를 import하지 않는다.

스킬 = 도구 묶음 + 열리는 조건(사실 조건만) + 지침(순서·요령). 서버는 순서를 강제하지 않는다.
쓸 수 있는 도구 = 열린 스킬의 도구 ∩ 그 Agent의 허용 도구 ∩ 유효한 인자 값이 있는 도구.
모든 Action은 skill 인자로 이번 행동에 쓰는 스킬을 밝힌다. 지침 원문은 definitions.py에 있다.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.agents.skills.definitions import SKILLS, Skill

__all__ = ["SKILLS", "Skill", "available", "catalog", "check", "default_skill", "open_skills"]

SKILL_NOT_OPEN = "SKILL_NOT_OPEN"
TOOL_NOT_IN_SKILL = "TOOL_NOT_IN_SKILL"


def open_skills(skill_ids: Sequence[str], facts: Mapping[str, bool]) -> list[str]:
    """그 Agent가 쓰는 스킬 중 지금 열린 것. 열리는 조건은 사실 하나다(없으면 항상 열린다)."""
    return [s for s in skill_ids if SKILLS[s].opens is None or facts.get(SKILLS[s].opens, False)]


def available(
    skill_ids: Sequence[str], facts: Mapping[str, bool], valid: Mapping[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """{도구: 인자 제한}. valid는 유효한 인자 값이 있는 허용 도구다(순서 조건은 넣지 않는다).

    열린 스킬에 없는 도구는 뺀다. skill 인자의 허용 값은 그 도구를 가진 열린 스킬이다.
    """
    opened = open_skills(skill_ids, facts)
    out = {}
    for tool, limits in valid.items():
        holders = [s for s in opened if tool in SKILLS[s].tools]
        if holders:
            out[tool] = {**limits, "skill": holders}
    return out


def check(skill_id: str, tool: str, opened: Sequence[str]) -> str | None:
    """고른 스킬이 열려 있고 그 도구를 가졌는가. 아니면 거절 사유 (형식 오류 연속에 세지 않는다, AG-19)."""
    if skill_id not in opened:
        return SKILL_NOT_OPEN
    return None if tool in SKILLS[skill_id].tools else TOOL_NOT_IN_SKILL


def default_skill(skill_ids: Sequence[str], tool: str) -> str:
    """그 도구를 가진 첫 스킬 (스크립트 응답·준비 스크립트용)."""
    return next(s for s in skill_ids if tool in SKILLS[s].tools)


def catalog(skill_ids: Sequence[str], actions: Mapping[str, Any]) -> str:
    """System에 넣는 스킬별 지침. 도구는 그 Agent의 허용 도구만 적는다."""
    lines = []
    for sid in skill_ids:
        s = SKILLS[sid]
        tools = ", ".join(t for t in s.tools if t in actions)
        lines.append(
            f"- {sid}({s.title}): 열림: {s.opens_text}. 도구: {tools}. 지침: {' '.join(s.guide)}"
        )
    return "\n".join(lines)
