-- SAFE-ORCH schema (설계서 §5.4, 우선순위 문서 부록 A.2). schema_version 2.
-- 테이블은 기능 구현 단계에서 추가하고, 추가할 때마다 schema_version을 올린 뒤 reset한다.
-- 적용은 db.init_db()가 빈 DB에서 한 트랜잭션으로 한다.
-- 복합 필드는 JSON TEXT + CHECK(json_valid). 시간은 Horizon 원점 기준 정수 분.

CREATE TABLE schema_meta (
    schema_version INTEGER NOT NULL
);

-- ── 현장·Pack 사실 ─────────────────────────────────────────────

CREATE TABLE site (
    site_id           TEXT PRIMARY KEY,
    pack_hash         TEXT NOT NULL,
    horizon_start_utc TEXT NOT NULL,
    horizon_minutes   INTEGER NOT NULL CHECK (horizon_minutes > 0),
    context_version   INTEGER NOT NULL CHECK (context_version >= 0),
    plan_revision     INTEGER NOT NULL CHECK (plan_revision >= 0)
);

CREATE TABLE work_unit (
    site_id   TEXT NOT NULL REFERENCES site (site_id),
    unit_id   TEXT NOT NULL,
    name      TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    PRIMARY KEY (site_id, unit_id)
);

CREATE TABLE actor (
    site_id  TEXT NOT NULL REFERENCES site (site_id),
    actor_id TEXT NOT NULL,
    name     TEXT NOT NULL,
    unit_id  TEXT NOT NULL,
    roles    TEXT NOT NULL CHECK (json_valid(roles)),
    PRIMARY KEY (site_id, actor_id),
    FOREIGN KEY (site_id, unit_id) REFERENCES work_unit (site_id, unit_id)
);

CREATE TABLE zone (
    site_id TEXT NOT NULL REFERENCES site (site_id),
    zone_id TEXT NOT NULL,
    PRIMARY KEY (site_id, zone_id)
);

-- SAME은 저장하지 않는다. ADJACENT는 양방향 2행, BELOW는 (upper, lower) 1행.
CREATE TABLE zone_relation (
    site_id  TEXT NOT NULL REFERENCES site (site_id),
    zone_a   TEXT NOT NULL,
    zone_b   TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('ADJACENT', 'BELOW')),
    PRIMARY KEY (site_id, zone_a, zone_b),
    CHECK (zone_a <> zone_b),
    FOREIGN KEY (site_id, zone_a) REFERENCES zone (site_id, zone_id),
    FOREIGN KEY (site_id, zone_b) REFERENCES zone (site_id, zone_id)
);

CREATE TABLE resource (
    site_id             TEXT NOT NULL REFERENCES site (site_id),
    resource_id         TEXT NOT NULL,
    resource_type       TEXT NOT NULL,
    owner_unit_id       TEXT NOT NULL,
    allowed_unit_ids    TEXT NOT NULL CHECK (json_valid(allowed_unit_ids)),
    capacity            INTEGER NOT NULL CHECK (capacity = 1),
    available_intervals TEXT NOT NULL CHECK (json_valid(available_intervals)),
    PRIMARY KEY (site_id, resource_id),
    FOREIGN KEY (site_id, owner_unit_id) REFERENCES work_unit (site_id, unit_id)
);

-- 새 revision은 INSERT로만 만든다. 현재 revision = MAX(revision).
-- hazard_tags 컬럼은 두지 않는다(I-14, 조회 시 Pack의 work_type에서 도출).
CREATE TABLE task (
    site_id                TEXT NOT NULL REFERENCES site (site_id),
    task_id                TEXT NOT NULL,
    revision               INTEGER NOT NULL CHECK (revision >= 1),
    unit_id                TEXT NOT NULL,
    owner_actor_id         TEXT NOT NULL,
    work_type              TEXT NOT NULL,
    zone_id                TEXT NOT NULL,
    duration               INTEGER NOT NULL CHECK (duration > 0),
    earliest_start         INTEGER NOT NULL,
    latest_start           INTEGER NOT NULL,
    latest_end             INTEGER NOT NULL,
    required_resource_type TEXT,
    requested_resource_id  TEXT,
    predecessors           TEXT NOT NULL CHECK (json_valid(predecessors)),
    movable                TEXT NOT NULL CHECK (json_valid(movable)),
    fields                 TEXT NOT NULL CHECK (json_valid(fields)),
    lifecycle              TEXT NOT NULL CHECK (lifecycle IN ('DRAFT', 'NEEDS_INFO', 'READY')),
    PRIMARY KEY (site_id, task_id, revision),
    CHECK (earliest_start <= latest_start),
    CHECK (earliest_start + duration <= latest_end),
    FOREIGN KEY (site_id, unit_id) REFERENCES work_unit (site_id, unit_id),
    FOREIGN KEY (site_id, owner_actor_id) REFERENCES actor (site_id, actor_id),
    FOREIGN KEY (site_id, zone_id) REFERENCES zone (site_id, zone_id),
    FOREIGN KEY (site_id, requested_resource_id) REFERENCES resource (site_id, resource_id)
);

-- ── 계산·검증 기록 (불변) ──────────────────────────────────────

CREATE TABLE snapshot (
    snapshot_id   TEXT PRIMARY KEY,
    site_id       TEXT NOT NULL REFERENCES site (site_id),
    snapshot_hash TEXT NOT NULL,
    content       TEXT NOT NULL CHECK (json_valid(content))
);

CREATE TABLE search_spec (
    search_spec_id        TEXT PRIMARY KEY,
    site_id               TEXT NOT NULL REFERENCES site (site_id),
    hash                  TEXT NOT NULL,
    snapshot_id           TEXT NOT NULL REFERENCES snapshot (snapshot_id),
    acting_unit_id        TEXT NOT NULL,
    scope_level           TEXT NOT NULL CHECK (scope_level IN ('L0', 'L1', 'L2')),
    axes                  TEXT NOT NULL CHECK (json_valid(axes)),
    resource_alternatives TEXT NOT NULL CHECK (json_valid(resource_alternatives)),
    time_limit_s          INTEGER NOT NULL CHECK (time_limit_s > 0),
    FOREIGN KEY (site_id, acting_unit_id) REFERENCES work_unit (site_id, unit_id)
);

-- stage2는 1단계가 OPTIMAL이 아니면 실행하지 않으므로 NULL 허용.
CREATE TABLE solver_result (
    solver_result_id TEXT PRIMARY KEY,
    site_id          TEXT NOT NULL REFERENCES site (site_id),
    search_spec_id   TEXT NOT NULL REFERENCES search_spec (search_spec_id),
    stage1           TEXT NOT NULL CHECK (json_valid(stage1)),
    stage2           TEXT CHECK (stage2 IS NULL OR json_valid(stage2)),
    chosen_stage     INTEGER CHECK (chosen_stage IN (1, 2))
);

-- 상태 컬럼 없음: 거절은 Decision, STALE은 조회 시 계산.
CREATE TABLE candidate (
    candidate_id       TEXT PRIMARY KEY,
    site_id            TEXT NOT NULL REFERENCES site (site_id),
    snapshot_id        TEXT NOT NULL REFERENCES snapshot (snapshot_id),
    search_spec_id     TEXT REFERENCES search_spec (search_spec_id),
    search_spec_hash   TEXT,
    solver_result_id   TEXT REFERENCES solver_result (solver_result_id),
    base_plan_revision INTEGER NOT NULL CHECK (base_plan_revision >= 0),
    context_version    INTEGER NOT NULL CHECK (context_version >= 0),
    pack_hash          TEXT NOT NULL,
    assignments        TEXT NOT NULL CHECK (json_valid(assignments)),
    candidate_hash     TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('REPLAN', 'RECONFIRM')),
    CHECK (kind = 'RECONFIRM'
           OR (search_spec_id IS NOT NULL AND search_spec_hash IS NOT NULL
               AND solver_result_id IS NOT NULL))
);

-- STALE은 저장하지 않는다(§8, 조회 시 계산).
CREATE TABLE validation (
    validation_id TEXT PRIMARY KEY,
    site_id       TEXT NOT NULL REFERENCES site (site_id),
    candidate_id  TEXT NOT NULL REFERENCES candidate (candidate_id),
    status        TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'INCOMPLETE')),
    checks        TEXT NOT NULL CHECK (json_valid(checks))
);

CREATE TABLE audit (
    audit_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id                TEXT NOT NULL REFERENCES site (site_id),
    command                TEXT NOT NULL,
    actor_id               TEXT,
    before_context_version INTEGER NOT NULL CHECK (before_context_version >= 0),
    after_context_version  INTEGER NOT NULL CHECK (after_context_version >= 0),
    before_plan_revision   INTEGER NOT NULL CHECK (before_plan_revision >= 0),
    after_plan_revision    INTEGER NOT NULL CHECK (after_plan_revision >= 0),
    reason_code            TEXT,
    payload                TEXT NOT NULL CHECK (json_valid(payload))
);

-- ── 확정 계획 ──────────────────────────────────────────────────

-- candidate_id는 R0만 NULL.
CREATE TABLE plan (
    site_id                   TEXT NOT NULL REFERENCES site (site_id),
    plan_revision             INTEGER NOT NULL CHECK (plan_revision >= 0),
    assignments               TEXT NOT NULL CHECK (json_valid(assignments)),
    candidate_id              TEXT REFERENCES candidate (candidate_id),
    committed_context_version INTEGER NOT NULL CHECK (committed_context_version >= 0),
    PRIMARY KEY (site_id, plan_revision),
    UNIQUE (site_id, candidate_id),
    CHECK ((plan_revision = 0) = (candidate_id IS NULL))
);

-- ── 불변 트리거 (I-03) ─────────────────────────────────────────

CREATE TRIGGER snapshot_no_update BEFORE UPDATE ON snapshot
BEGIN SELECT RAISE(ABORT, 'immutable: snapshot'); END;
CREATE TRIGGER snapshot_no_delete BEFORE DELETE ON snapshot
BEGIN SELECT RAISE(ABORT, 'immutable: snapshot'); END;

CREATE TRIGGER search_spec_no_update BEFORE UPDATE ON search_spec
BEGIN SELECT RAISE(ABORT, 'immutable: search_spec'); END;
CREATE TRIGGER search_spec_no_delete BEFORE DELETE ON search_spec
BEGIN SELECT RAISE(ABORT, 'immutable: search_spec'); END;

CREATE TRIGGER solver_result_no_update BEFORE UPDATE ON solver_result
BEGIN SELECT RAISE(ABORT, 'immutable: solver_result'); END;
CREATE TRIGGER solver_result_no_delete BEFORE DELETE ON solver_result
BEGIN SELECT RAISE(ABORT, 'immutable: solver_result'); END;

CREATE TRIGGER candidate_no_update BEFORE UPDATE ON candidate
BEGIN SELECT RAISE(ABORT, 'immutable: candidate'); END;
CREATE TRIGGER candidate_no_delete BEFORE DELETE ON candidate
BEGIN SELECT RAISE(ABORT, 'immutable: candidate'); END;

CREATE TRIGGER validation_no_update BEFORE UPDATE ON validation
BEGIN SELECT RAISE(ABORT, 'immutable: validation'); END;
CREATE TRIGGER validation_no_delete BEFORE DELETE ON validation
BEGIN SELECT RAISE(ABORT, 'immutable: validation'); END;

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'immutable: audit'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'immutable: audit'); END;
