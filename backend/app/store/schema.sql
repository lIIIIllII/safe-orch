-- SAFE-ORCH schema (설계서 §5.4). 테이블은 기능 구현 단계에서 추가한다.

CREATE TABLE IF NOT EXISTS schema_meta (
    schema_version INTEGER NOT NULL
);
