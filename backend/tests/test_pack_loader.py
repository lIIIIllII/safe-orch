"""Pack 로더 (설계서 §5.3, 부록 A.4). T29·T30과 pack_hash, 기동 시 확인."""

import pytest
import yaml
from fastapi.testclient import TestClient

from app.domain.canonical import canonical_json, sha256_hex
from app.main import app
from app.packs.loader import PACK_FILES, PackError, load_pack
from app.store import db
from app.store.repos.site import PackMismatchError


def _edit(pack_dir, fname, mutate):
    f = pack_dir / fname
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    mutate(data)
    f.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _reasons(pack_dir) -> str:
    with pytest.raises(PackError) as exc:
        load_pack(pack_dir)
    return "\n".join(exc.value.reasons)


def _task(data, task_id):
    return next(t for t in data["tasks"] if t["task_id"] == task_id)


# ── pack_hash ──────────────────────────────────────────────────


def test_pack_hash_is_canonical_json_of_all_five_files(pack_copy):
    raw = {f: yaml.safe_load((pack_copy / f).read_text(encoding="utf-8")) for f in PACK_FILES}
    assert load_pack(pack_copy).pack_hash == sha256_hex(canonical_json(raw))


def test_pack_hash_same_when_only_newlines_comments_whitespace_change(pack, pack_copy):
    for fname in PACK_FILES:
        f = pack_copy / fname
        text = f.read_text(encoding="utf-8")
        f.write_bytes(
            ("# 추가 주석\n\n" + text.replace("\n", "   # 줄끝 주석\r\n", 3) + "\n\n# 끝\n").encode(
                "utf-8"
            )
        )
    assert load_pack(pack_copy).pack_hash == pack.pack_hash


def test_pack_hash_changes_when_value_changes(pack, pack_copy):
    _edit(pack_copy, "rules.yaml", lambda d: d["rules"][1].update(min_gap=20))
    assert load_pack(pack_copy).pack_hash != pack.pack_hash


def test_unquoted_datetime_rejected(pack_copy):
    f = pack_copy / "site.yaml"
    f.write_text(
        f.read_text(encoding="utf-8").replace('"2026-10-12T00:00:00Z"', "2026-10-12T00:00:00Z"),
        encoding="utf-8",
    )
    assert "not canonical JSON" in _reasons(pack_copy)


# ── T29 ────────────────────────────────────────────────────────


def test_t29_hazard_tags_input_ignored_and_derived(pack_copy):
    def inject(d):
        _task(d, "B")["hazard_tags"] = ["FLAMMABLE"]
        _task(d, "C")["hazard_tags"] = []

    _edit(pack_copy, "plan_r0.yaml", inject)
    _edit(pack_copy, "scenario.yaml", lambda d: d["new_task"].update(hazard_tags=["NONE"]))
    loaded = load_pack(pack_copy)
    tags = {t.task_id: t.hazard_tags for t in loaded.tasks}
    assert tags["B"] == ("WORK_BELOW",)
    assert tags["C"] == ("LIFTING",)


def test_t29_lifting_tag_empty_rejected(pack_copy):
    _edit(pack_copy, "pack.yaml", lambda d: d["work_types"]["LIFTING"].update(hazard_tags=[]))
    reasons = _reasons(pack_copy)
    assert "LIFTING: empty hazard_tags" in reasons
    assert "undefined hazard tag 'LIFTING'" in reasons  # SEP-LIFT-BELOW가 참조


def test_t29_lifting_tag_missing_rejected(pack_copy):
    _edit(
        pack_copy, "pack.yaml", lambda d: d["work_types"]["LIFTING"].update(hazard_tags=["HEAVY"])
    )
    assert "undefined hazard tag 'LIFTING'" in _reasons(pack_copy)


def test_t29_unsupported_evaluator_rejected(pack_copy):
    _edit(pack_copy, "rules.yaml", lambda d: d["rules"][2].update(type="CUMULATIVE"))
    assert "unsupported evaluator 'CUMULATIVE'" in _reasons(pack_copy)


def test_t29_undefined_rule_relation_rejected(pack_copy):
    _edit(pack_copy, "rules.yaml", lambda d: d["rules"][0].update(relations=["SAME", "ABOVE"]))
    assert "undefined relation 'ABOVE'" in _reasons(pack_copy)


def test_t29_capacity_2_rejected(pack_copy):
    _edit(pack_copy, "site.yaml", lambda d: d["resources"][0].update(capacity=2))
    assert "capacity must be 1, got 2" in _reasons(pack_copy)


def test_t29_pack_file_change_refuses_startup(seeded, pack_copy, monkeypatch):
    """seed 후 Pack 파일이 바뀌면 pack_hash 불일치로 기동을 거절한다(C01은 Validator 구현 후)."""
    _edit(pack_copy, "rules.yaml", lambda d: d["rules"][1].update(min_gap=20))
    monkeypatch.setattr("app.main.pack_dir", lambda _name: pack_copy)
    with pytest.raises(PackMismatchError), TestClient(app):
        pass
    with db.read() as conn:  # 자동 reset 없음
        assert conn.execute("SELECT pack_hash FROM site").fetchone()[0] == seeded.pack_hash


def test_startup_accepts_matching_seeded_pack(seeded):
    with TestClient(app) as client:
        assert client.app.state.pack.pack_hash == seeded.pack_hash


# ── 기타 로더 검증 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fname", "mutate", "expected"),
    [
        ("site.yaml", lambda d: d["actors"][0].update(unit_id="UX"), "undefined unit 'UX'"),
        (
            "site.yaml",
            lambda d: d["resources"][0].update(allowed_unit_ids=["UZ"]),
            "undefined unit 'UZ'",
        ),
        (
            "plan_r0.yaml",
            lambda d: _task(d, "C").update(owner_actor_id="nobody"),
            "undefined actor 'nobody'",
        ),
        (
            "plan_r0.yaml",
            lambda d: _task(d, "C").update(requested_resource_id="X-CR-09"),
            "undefined resource 'X-CR-09'",
        ),
        (
            "plan_r0.yaml",
            lambda d: _task(d, "E").update(work_type="WELDING"),
            "undefined work_type 'WELDING'",
        ),
        ("plan_r0.yaml", lambda d: _task(d, "E").update(zone_id="Z9"), "undefined zone 'Z9'"),
        ("plan_r0.yaml", lambda d: d["assignments"][0].update(task_id="X9"), "undefined task 'X9'"),
        (
            "plan_r0.yaml",
            lambda d: d["assignments"][1].update(end=100),
            "end - start = 40 != duration 30",
        ),
        ("site.yaml", lambda d: d["zones"].append("B"), "duplicate id 'B'"),
        ("site.yaml", lambda d: d["units"].append(dict(d["units"][0])), "duplicate id 'UA'"),
        ("scenario.yaml", lambda d: d["new_task"].update(task_id="C"), "duplicate id 'C'"),
        (
            "site.yaml",
            lambda d: d["zone_relations"].append({"relation": "BELOW", "zones": ["B", "C"]}),
            "BELOW needs direction",
        ),
        (
            "site.yaml",
            lambda d: d["zone_relations"].append({"relation": "ADJACENT", "zones": ["D", "Q"]}),
            "undefined zone",
        ),
        (
            "site.yaml",
            lambda d: d["zone_relations"].append({"relation": "SAME", "zones": ["B", "C"]}),
            "undefined relation 'SAME'",
        ),
    ],
)
def test_loader_rejects_invalid_references(pack_copy, fname, mutate, expected):
    _edit(pack_copy, fname, mutate)
    assert expected in _reasons(pack_copy)


def test_loader_collects_all_reasons(pack_copy):
    _edit(pack_copy, "site.yaml", lambda d: d["resources"][0].update(capacity=2))
    _edit(pack_copy, "rules.yaml", lambda d: d["rules"][2].update(type="CUMULATIVE"))
    reasons = _reasons(pack_copy)
    assert "capacity must be 1" in reasons
    assert "unsupported evaluator" in reasons


def test_loaded_pack_is_frozen(pack):
    with pytest.raises(Exception, match="frozen"):
        pack.site_id = "OTHER"


# ── rel (T30) ──────────────────────────────────────────────────


def test_rel_same_adjacent_none(pack):
    assert pack.rel("B", "B") == "SAME"
    assert pack.rel("D", "D2") == "ADJACENT"
    assert pack.rel("D2", "D") == "ADJACENT"
    assert pack.rel("B", "C") is None
    assert pack.rel("C", "D") is None


def test_t30_below_forward_only(pack_copy):
    _edit(
        pack_copy,
        "site.yaml",
        lambda d: d["zone_relations"].append({"relation": "BELOW", "upper": "C", "lower": "B"}),
    )
    loaded = load_pack(pack_copy)
    assert loaded.rel("C", "B") == "BELOW"  # 정방향: SEP-LIFT-BELOW 적용 대상
    assert loaded.rel("B", "C") is None  # 역방향: 미적용


def test_scenario_requested_start_must_equal_earliest_start(pack_copy):
    """신규 작업의 기준 배정 = (earliest_start, 요청 자원) (부록 A.10)."""
    _edit(pack_copy, "scenario.yaml", lambda d: d["new_task"]["requested"].update(start=10, end=40))
    assert "requested.start 10 != earliest_start 0" in _reasons(pack_copy)


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        ([[0, 90], [60, 180]], "must start after previous end 90"),  # 겹침
        ([[0, 90], [90, 180]], "must start after previous end 90"),  # 맞닿음
        ([[120, 180], [0, 60]], "must start after previous end 180"),  # 정렬 안 됨
        ([[0, 3361]], "0 <= lo < hi <= 3360"),  # Horizon 밖
        ([[-10, 60]], "0 <= lo < hi <= 3360"),  # 0 미만
        ([[60, 60]], "0 <= lo < hi <= 3360"),  # lo = hi
        ([[90, 60]], "0 <= lo < hi <= 3360"),  # lo > hi
    ],
)
def test_available_intervals_rejected(pack_copy, intervals, expected):
    """Rule Engine(한 구간 포함)과 CP-SAT(합집합)의 판정이 같도록 구간 모양을 강제한다."""
    _edit(pack_copy, "site.yaml", lambda d: d["resources"][0].update(available_intervals=intervals))
    assert expected in _reasons(pack_copy)


def test_available_intervals_disjoint_sorted_accepted(pack_copy):
    _edit(
        pack_copy,
        "site.yaml",
        lambda d: d["resources"][0].update(available_intervals=[[0, 60], [61, 180]]),
    )
    assert load_pack(pack_copy).resources[0].available_intervals == ((0, 60), (61, 180))


# ── 근무 달력·표시 정보·시연값 (부록 A.20) ─────────────────────


def test_calendar_timezone_and_display_names_loaded(pack):
    assert pack.horizon_minutes == 3360
    assert pack.work_intervals == ((0, 480), (1440, 1920), (2880, 3360))
    assert pack.timezone == "Asia/Seoul"
    assert pack.work_types["PAINTING"].display_name == "도장"
    assert {r.rule_id: r.display_name for r in pack.rules}["CAP-RESOURCE"] == "자원 중복 배정 금지"


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        ([], "work_intervals missing or empty"),
        ([[0, 480], [480, 960]], "must start after previous end 480"),  # 맞닿음
        ([[0, 480], [400, 900]], "must start after previous end 480"),  # 겹침
        ([[1440, 1920], [0, 480]], "must start after previous end 1920"),  # 정렬 안 됨
        ([[0, 480], [2880, 3361]], "0 <= lo < hi <= 3360"),  # Horizon 밖
        ([[0, "480"]], "must be [lo, hi] integer minutes"),
    ],
)
def test_work_intervals_rejected(pack_copy, intervals, expected):
    _edit(pack_copy, "site.yaml", lambda d: d.update(work_intervals=intervals))
    assert expected in _reasons(pack_copy)


def test_work_intervals_missing_rejected(pack_copy):
    _edit(pack_copy, "site.yaml", lambda d: d.pop("work_intervals"))
    assert "work_intervals missing or empty" in _reasons(pack_copy)


def test_plan_r0_outside_calendar_rejected(pack_copy):
    """고정 작업이 달력을 어기면 모든 Solver 호출이 INFEASIBLE이므로 로더에서 막는다."""
    _edit(
        pack_copy,
        "site.yaml",
        lambda d: d.update(work_intervals=[[30, 480], [1440, 1920], [2880, 3360]]),
    )
    reasons = _reasons(pack_copy)
    assert "[0, 60) outside work_intervals (CALENDAR)" in reasons  # B 09:00–10:00
    assert "new_task: requested outside work_intervals" in reasons  # A 09:00–09:30


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d.pop("timezone"), "timezone missing"),
        (lambda d: d.update(timezone="Mars/Olympus"), "unknown timezone 'Mars/Olympus'"),
    ],
)
def test_timezone_rejected(pack_copy, mutate, expected):
    _edit(pack_copy, "site.yaml", mutate)
    assert expected in _reasons(pack_copy)


def test_display_names_required(pack_copy):
    _edit(pack_copy, "pack.yaml", lambda d: d["work_types"]["PAINTING"].pop("display_name"))
    _edit(pack_copy, "rules.yaml", lambda d: d["rules"][2].pop("display_name"))
    reasons = _reasons(pack_copy)
    assert "pack.yaml.work_types.PAINTING.display_name: Field required" in reasons
    assert "rules.yaml.rules[2].display_name: Field required" in reasons


def test_demo_requests_and_events_loaded(pack):
    assert [(d.task_id, d.requester) for d in pack.demo_requests] == [
        ("N1", "planner_a"),
        ("N2", "planner_a"),
        ("N3", "planner_b"),
        ("N4", "planner_a"),
        ("N5", "planner_b"),
    ]
    assert pack.new_task.label and "label" not in pack.new_task.model_dump()
    assert pack.demo_events[0].text == "도장 준비 15분 늦어져 10시부터"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda r: r.update(requester="foreman_a2"), "is not UNIT_PLANNER"),
        (lambda r: r.update(requester="nobody"), "undefined actor 'nobody'"),
        (lambda r: r.update(zone_id="Z9"), "undefined zone 'Z9'"),
        (lambda r: r.update(requested_resource_id="A-CR-01"), "resource type mismatch"),
        (lambda r: r.update(requested_resource_id=None), "needs required_resource_type"),
        (lambda r: r.update(earliest_start=1000, latest_start=1000), "outside work_intervals"),
        (lambda r: r.update(task_id="K"), "duplicate id 'K'"),
    ],
)
def test_demo_requests_rejected(pack_copy, mutate, expected):
    _edit(pack_copy, "scenario.yaml", lambda d: mutate(d["demo_requests"][0]))  # N1
    assert expected in _reasons(pack_copy)


def test_demo_event_target_must_exist(pack_copy):
    _edit(pack_copy, "scenario.yaml", lambda d: d["demo_events"][0].update(target_task_id="X9"))
    assert "undefined task 'X9'" in _reasons(pack_copy)


# ── 판정 기준 정리 (부록 A.22) ─────────────────────────────────


@pytest.mark.parametrize(
    ("mutate", "count"),
    [
        (lambda d: d["rules"].pop(2), 0),
        (lambda d: d["rules"].append({**d["rules"][2], "rule_id": "CAP-2"}), 2),
    ],
    ids=["none", "two"],
)
def test_exactly_one_capacity_rule_required(pack_copy, mutate, count):
    _edit(pack_copy, "rules.yaml", mutate)
    assert f"exactly one CAPACITY rule required, got {count}" in _reasons(pack_copy)


def _set_preds(data, task_id, preds):
    _task(data, task_id)["predecessors"] = preds


@pytest.mark.parametrize(
    ("preds", "expected"),
    [
        ([{"task_id": "X9"}], "undefined predecessor task 'X9' (plan_r0 only)"),
        ([{"task_id": "A"}], "undefined predecessor task 'A' (plan_r0 only)"),  # new_task
        ([{"task_id": "C"}], "predecessor refers to itself 'C'"),
        ([{"task_id": "B", "min_lag": -5}], "predecessor 'B' min_lag -5 < 0"),
    ],
    ids=["unknown", "new_task", "self", "negative_lag"],
)
def test_plan_r0_predecessors_rejected(pack_copy, preds, expected):
    _edit(pack_copy, "plan_r0.yaml", lambda d: _set_preds(d, "C", preds))
    assert f"plan_r0.yaml.tasks C: {expected}" in _reasons(pack_copy)


def test_plan_r0_predecessor_cycle_rejected(pack_copy):
    def mutate(d):
        _set_preds(d, "B", [{"task_id": "C"}])
        _set_preds(d, "C", [{"task_id": "D"}])
        _set_preds(d, "D", [{"task_id": "B"}])

    _edit(pack_copy, "plan_r0.yaml", mutate)
    assert "predecessor cycle B -> C -> D -> B" in _reasons(pack_copy)


def test_plan_r0_predecessor_without_cycle_loads(pack_copy):
    _edit(pack_copy, "plan_r0.yaml", lambda d: _set_preds(d, "C", [{"task_id": "B"}]))
    pack = load_pack(pack_copy)
    assert next(t for t in pack.tasks if t.task_id == "C").predecessors[0].task_id == "B"


@pytest.mark.parametrize(
    ("preds", "expected"),
    [
        ([{"task_id": "N1"}], "undefined predecessor task 'N1' (plan_r0 only)"),  # demo_request
        ([{"task_id": "A"}], "predecessor refers to itself 'A'"),
        ([{"task_id": "B", "min_lag": -1}], "predecessor 'B' min_lag -1 < 0"),
    ],
    ids=["demo_request", "self", "negative_lag"],
)
def test_new_task_predecessors_rejected(pack_copy, preds, expected):
    _edit(pack_copy, "scenario.yaml", lambda d: d["new_task"].update(predecessors=preds))
    assert f"scenario.yaml.new_task: {expected}" in _reasons(pack_copy)


def test_demo_request_predecessors_key_not_accepted(pack_copy):
    _edit(
        pack_copy,
        "scenario.yaml",
        lambda d: d["demo_requests"][0].update(predecessors=[{"task_id": "B"}]),
    )
    assert "scenario.yaml.demo_requests[0].predecessors: Extra inputs" in _reasons(pack_copy)
