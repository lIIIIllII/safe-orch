# SAFE-ORCH

설계 의도: `docs/SAFE-ORCH_Blueprint_v2.0.md` (무엇을 왜 만드나. 구현 상태는 적지 않음)
Agent·도구·스킬: `docs/Agent_도구_스킬.md` (목록과 규칙. 지침 원문은 코드)
결정 기록: `docs/결정_기록.md` (되돌려질 위험이 있는 결정만. 결정 한 줄 + 이유 한 줄, 200줄 상한. 새 결정은 영역별 마지막 번호 다음. 바뀌면 고쳐 쓰고, 구현 기록은 커밋 메시지에)
기존 자산: `docs/기존_자산_지도.md` (새로 만들기 전에 먼저 찾기. 새 테이블·환경 변수·공용 함수를 만들면 지도에 한 줄 더하기)
문서와 코드가 다르면 구현을 멈추고 확인. 코드 주석은 결정 기록 번호(예: AG-05)만 가리킨다.

## 작업 규칙
- 패키지 설치 전 venv 먼저(`uv venv --python 3.12`), 설치는 `uv add`(개발용 `uv add --dev`). pip 직접 사용 금지.
- 최소 변경. 기존 구조 유지. 파일 전체 재작성 금지(부분 수정).

## 저장소
- SQLite + 표준 `sqlite3`. ORM 없음.
- 모든 쓰기는 `store.write()` 안에서만. 트랜잭션 중첩 금지 — 하위 함수에는 `tx`를 인자로 전달.
- 트랜잭션 안에서 LLM·Solver·파일 I/O·`await` 금지.

## 경계
- `app/validator`는 `app.solver` import 금지.
- `app/agents/graph.py`·`app/agents/specs`는 `app.store`·`app.commands` import 금지(Tool Gateway만 가능).
- `langgraph.prebuilt`, `create_agent`, checkpointer, `interrupt` 사용 금지.
- Tool Gateway에 승인·확정·Hold 해제·Proposal 확인 함수 금지.
- 1 step = step 예약 → LLM 1회 → Action 1개.

## 실행
```
cd backend && uv run uvicorn app.main:app --reload --port 8000
cd backend && uv run pytest
cd frontend && npm run dev
```
- Agent 자동 시작 스위치: `COORDINATION_ENABLED`·`EVENT_RESPONSE_ENABLED`(기본 true). 테스트는 .env를 읽지 않고 conftest가 둘 다 끈다.
- 현장의 지금: `SITE_NOW`(ISO 8601, 오프셋 필수). 비면 실제 시계. 테스트는 conftest가 고정한다.
- 평가(실제 모델): `cd backend && uv run python -m evals.run --scenario S1|S2|S3|all [--hidden] [--runs 10]`, 비교는 `uv run python -m evals.compare a.jsonl b.jsonl`. 숨긴 판 파일(`evals/scenarios/*/hidden/`)은 지침을 고치는 동안 열지 않는다.
