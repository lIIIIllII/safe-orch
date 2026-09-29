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
        ("plan_r0.yaml", lambda d: d["assignments"][0].update(task_id="Q"), "undefined task 'Q'"),
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
