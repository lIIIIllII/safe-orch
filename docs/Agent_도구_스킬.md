# SAFE-ORCH Agent·도구·스킬

2026-10-03. 목록과 규칙만 적는다. 왜 이런 구조인지는 블루프린트 §5, 지침 원문은 코드(`backend/app/agents/`)에 있다. 도구 이름은 제안이다.

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
  - 담당자가 거절한 값은 다시 묻지 못한다. 그 작업에 관련된 버전이 거절 이후 바뀌었을 때만, 바뀐 내용을 서버 문구에 붙여 다시 물을 수 있다.
- 1 step = step 예약 → LLM 1회 → Action 1개. 도구 실행은 Tool Gateway만 거친다.
- 도구의 시각 인자는 현장 날짜·시각 문자열("YYYY-MM-DD HH:MM")이다. 서버가 분으로 바꾸고, 형식이 틀리거나 Horizon 밖이면 거절한다.

## 2. Agent

| Agent | Goal | 끝나는 방식 | Budget |
|---|---|---|---|
| Main | 맡은 사건을 끝까지 처리한다 | 맡은 일이 모두 닫히면 `CLOSE`. 풀 수 없으면 Supervisor에게 `ESCALATE` | step, 전문 Agent 호출 수 (값 미정). 사건을 합치면 더하되 상한 |
| Intake | 확인된 작업 묶음 | `RETURN_RESULT` | step 12, 사람 라운드 3 |
| Replanning | 검증 가능한 후보 묶음과 서버 지표 기반 설명 | `RETURN_RESULT` | step 15, Solver 6, 알아보기 계산 (값 미정) |
| Coordination | 협의 항목 해소, 확정 뒤 통지 | `RETURN_RESULT` | step 12, 사람 라운드 (값 미정) |
| Event Response | 신고의 대상·영향·사실 수정안 | `RETURN_RESULT` | step 10, 사람 라운드 2 |
| Site Assistant | 근거 있는 답 | `ANSWER` | 질문당 step 6 |

- LLM 시도 Budget은 모든 Agent가 step × 2다.
- 전문 Agent는 다른 Agent를 부르지 않고 사람에게 이관하지도 않는다. 막히면 `RETURN_RESULT(BLOCKED)`에 이유와 필요한 것을 담아 메인에게 돌려준다. Supervisor 이관은 메인만 한다.
- 메인은 한 번에 하나다. 새 사건은 열린 메인을 깨우고, 열린 메인이 없으면 새 메인을 띄운다.

## 3. 도구

흐름: C = 계속, W = 대기, D = 끝.

**메인 전용**
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `CALL_AGENT(agent, input)` | 전문 Agent Run을 요청하고 결과를 기다린다 | W |
| `MERGE_EVENT(event)` / `DEFER_EVENT(event)` | 새 사건을 지금 일에 합치거나 뒤로 미룬다 | C |
| `SEND_TO_REVIEW(group, candidate_set)` | 후보 묶음을 사람 검토 대기로 보낸다. 승인은 사람만 한다 | W |
| `ESCALATE(reason, needs)` | Supervisor에게 이관한다 | D |
| `CLOSE(summary)` | 맡은 일이 모두 닫혔을 때 끝낸다(서버가 열린 일 없음 확인) | D |

**조회** (읽기)
| 도구 | 하는 일 |
|---|---|
| `LOOKUP_TASKS(filter)` | 작업 찾기(유형·구역·자원·시간·문구) |
| `LOOKUP_RESOURCES(task \| type, zone, work_type)` | 쓸 수 있는 자원과 제외된 자원(이유 포함). Intake는 구역·작업 유형을 주면 유형을 가리지 않고 판정 결과를 받는다(작업 유형 기본 요구 조건은 서버가 붙인다). 유형으로 좁혔는데 없으면 다른 유형에서 쓸 수 있는 자원 수도 받는다 |
| `LOOKUP_ZONES(ref)` | 구역과 구역 관계 |
| `GET_GROUPS(batch)` | 서버가 계산한 엮임 그룹 |
| `GET_STATE(scope)` | 상태 요약: 충돌·후보·검증·Hold·Run |

**알아보기** (계산, 후보를 등록하지 않음)
| 도구 | 하는 일 |
|---|---|
| `DIAGNOSE(conflict)` | 왜 안 풀리나: 막고 있는 조건·작업 |
| `TEST_RELAXATION(conflict, conditions)` | 무엇을 풀면 풀리나: 물어볼 수 있는 조건(Soft 조건, 자원 축, 다른 작업 이동)을 하나씩 풀어 해가 생기는지 |
| `PREVIEW(change)` | 가정한 변경을 등록 없이 검사 |
| `ANALYZE_IMPACT(change)` | 변경이 닿는 작업·담당자·규칙 |
| `COMPARE_CANDIDATES(ids)` | 후보 사이 지표 차이(변경 수·지연·비용·넘은 Soft 조건) |

알아보기 도구 모두 흐름은 C다.

**후보** (Replanning)
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `SOLVE(group, profile, scope, extra_constraints, try_resources)` | Solver 하나. 후보를 등록하고 Validator에 넘긴다 | C |
| `SUBMIT_CANDIDATES(ids, recommended, reasons)` | 후보 묶음과 추천 이유를 결과로 낸다. 이유는 서버 지표 ID를 가리켜야 한다 | D |

**사람** (수신자는 Agent별로 서버가 고정)
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `ASK_REQUESTER(fields, question)` | 요청자에게 빠진 값을 묶어서 묻는다. 필드별 판단(상태: 받음·모호·빠짐, 현재 값)이 필수이고, 물을 필드는 서버가 그 판단에서 도출한다(모호·빠짐 전부). 모호·빠짐이 없으면 거절한다 | C |
| `REQUEST_CONFIRMATION(values)` | 요청자에게 뽑은 값 확인을 받는다 | C |
| `ASK_REPORTER(question)` | 신고자에게 되묻는다 | C |
| `ASK_OWNER(owner, items)` | 담당자에게 변경 확인·조건 확인을 묶어서 묻는다 | C |
| `WAIT_FOR_REPLIES()` | 보낸 질문의 답을 기다린다 | W |
| `SEND_NOTICE(actor, tasks, message)` | 확정 뒤 통지(시간·구역·위험·조치) | C |

**제안** (사람 확인이 있어야 효력)
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `COMPLETE_TASK_BATCH(tasks)` | 확인된 값으로 작업 묶음을 만든다(확인 값과 다르면 거절) | D |
| `DRAFT_CONSTRAINT(reply, task, axes)` | 담당자 이견을 제약 초안으로, 그 담당자에게 확인 요청 | C |
| `PROPOSE_FACT_UPDATE(task, old, new, evidence)` | 사실 수정안, Supervisor 확인 요청 | C |

**마무리·답변**
| 도구 | 하는 일 | 흐름 |
|---|---|---|
| `RETURN_RESULT(status, summary, needs)` | 전문 Agent가 메인에게 결과를 돌려준다. status DONE / BLOCKED | D |
| `ANSWER(text, evidence_refs)` | 근거 ID를 붙여 답한다. 근거가 없으면 모른다고 답한다 | D |

## 4. 스킬

| 스킬 | 열리는 조건 | 도구 | 쓰는 Agent |
|---|---|---|---|
| 조율 | 맡은 사건이 있음 | 메인 전용 도구 | Main |
| 상황 파악 | 항상 | 조회 도구 | 전부 |
| 작업 접수 | 작성 중인 작업 묶음이 있음 | `REQUEST_CONFIRMATION`, `COMPLETE_TASK_BATCH` | Intake |
| 원인 찾기 | 풀리지 않은 충돌 그룹이 있음 | `DIAGNOSE`, `TEST_RELAXATION`, `PREVIEW` | Replanning |
| 후보 구성 | 충돌 그룹이 있음 | `SOLVE`, `COMPARE_CANDIDATES`, `SUBMIT_CANDIDATES` | Replanning |
| 거절 반영 | 이 Case 후보에 거절·이견이 있음 | `DIAGNOSE`, `SOLVE`, `COMPARE_CANDIDATES` | Replanning |
| 영향 분석 | 분석할 변경(신고·후보·가정)이 있음 | `ANALYZE_IMPACT`, `PREVIEW` | Main, Replanning, Event Response, Coordination |
| 요청자 질문 | 작성 중인 작업 묶음이 있음 | `ASK_REQUESTER`, `WAIT_FOR_REPLIES` | Intake |
| 신고자 질문 | 신고가 있음 | `ASK_REPORTER`, `WAIT_FOR_REPLIES` | Event Response |
| 협의 | 확인 대기 항목이나 이견이 있음 | `ASK_OWNER`, `WAIT_FOR_REPLIES`, `DRAFT_CONSTRAINT` | Coordination |
| 통지 | 확정됐는데 통지하지 않은 대상이 있음 | `SEND_NOTICE` | Coordination |
| 사실 수정 | Hold가 걸린 신고가 있음 | `PROPOSE_FACT_UPDATE` | Event Response |
| 답변 | 질문이 있음 | `ANSWER`, `COMPARE_CANDIDATES` | Site Assistant |
| 마무리 | 항상 | `RETURN_RESULT` | 전문 Agent 전부 |

## 5. Agent별 허용 도구

| Agent | 허용 도구 |
|---|---|
| Main | 메인 전용, 조회, `ANALYZE_IMPACT`, `PREVIEW`, `COMPARE_CANDIDATES` |
| Intake | 조회, `ASK_REQUESTER`, `REQUEST_CONFIRMATION`, `WAIT_FOR_REPLIES`, `COMPLETE_TASK_BATCH`, `RETURN_RESULT` |
| Replanning | 조회, 알아보기 전부, `SOLVE`, `SUBMIT_CANDIDATES`, `RETURN_RESULT`. 사람 도구 없음 |
| Coordination | 조회, `ANALYZE_IMPACT`, `ASK_OWNER`, `WAIT_FOR_REPLIES`, `DRAFT_CONSTRAINT`, `SEND_NOTICE`, `RETURN_RESULT` |
| Event Response | 조회, `ANALYZE_IMPACT`, `PREVIEW`, `ASK_REPORTER`, `WAIT_FOR_REPLIES`, `PROPOSE_FACT_UPDATE`, `RETURN_RESULT` |
| Site Assistant | 조회, `COMPARE_CANDIDATES`, `ANSWER` |

## 6. 현재 코드와의 대응 (옮긴 뒤 이 절은 지운다)

- Replanning `SOLVE_WITH_SCOPE`·`TRY_ALTERNATIVE_RESOURCE` → `SOLVE`
- Replanning `LIST_ASSIGNABLE_RESOURCES`, Intake `LOOKUP_RESOURCE` → `LOOKUP_RESOURCES`
- Replanning `ASK_TASK_OWNER`, Coordination `SEND_CHANGE_REQUEST` → Coordination `ASK_OWNER`
- Intake `ASK_CLARIFICATION` → `ASK_REQUESTER`, `COMPLETE_TASKSPEC` → `COMPLETE_TASK_BATCH`
- 각 Agent의 `ESCALATE`·`ESCALATE_NO_SOLUTION`, Coordination `REPORT_TO_SUPERVISOR` → 전문 Agent는 `RETURN_RESULT`, 이관은 메인 `ESCALATE`
- Coordinator가 사건마다 전문 Agent를 자동 시작하던 것 → 메인이 `CALL_AGENT`로 부른다

## 7. 열린 값

- 메인 Budget 상한, 사건을 합칠 때 더하는 양, Coordination 사람 라운드, 알아보기 계산 Budget
- 기준 프로필 목록(비용 중심·지연 중심·변경 최소 외)
- `ASK_*`를 보낸 뒤 바로 기다릴지(지금 방식), 다른 일을 하다 `WAIT_FOR_REPLIES`로 기다릴지(위 도구 표)
