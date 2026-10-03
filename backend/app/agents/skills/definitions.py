"""스킬 정의와 지침 원문 (AG-11). 바꾸면 그 스킬을 쓰는 Agent의 prompt 버전을 올린다.

지침은 일반 규칙만 쓴다. 시나리오·시연 값(작업·자원·Actor ID, 시각, 구역, 문구)을 넣지 않는다.
열리는 조건(opens)은 사실 이름 하나다. "A를 한 뒤에만 B" 같은 순서 조건은 두지 않는다 (AG-03).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    skill_id: str
    title: str
    tools: tuple[str, ...]
    opens: str | None  # Agent spec의 skill_facts가 주는 사실 이름. None이면 항상 열린다
    opens_text: str
    guide: tuple[str, ...]


_ALL = (
    Skill(
        "ASSESS",
        "상황 파악",
        ("LIST_ASSIGNABLE_RESOURCES", "LOOKUP_RESOURCE", "LOOKUP_TASKS"),
        None,
        "항상",
        (
            "조회는 판단에 필요한 사실을 얻는 수단이다.",
            (
                "지금 풀려는 문제의 당사자부터 조회한다. 확인된 제약으로 고정되었거나 문제와 무관한 "
                "대상은 조회해도 해가 열리지 않는다."
            ),
            "같은 조건의 조회는 같은 결과를 돌려주므로 되풀이하지 않는다.",
        ),
    ),
    Skill(
        "TASK_INTAKE",
        "작업 접수",
        ("REQUEST_CONFIRMATION", "COMPLETE_TASKSPEC"),
        "has_draft_request",
        "작성 중인 작업 요청이 있음",
        (
            "값 확인 요청에는 확인받을 값 전체를 넣는다.",
            "완료는 요청자가 확인한 값 그대로만 한다.",
            (
                "완료하려면 값 확인에 요청자의 확인이 있어야 한다. 남은 사람 확인 라운드가 1이면 "
                "질문하지 말고 지금까지의 값으로 값 확인을 낸다."
            ),
            (
                "첫 질문에 빠지거나 모호한 필드를 모두 묶는다. 다음 질문은 답이 모호하게 남은 "
                "필드만 묻는다."
            ),
            (
                "답으로 받은 값은 그대로 쓴다. 날짜는 현장의 지금 기준으로 풀고, 연도·형식을 다시 "
                "묻지 않는다."
            ),
            (
                "자원 표현은 관찰의 자원 유형 표시 이름과 대조하고, 여러 유형에 해당할 수 있으면 유형을 "
                "정하지 말고 묻는다. 관찰에 없는 제약을 만들지 않는다."
            ),
            (
                "값이 맞는지 묻는 것은 질문이 아니라 값 확인으로 한다. 거절되면 사유에 고칠 값이 온다."
            ),
        ),
    ),
    Skill(
        "BUILD_CANDIDATE",
        "후보 구성",
        ("SOLVE_WITH_SCOPE", "TRY_ALTERNATIVE_RESOURCE"),
        "has_conflict",
        "풀어야 할 충돌이 있음",
        (
            "계산으로 시도할 수 있는 탐색이 남아 있으면 사람에게 묻기 전에 계산한다.",
            "대체 자원은 자원 조회로 쓸 수 있는지 확인한 뒤 시도한다.",
        ),
    ),
    Skill(
        "APPLY_REJECTION",
        "거절 반영",
        ("SOLVE_WITH_SCOPE", "TRY_ALTERNATIVE_RESOURCE"),
        "has_rejection",
        "이 Case의 후보에 거절이나 이견으로 생긴 제약이 있음",
        (
            (
                "거절·이견으로 고정된 작업은 움직이지 않는다. 고정되지 않은 작업의 조회·대체 자원·확인으로 "
                "열 수 있는 길을 본다."
            ),
            "거절된 값은 다시 제안하거나 묻지 않는다.",
        ),
    ),
    Skill(
        "IMPACT",
        "영향 분석",
        ("ANALYZE_IMPACT",),
        "has_change",
        "분석할 변경(신고)이 있음",
        (
            "새 값은 제안하기 전에 영향 분석으로 확인한다.",
            "분석 결과의 날짜·시각이 신고 내용과 맞는지 본다.",
        ),
    ),
    Skill(
        "ASK_PEOPLE",
        "사람 확인",
        (
            "ASK_CLARIFICATION",
            "REQUEST_CONFIRMATION",
            "ASK_REPORTER",
            "SEND_CHANGE_REQUEST",
            "WAIT_FOR_REPLIES",
        ),
        "has_ask_target",
        "물을 대상이 있음",
        (
            "사람에게는 조회·계산으로 알 수 없는 것만 묻는다. 묻기 전에 조회로 알 수 있는 것을 먼저 확인한다.",
            "빠진 값은 한 번에 묶어 묻고, 문장이나 답에서 이미 받은 값은 다시 묻지 않는다.",
        ),
    ),
    Skill(
        "ASK_OWNER_TEMP",
        "담당자 확인(임시)",
        ("ASK_TASK_OWNER",),
        "has_unconfirmed_axis",
        "이동 축이 확인되지 않은 작업이 있음",
        (
            "계산으로 시도할 탐색 범위가 남아 있으면 먼저 계산하고, 계산이 막혔을 때만 담당자에게 묻는다.",
            "허용을 요청할 자원은 자원 조회로 쓸 수 있음을 확인한 것만 넣는다.",
        ),
    ),
    Skill(
        "CONSULT",
        "협의",
        ("SEND_CHANGE_REQUEST", "WAIT_FOR_REPLIES", "DRAFT_CONSTRAINT"),
        "has_consult_item",
        "확인 대기 항목이나 이견이 있음",
        (
            "답을 기다리는 요청이 있으면 다시 보내지 않고 기다린다.",
            "이견이 작업을 그대로 두라는 요구일 때만 제약 초안을 만든다. 그 밖의 이견은 보고하거나 이관한다.",
        ),
    ),
    Skill(
        "NOTIFY",
        "통지",
        ("SEND_NOTICE",),
        "has_unsent_notice",
        "확정됐는데 통지하지 않은 대상이 있음",
        ("통지 대상마다 그 사람의 작업을 묶어 한 번 알린다.",),
    ),
    Skill(
        "FACT_UPDATE",
        "사실 수정",
        ("PROPOSE_FACT_UPDATE",),
        "has_hold",
        "Hold가 걸린 신고가 있음",
        (
            "대상 작업과 새 값이 조회·분석·신고자 답으로 확인되었을 때 제안한다. 추정만으로 제안하지 않는다.",
        ),
    ),
    Skill(
        "WRAP_UP",
        "마무리",
        ("ESCALATE_NO_SOLUTION", "ESCALATE", "REPORT_TO_SUPERVISOR"),
        None,
        "항상",
        (
            "할 일을 마쳤으면 결과를 요약해 끝낸다.",
            (
                "풀 수 없어 끝낼 때는 조회·계산·확인으로 열 수 있는 길이 남아 있지 않을 때만 끝내고, "
                "시도한 것과 풀려야 할 조건을 사유에 적는다."
            ),
        ),
    ),
)

SKILLS: dict[str, Skill] = {s.skill_id: s for s in _ALL}
