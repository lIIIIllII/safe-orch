"""어려운 시나리오 평가 하네스.

실제 모델로 시나리오를 돌리고 DB 사실로 등급을 매긴다(Action 이름을 보지 않는다, EV-01).
    cd backend && uv run python -m evals.run --scenario S1|S2|S3|all [--hidden] [--runs 10]
    cd backend && uv run python -m evals.compare a.jsonl b.jsonl [--force]
"""

# 하네스(실행기·사람 역할·판정기)의 판정에 영향을 주는 변경이 있으면 올린다. 다르면 비교를 거절한다 (EV-04)
# 2: 값 확인에서 요구 조건·수요 판정. 3: 수요는 서버 적용 값으로, 물은 필드는 step 결과에서
# 4: 메인 Agent(해 없음 판정은 메인의 이관, 준비 스크립트에 메인 줄)
HARNESS_VERSION = "4"
# 지금 코드의 단계. 단계를 닫을 때 올린다. 시나리오 정의의 from_stage가 이 값보다 크면 기록만 한다
EVAL_STAGE = 1
