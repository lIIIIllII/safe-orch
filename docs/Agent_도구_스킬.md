# SAFE-ORCH Agent·도구·스킬

2026-10-04. 목록과 규칙만 적는다. 왜 이런 구조인지는 블루프린트 §5, 지침 원문은 코드(`backend/app/agents/`)에 있다. 도구 이름은 제안이다.

## 1. 층과 공통 규칙

| 층 | 누가 | 무엇 |
|---|---|---|
| 아래층 | 서버, 항상 | 권한·안전·동의·인자 유효성·Budget. 어떤 스킬을 켜도 바뀌지 않는다 |
| 스킬 층 | 서버 | 열린 스킬의 도구만 보인다. 쓸 수 있는 도구 = 스킬의 도구 ∩ 그 Agent에 허용된 도구 |
| 지침 | 모델 | 순서와 요령. 서버는 순서를 강제하지 않는다 |

- 모든 행동에 `skill`(이번 행동에 쓴 스킬)과 `decision_summary`("이유: …/다음: …")가 필수다. 스킬을 바꾸는 데 Budget을 쓰지 않는다.
- 스킬이 열리는 조건은 사실 조건(할 일이 있는지)만 둔다. "A를 한 뒤에만 B" 같은 순서 조건은 두지 않는다.
- 서버에 남는 흐름 규칙은 둘뿐이다.
  - 같은 탐색을 다시 요청하면 계산하지 않고 이전 결과를 "이미 해 본 탐색"으로 돌려준다(Solver Budget 안 씀, step은 씀).
  - 담당자가 거절한 값은 다시 묻지 못한다. 판정은 보낸 Run이나 Case가 아니라 메시지 기준으로 현장 전체에서 하고, 같은 작업 revision인 동안 유지된다. 그 작업이 거절 이후 바뀌었을 때만(새 revision), 바뀐 내용을 서버 문구에 붙여 다시 물을 수 있다.
- 1 step = step 예약 → LLM 1회 → Action 1개. 도구 실행은 Tool Gateway만 거친다.
- 도구의 시각 인자는 현장 날짜·시각 문자열("YYYY-MM-DD HH:MM")이다. 서버가 분으로 바꾸고, 형식이 틀리거나 Horizon 밖이면 거절한다.

## 2. Agent

| Agent | Goal | 끝나는 방식 | Budget |
|---|---|---|---|
| Main | 맡은 사건을 끝까지 처리한다 | 이 Case의 열린 일이 없으면 `CLOSE`. 풀 수 없으면 Supervisor에게 `ESCALATE` | 기본 step 14, 전문 Agent 호출 10 (설정값). 그 Case에서 사람이 새 일을 만들 때마다 step 8·호출 5씩, 네 번까지 늘어난다(AG-30). 사건을 합칠 때의 가산은 3단계 |
| Intake | 묻지 않고 만든 작업 묶음(값마다 출처: 말함·정함) | `COMPLETE_TASK_BATCH`로 완료. 막히면 `RETURN_RESULT(BLOCKED)`: 접수 미완으로 끝나고 요청자에게 사유를 통지한다 | step 12 |
| Replanning | 검증 가능한 후보 묶음과 서버 지표 기반 설명 | `RETURN_RESULT` | step 15, Solver 6, 알아보기 계산 (값 미정) |
| Coordination | 협의 항목 해소, 확정 뒤 통지 | `RETURN_RESULT` | step 12 |
| Event Response | 신고의 대상·영향·사실 수정안 | `RETURN_RESULT` | step 10, 사람 라운드 2 |
| Site Assistant | 근거 있는 답 | `ANSWER` | 질문당 step 6 |

- LLM 시도 Budget은 모든 Agent가 step × 2다.
- 전문 Agent는 다른 Agent를 부르지 않고 사람에게 이관하지도 않는다. 막히면 `RETURN_RESULT(BLOCKED)`에 요약과 풀 수 있는 길을 담아 메인에게 돌려준다. Supervisor 이관은 메인만 한다.
- 작업 담당자 확인은 Coordination 한 창구이고, Supervisor가 고른 안의 협의에서만 묻는다(사전 확인은 없다). Replanning은 사람에게 묻지 않는다. 고정되지 않은 작업은 시각도 자원도 움직이고(AG-34), 범위 안 작업마다 서버가 채운 적격 자원 가운데서 Solver가 고른다. 요청 자원은 동의다: 그 자원이면 묻지 않고, 다른 자원으로 바뀐 안은 협의 항목이 된다.
- 작업 고정·고정 해제와 희망 영역, 직접 이동·자원 바꾸기·작업 없애기(담당자가 자기 작업의 시각을 옮기거나 작업 카드에서 자원을 바꾸거나 자기 작업을 없애고 바로 확정, AG-31)는 사람만 타임라인에서 한다. 없앤 작업(취소됨)은 관찰에 들어가지 않는다. Agent 도구에 없다(AG-27). 작업을 관찰에 넣는 Agent(Replanning, Event Response)의 관찰에는 고정 여부(누가)와 희망 영역이 들어가고, 메인의 "움직일 수 있는 작업"은 고정되지 않은 작업이다. 희망 영역을 Solver 시도에 쓰는 것은 여러 안 제안 때 한다.
- 사람이 남긴 사유(Supervisor 거절 문장, 담당자 이견)는 Case가 닫힐 때까지 Replanning 관찰에 인용으로 전부 쌓인다. Replanning은 사유가 시각·자원 조건으로 읽히면 조건을 걸어 다시 풀고, 서버는 사유를 해석하지 않는다(CV-26).
- 서버는 살아 있는 후보가 거절·이견된 변경과 같은 변경을 담았는지 표시만 한다(Replanning·메인 관찰과 검토 패널). 메인은 그 표시가 있는 후보를 협의로 보내지 않고 재계획을 다시 부른다(지침).
- 여러 안: 메인은 재계획을 접근을 달리해 두세 번 순서대로 부른다(같은 배치는 한 후보로 묶이고 그 후보에 접근이 더해진다). 안이 모이면 Supervisor가 고를 때까지 기다리고, 고른 안만 협의를 부른 뒤 승인을 기다린다(AG-28·AG-29). 거절·이견이 나오면 재계획을 다시 부를 수 있다.
- 재계획 관찰에는 이번 접근과 문장, 이 Case의 접근별 후보가 들어간다. 조건 도구의 목적 순서(변경 먼저 / 지연 먼저)는 재계획이 고른다(CV-27).
- Intake는 메인이 부르지 않는 입구다. 접수에서 시작하고, 완료하면 작업이 준비되며, 막히면 이관 없이 요청자에게 접수 미완을 알린다. 요청자에게 묻지 않는다(AG-32): 문장에 없는 값은 스스로 정하고(작업 시간 추정, 조회한 적격 자원에서 고르기) 값마다 출처를 적는다. 서버는 출처를 검사하지 않는다. 정한 값은 요청자가 작업 카드에서 고치거나 확인하고(AG-33), 재계획 관찰과 열 수 있는 것에 정한 값으로 표시된다.
- 메인은 한 번에 하나다. 사건(작업 준비됨, 신고, Hold 해제, 후보 승인·거절, 하위 Run 종료, 요청 철회)이 생겼는데 열린 메인이 없으면 새 메인을 띄운다. 작업 고정·고정 해제·직접 이동·작업 없애기는 열린 메인이 있을 때만 사건이 되고, 새 메인을 띄우지 않는다. 카드에서 값 고치기는 고친 뒤 충돌이 있거나 계획 밖 요청이 남으면 작업 준비됨처럼 사건이 되어 메인을 띄우고(AG-33), 그렇지 않으면 열린 메인이 있을 때만 사건이 된다. 열린 메인은 하위 Run이 끝날 때, 또는 열린 하위 Run이 없을 때 그 Case의 사건이 생기면 깨어난다(하위 Run이 사람을 기다리는 동안에는 깨우지 않는다). 사람의 답은 사건이 아니라 물은 전문 Agent가 받는다.
- 열린 메인이 있는 동안 새 작업은 대기열에 서고, 메인이 어떤 상태로 끝나든 대기열에서 1건이 올라간다(사건을 합치거나 미루는 판단은 3단계).

## 3. 도구

흐름: C = 계속, W = 대기, D = 끝.

**메인 전용**
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `CALL_AGENT(agent, 참조)` | 전문 Agent Run을 요청하고 결과를 기다린다. Agent 종류와 참조만 넘긴다: 재계획은 충돌 그룹과 주체 Unit과 접근(목록 값 하나: 변경 최소·지연 최소·희망 영역 우선. 짧은 문장을 덧붙일 수 있고 인용으로만 전해진다), 협의는 Supervisor가 고른 후보, 통지는 확정된 후보, 신고 대응은 신고. 하위 Run은 한 번에 하나다. 주체 Unit이 그 그룹에 움직일 수 있는 작업을 가졌는지 서버가 검사하고, ACTIVE Hold 중의 재계획·협의, 관련 사실이 바뀌지 않은 재호출(재계획은 같은 접근일 때), 고르지 않은 후보의 협의는 거절한다 | W |
| `WAIT()` | 사람의 결정(후보 승인·거절, Hold 해제)을 기다린다. 검토 대기 후보나 ACTIVE Hold가 있을 때만 유효하다 | W |
| `MERGE_EVENT(event)` / `DEFER_EVENT(event)` | 새 사건을 지금 일에 합치거나 뒤로 미룬다 (3단계) | C |
| `SEND_TO_REVIEW(group, candidate_set)` | 후보 묶음을 사람 검토 대기로 보낸다. 승인은 사람만 한다 (4단계. 지금은 검증을 통과한 후보를 서버가 검토 대기로 계산하고, Supervisor가 그 가운데 안을 고른다. 이 도구가 생기면 "어느 안을 보일지"만 메인으로 옮기고 고르기는 그대로 사람이 한다) | W |
| `ESCALATE(summary, needs)` | Supervisor에게 이관한다(서버 문구 통지가 남는다). 열린 하위 Run이나 아직 보지 않은 사건이 있으면 거절한다 | D |
| `CLOSE(summary)` | 맡은 일이 모두 닫혔을 때 끝낸다. 서버가 이 Case의 열린 일(검토 대기 후보, 통지 안 된 확정, 계획에 못 들어간 작업, 이 Case 작업의 충돌, 풀리지 않은 Hold)과 아직 보지 않은 사건이 없음을 확인한다 | D |

**조회** (읽기)
| 도구 | 하는 일 |
|---|---|
| `LOOKUP_TASKS(filter)` | 작업 찾기(유형·구역·자원·시간·문구) |
| `LOOKUP_RESOURCES(task \| type, zone, work_type)` | 쓸 수 있는 자원과 제외된 자원(이유 포함). Intake는 구역·작업 유형을 주면 유형을 가리지 않고 판정 결과를 받는다(작업 유형 기본 요구 조건은 서버가 붙인다). 유형으로 좁혔는데 없으면 다른 유형에서 쓸 수 있는 자원 수도 받는다 |
| `LOOKUP_ZONES(ref)` | 구역과 구역 관계 |
| `GET_GROUPS(batch)` | 서버가 계산한 엮임 그룹. 메인에게는 도구가 아니라 관찰로 준다(지금은 충돌을 공유 작업으로 묶은 그룹과 그룹별 Unit. 묶음 등록용 엮임 계산은 3단계) |
| `GET_STATE(scope)` | 상태 요약: 충돌·후보·검증·Hold·Run. 메인에게는 도구가 아니라 관찰로 준다 |

**알아보기** (계산, 후보를 등록하지 않음)
| 도구 | 하는 일 |
|---|---|
| `DIAGNOSE(conflict)` | 왜 안 풀리나: 막고 있는 조건·작업 |
| `TEST_RELAXATION(conflict, conditions)` | 무엇을 풀면 풀리나: 물어볼 수 있는 조건(Soft 조건, 다른 작업 이동)을 하나씩 풀어 해가 생기는지 |
| `PREVIEW(change)` | 가정한 변경을 등록 없이 검사 |
| `ANALYZE_IMPACT(change)` | 변경이 닿는 작업·담당자·규칙 |
| `COMPARE_CANDIDATES(ids)` | 후보 사이 지표 차이(변경 수·지연·비용·넘은 Soft 조건) |

알아보기 도구 모두 흐름은 C다.

**후보** (Replanning)
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `SOLVE(group, profile, scope, conditions, try_resources)` | Solver 하나. 후보를 등록하고 Validator에 넘긴다. `conditions`는 작업별 조건(시작 이후·이전, 시작 지정, 자원 지정, 희망 영역을 시작 범위로)이고 좁히기만 한다. 범위 안 작업을 모두 지정하면 그 배치 그대로를 검사하고, 해가 없으면 그 배치가 어긴 규칙을 돌려준다. 같은 범위·같은 조건은 다시 풀지 않는다. 살아 있는 후보와 전체 배정이 같으면 새 후보를 만들지 않고 그 후보를 돌려준다 | C |
| `SUBMIT_CANDIDATES(ids, recommended, reasons)` | 후보 묶음과 추천 이유를 결과로 낸다. 이유는 서버 지표 ID를 가리켜야 한다 | D |

**사람** (수신자는 Agent별로 서버가 고정)
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `ASK_REPORTER(question)` | 신고자에게 되묻는다 | C |
| `ASK_OWNER(owner, items)` | 담당자에게 변경 확인·조건 확인을 묶어서 묻는다 | C |
| `WAIT_FOR_REPLIES()` | 보낸 질문의 답을 기다린다 | W |
| `SEND_NOTICE(actor, tasks, message)` | 확정 뒤 통지(시간·구역·위험·조치) | C |

**제안** (사람 확인이 있어야 효력)
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `COMPLETE_TASK_BATCH(tasks)` | 확인된 값으로 작업 묶음을 만든다(확인 값과 다르면 거절) | D |
| `PROPOSE_FACT_UPDATE(task, old, new, evidence)` | 사실 수정안, Supervisor 확인 요청 | C |

**마무리·답변**
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `RETURN_RESULT(status, summary, paths)` | 전문 Agent가 결과를 돌려준다. status DONE / BLOCKED. 모델은 요약과 길만 쓰고, 결과물(후보·협의 상태·수정안 등)은 서버가 채운다 | D |
| `ANSWER(text, evidence_refs)` | 근거 ID를 붙여 답한다. 근거가 없으면 모른다고 답한다 | D |

### 결과의 길과 needs

- 길(path) 하나는 needs 묶음이다. 그 길의 needs가 모두 충족되면 다시 시도할 가치가 있다는 뜻이다. 서로 다른 방법은 다른 길로 낸다(최대 3개, 길마다 needs 최대 4개). 풀 길을 찾지 못했으면 0개다.
- need는 종류와 그 종류의 참조만 쓴다. 자유 문장은 없다. 서버는 참조가 가리키는 대상이 지금 사실에 있는지 검사하고, 아니면 거절한다(`NEED_INVALID`).
- Budget 소진은 need가 아니다. Run의 종료 사유로만 남는다.
- 열 수 있는 것(openers): 서버가 지금 사실에서 계산해 need 모양으로 준다. Replanning 관찰에 넣고, 막힌 결과에도 붙인다(모델이 길을 비워도 메인이 볼 것이 남는다). 서버는 엮지 않는다. 길은 모델이 엮는다.
  - 다른 Unit(`OTHER_UNIT`): 그 그룹에 움직일 수 있는 작업을 가진 다른 Unit.
  - 사실(`FACT_CHANGE`): 풀 초과 충돌의 풀(수량), 자원 조회에서 제외된 자원(사용 권한 없음 → 사용 권한, 가용 없음 → 가용 구간), 모든 범위에서 해가 없는 요청 작업의 시간창.
- need ID: 결과의 need마다 서버가 ID를 붙인다(Run·길·순번). 모델이 엮은 길의 need와 서버가 붙인 openers의 need는 결과와 기록에서 따로 남고, 메인은 어느 쪽 ID든 같은 방식으로 넘긴다.

| 종류 | 참조 | 서버 검증 |
|---|---|---|
| `OTHER_UNIT` | 충돌 그룹, Unit | 그 그룹에 움직일 수 있는 작업을 가진 Unit이고 그 Run의 acting unit이 아님 |
| `FACT_CHANGE` | 필드와 대상 하나(작업: 시간창·작업 시간·요청 철회, 자원: 가용 구간·사용 권한, 풀: 수량) | 대상이 있음 |
| `HUMAN_INFO` | 사람, 신고 또는 작업 | 사람과 대상이 있음. Intake는 요청자와 접수 중인 작업, Event Response는 신고자와 그 신고 |
| `HUMAN_DECISION` | 후보·신고·메시지 중 하나 | 대상이 있음 |

## 4. 스킬

| 스킬 | 열리는 조건 | 도구 | 쓰는 Agent |
|---|---|---|---|
| 조율 | 맡은 사건이 있음 | `CALL_AGENT`, `WAIT`, `ESCALATE`, `CLOSE` | Main |
| 상황 파악 | 항상 | 조회 도구 | 전부 |
| 작업 접수 | 작성 중인 작업 묶음이 있음 | `COMPLETE_TASK_BATCH` | Intake |
| 원인 찾기 | 풀리지 않은 충돌 그룹이 있음 | `DIAGNOSE`, `TEST_RELAXATION`, `PREVIEW` | Replanning |
| 후보 구성 | 충돌 그룹이 있음 | `SOLVE`, `COMPARE_CANDIDATES`, `SUBMIT_CANDIDATES` | Replanning |
| 거절 반영 | 이 Case 후보에 거절이나 담당자 이견이 있음 | `DIAGNOSE`, `SOLVE`, `COMPARE_CANDIDATES` | Replanning |
| 영향 분석 | 분석할 변경(신고·후보·가정)이 있음 | `ANALYZE_IMPACT`, `PREVIEW` | Replanning, Event Response, Coordination (Main은 4단계) |
| 신고자 질문 | 신고가 있음 | `ASK_REPORTER`, `WAIT_FOR_REPLIES` | Event Response |
| 협의 | 확인 대기 항목이나 이견이 있음 | `ASK_OWNER`, `WAIT_FOR_REPLIES` (이견은 결과에 담아 돌려준다) | Coordination |
| 통지 | 확정됐는데 통지하지 않은 대상이 있음 | `SEND_NOTICE` | Coordination |
| 사실 수정 | Hold가 걸린 신고가 있음 | `PROPOSE_FACT_UPDATE` | Event Response |
| 답변 | 질문이 있음 | `ANSWER`, `COMPARE_CANDIDATES` | Site Assistant |
| 마무리 | 항상 | `RETURN_RESULT` | 전문 Agent 전부 |

## 5. Agent별 허용 도구

| Agent | 허용 도구 |
|---|---|
| Main | `CALL_AGENT`, `WAIT`, `ESCALATE`, `CLOSE`. 조회·영향 분석·알아보기 도구는 4단계 |
| Intake | 조회, `COMPLETE_TASK_BATCH`, `RETURN_RESULT`. 사람 도구 없음 |
| Replanning | 조회, 알아보기 전부, `SOLVE`, `SUBMIT_CANDIDATES`, `RETURN_RESULT`. 사람 도구 없음 |
| Coordination | 조회, `ANALYZE_IMPACT`, `ASK_OWNER`, `WAIT_FOR_REPLIES`, `SEND_NOTICE`, `RETURN_RESULT` |
| Event Response | 조회, `ANALYZE_IMPACT`, `PREVIEW`, `ASK_REPORTER`, `WAIT_FOR_REPLIES`, `PROPOSE_FACT_UPDATE`, `RETURN_RESULT` |
| Site Assistant | 조회, `COMPARE_CANDIDATES`, `ANSWER` |

## 6. 현재 코드와의 대응 (옮긴 뒤 이 절은 지운다)

- Replanning `SOLVE_WITH_SCOPE`·`SOLVE_WITH_CONDITIONS`(조건) → `SOLVE`
- Replanning `LIST_ASSIGNABLE_RESOURCES`, Intake `LOOKUP_RESOURCE` → `LOOKUP_RESOURCES`
- Coordination `SEND_CHANGE_REQUEST`(후보의 변경 확인) → Coordination `ASK_OWNER`
- 담당자별 묶음 메시지(`ASK_OWNER(owner, items)`)는 3단계 묶음 등록 때 한다.
- Intake `COMPLETE_TASKSPEC(values, origins)` → `COMPLETE_TASK_BATCH`

## 7. 열린 값

- 사건을 합칠 때 메인 Budget에 더하는 양과 상한(3단계), 알아보기 계산 Budget
- 기준 프로필 목록(비용 중심·지연 중심·변경 최소 외)
- `ASK_*`를 보낸 뒤 바로 기다릴지(지금 방식), 다른 일을 하다 `WAIT_FOR_REPLIES`로 기다릴지(위 도구 표)
