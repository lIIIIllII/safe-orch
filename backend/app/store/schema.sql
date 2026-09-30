-- SAFE-ORCH schema (설계서 §5.4, 우선순위 문서 부록 A.2·A.14). schema_version 3.
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
    payload                TEXT NOT NULL CHECK (json_valid(payload)),
    -- 서버 시각(UTC ISO). 로직·hash에는 쓰지 않고 소요 시간 측정에만 쓴다 (A.14)
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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

-- ── 명령·결정·Event·Hold (부록 A.14) ───────────────────────────

-- 멱등 결과 (§11.3-6). RETRYABLE_ERROR는 저장하지 않는다.
CREATE TABLE command_result (
    idempotency_key TEXT PRIMARY KEY,
    site_id         TEXT NOT NULL REFERENCES site (site_id),
    command_type    TEXT NOT NULL,
    actor_id        TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('APPLIED', 'REJECTED')),
    reason_codes    TEXT NOT NULL CHECK (json_valid(reason_codes)),
    result_refs     TEXT NOT NULL CHECK (json_valid(result_refs)),
    response        TEXT NOT NULL CHECK (json_valid(response)),
    -- 서버 시각(UTC ISO). 로직·hash에는 쓰지 않고 소요 시간 측정에만 쓴다 (A.14)
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- reason_code는 REJECT만 (§9.2). WAIVE의 comment 필수는 명령에서 검사한다.
CREATE TABLE decision (
    decision_id     TEXT PRIMARY KEY,
    site_id         TEXT NOT NULL REFERENCES site (site_id),
    type            TEXT NOT NULL CHECK (type IN ('APPROVE', 'REJECT', 'WAIVE')),
    candidate_id    TEXT NOT NULL REFERENCES candidate (candidate_id),
    validation_id   TEXT NOT NULL REFERENCES validation (validation_id),
    actor_id        TEXT NOT NULL,
    reason_code     TEXT CHECK (reason_code IS NULL OR reason_code IN
                        ('TASK_IMMOVABLE', 'RESOURCE_UNAVAILABLE', 'TIME_WINDOW_UNACCEPTABLE',
                         'PREFERENCE', 'OTHER')),
    target_task_ids TEXT NOT NULL CHECK (json_valid(target_task_ids)),
    axes            TEXT NOT NULL CHECK (json_valid(axes)),
    comment         TEXT NOT NULL,
    context_version INTEGER NOT NULL CHECK (context_version >= 0),
    CHECK ((type = 'REJECT') = (reason_code IS NOT NULL)),
    FOREIGN KEY (site_id, actor_id) REFERENCES actor (site_id, actor_id)
);

-- task revision에 묶지 않는다(I-10). task PK에 revision이 있어 존재는 명령에서 검사한다.
CREATE TABLE feedback_constraint (
    constraint_id           TEXT PRIMARY KEY,
    site_id                 TEXT NOT NULL REFERENCES site (site_id),
    task_id                 TEXT NOT NULL,
    frozen_axes             TEXT NOT NULL
                                CHECK (json_valid(frozen_axes) AND json_array_length(frozen_axes) > 0),
    source_type             TEXT NOT NULL CHECK (source_type IN ('DECISION', 'PROPOSAL')),
    source_id               TEXT NOT NULL,
    created_context_version INTEGER NOT NULL CHECK (created_context_version >= 0)
);

CREATE TABLE event (
    event_id          TEXT PRIMARY KEY,
    site_id           TEXT NOT NULL REFERENCES site (site_id),
    source_event_id   TEXT NOT NULL,
    event_type        TEXT NOT NULL CHECK (event_type IN ('DELAY', 'OTHER')),
    reporter_actor_id TEXT NOT NULL,
    text              TEXT NOT NULL,
    target_task_id    TEXT,
    body_hash         TEXT NOT NULL,
    context_version   INTEGER NOT NULL CHECK (context_version >= 0),
    UNIQUE (site_id, source_event_id),
    FOREIGN KEY (site_id, reporter_actor_id) REFERENCES actor (site_id, actor_id)
);

-- ACTIVE → RELEASED 한 번만(트리거). 삭제 금지.
CREATE TABLE hold (
    hold_id                  TEXT PRIMARY KEY,
    site_id                  TEXT NOT NULL REFERENCES site (site_id),
    event_id                 TEXT NOT NULL REFERENCES event (event_id),
    scope                    TEXT NOT NULL CHECK (scope IN ('TASK', 'SITE')),
    task_id                  TEXT,
    status                   TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED')),
    created_context_version  INTEGER NOT NULL CHECK (created_context_version >= 0),
    resolution               TEXT CHECK (resolution IS NULL
                                         OR resolution IN ('FACT_CONFIRMED', 'NO_CHANGE')),
    released_by              TEXT,
    released_context_version INTEGER CHECK (released_context_version >= 0),
    CHECK ((scope = 'TASK') = (task_id IS NOT NULL)),
    CHECK ((status = 'RELEASED') = (resolution IS NOT NULL AND released_by IS NOT NULL
                                     AND released_context_version IS NOT NULL))
);

-- scope: TIME {start_min, start_max} / RESOURCE {resource_ids} (§5.1, §9.3)
CREATE TABLE consent (
    consent_id              TEXT PRIMARY KEY,
    site_id                 TEXT NOT NULL REFERENCES site (site_id),
    task_id                 TEXT NOT NULL,
    task_revision           INTEGER NOT NULL,
    owner_actor_id          TEXT NOT NULL,
    axis                    TEXT NOT NULL CHECK (axis IN ('TIME', 'RESOURCE')),
    scope                   TEXT NOT NULL CHECK (json_valid(scope)),
    source_ref              TEXT NOT NULL,
    created_context_version INTEGER NOT NULL CHECK (created_context_version >= 0),
    FOREIGN KEY (site_id, task_id, task_revision) REFERENCES task (site_id, task_id, revision),
    FOREIGN KEY (site_id, owner_actor_id) REFERENCES actor (site_id, actor_id)
);

-- 후보당 1개(§5.4). item의 base_status만 저장하고 상태는 조회 시 계산한다.
CREATE TABLE consultation (
    candidate_id TEXT PRIMARY KEY REFERENCES candidate (candidate_id),
    site_id      TEXT NOT NULL REFERENCES site (site_id),
    items        TEXT NOT NULL CHECK (json_valid(items))
);

-- 후속 작업 (§5.1, §11.3). run_id FK는 agent_run 테이블과 함께 3단계에서 추가한다.
CREATE TABLE dispatch_job (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         TEXT NOT NULL REFERENCES site (site_id),
    kind            TEXT NOT NULL CHECK (kind IN ('START_RUN', 'RESUME_RUN', 'CONTINUE_RUN',
                                                  'VALIDATE', 'BUILD_CONSULTATION', 'RECHECK')),
    run_id          TEXT,
    wait_generation INTEGER CHECK (wait_generation >= 0),
    payload         TEXT NOT NULL CHECK (json_valid(payload)),
    dedupe_key      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'CLAIMED', 'DONE', 'FAILED')),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error      TEXT,
    UNIQUE (site_id, dedupe_key),
    CHECK (kind <> 'RESUME_RUN' OR (run_id IS NOT NULL AND wait_generation IS NOT NULL))
);

-- Run당 PENDING RESUME 1개 (§5.1)
CREATE UNIQUE INDEX dispatch_job_pending_resume ON dispatch_job (run_id)
    WHERE kind = 'RESUME_RUN' AND status = 'PENDING';

-- ── Hold 전이 트리거 (§5.4 "Hold는 개별 해제만") ────────────────

CREATE TRIGGER hold_release_only BEFORE UPDATE ON hold
WHEN OLD.status <> 'ACTIVE' OR NEW.status <> 'RELEASED'
     OR NEW.hold_id <> OLD.hold_id OR NEW.site_id <> OLD.site_id OR NEW.event_id <> OLD.event_id
     OR NEW.scope <> OLD.scope OR NEW.task_id IS NOT OLD.task_id
     OR NEW.created_context_version <> OLD.created_context_version
BEGIN SELECT RAISE(ABORT, 'hold: only ACTIVE -> RELEASED'); END;
CREATE TRIGGER hold_no_delete BEFORE DELETE ON hold
BEGIN SELECT RAISE(ABORT, 'hold: no delete'); END;

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

CREATE TRIGGER command_result_no_update BEFORE UPDATE ON command_result
BEGIN SELECT RAISE(ABORT, 'immutable: command_result'); END;
CREATE TRIGGER command_result_no_delete BEFORE DELETE ON command_result
BEGIN SELECT RAISE(ABORT, 'immutable: command_result'); END;

CREATE TRIGGER decision_no_update BEFORE UPDATE ON decision
BEGIN SELECT RAISE(ABORT, 'immutable: decision'); END;
CREATE TRIGGER decision_no_delete BEFORE DELETE ON decision
BEGIN SELECT RAISE(ABORT, 'immutable: decision'); END;

CREATE TRIGGER feedback_constraint_no_update BEFORE UPDATE ON feedback_constraint
BEGIN SELECT RAISE(ABORT, 'immutable: feedback_constraint'); END;
CREATE TRIGGER feedback_constraint_no_delete BEFORE DELETE ON feedback_constraint
BEGIN SELECT RAISE(ABORT, 'immutable: feedback_constraint'); END;

CREATE TRIGGER event_no_update BEFORE UPDATE ON event
BEGIN SELECT RAISE(ABORT, 'immutable: event'); END;
CREATE TRIGGER event_no_delete BEFORE DELETE ON event
BEGIN SELECT RAISE(ABORT, 'immutable: event'); END;

CREATE TRIGGER consent_no_update BEFORE UPDATE ON consent
BEGIN SELECT RAISE(ABORT, 'immutable: consent'); END;
CREATE TRIGGER consent_no_delete BEFORE DELETE ON consent
BEGIN SELECT RAISE(ABORT, 'immutable: consent'); END;

CREATE TRIGGER consultation_no_update BEFORE UPDATE ON consultation
BEGIN SELECT RAISE(ABORT, 'immutable: consultation'); END;
CREATE TRIGGER consultation_no_delete BEFORE DELETE ON consultation
BEGIN SELECT RAISE(ABORT, 'immutable: consultation'); END;
