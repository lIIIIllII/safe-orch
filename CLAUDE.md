# SAFE-ORCH

설계 기준: `docs/SAFE-ORCH_Project_Blueprint_v1.2.3.md` (저장소 §5.4, Agent 실행 계층 §11, 스택·구조 §14)

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
