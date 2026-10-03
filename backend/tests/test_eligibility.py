"""자원 적격성 판정 한 함수 (CV-17·18·20). 사유별: 유형·권한·가용 구간 없음·구역·요구 조건."""

import pytest

from app.domain.eligibility import (
    ALL_ZONES,
    Exclusion,
    ResourceNeed,
    exclusion_reasons,
    meets,
    requirement_error,
)
from app.domain.models import Requirement, Resource, ResourceAttribute

ATTRIBUTES = {
    "max_load": ResourceAttribute(name="max_load", type="NUMBER", unit="t", display_name="하중"),
    "usage": ResourceAttribute(name="usage", type="LIST", display_name="용도"),
}


def _resource(**changes):
    data = {
        "resource_id": "R1",
        "resource_type": "CRANE",
        "owner_unit_id": "UA",
        "allowed_unit_ids": ("UA",),
        "allowed_zone_ids": ("B", "C"),
        "available_intervals": ((0, 100),),
        "attributes": {"max_load": 25, "usage": ("일반",)},
        **changes,
    }
    return Resource(**data)


def _need(*requirements, rtype="CRANE", zone="B"):
    return ResourceNeed(required_resource_type=rtype, zone_id=zone, requirements=requirements)


def _req(attribute, op, value):
    return Requirement(attribute=attribute, op=op, value=value)


def _codes(need, resource, unit="UA"):
    return [(e.reason, e.attribute) for e in exclusion_reasons(need, resource, unit)]


def test_eligible_resource_has_no_reason():
    need = _need(_req("max_load", "GTE", 20), _req("usage", "CONTAINS", "일반"))
    assert exclusion_reasons(need, _resource(), "UA") == []


@pytest.mark.parametrize(
    ("need", "resource", "unit", "expected"),
    [
        (_need(rtype="GANTRY"), _resource(), "UA", [("TYPE_MISMATCH", None)]),
        (_need(rtype=None), _resource(), "UA", [("TYPE_MISMATCH", None)]),
        (_need(), _resource(), "UB", [("NOT_ALLOWED", None)]),
        (_need(), _resource(available_intervals=()), "UA", [("NO_AVAILABILITY", None)]),
        (_need(zone="D"), _resource(), "UA", [("ZONE_NOT_ALLOWED", None)]),
        (
            _need(_req("max_load", "GTE", 30)),
            _resource(),
            "UA",
            [("REQUIREMENT_NOT_MET", "max_load")],
        ),
        (
            _need(_req("max_load", "LTE", 20)),
            _resource(),
            "UA",
            [("REQUIREMENT_NOT_MET", "max_load")],
        ),
        (
            _need(_req("usage", "CONTAINS", "블록")),
            _resource(),
            "UA",
            [("REQUIREMENT_NOT_MET", "usage")],
        ),
    ],
)
def test_each_reason(need, resource, unit, expected):
    assert _codes(need, resource, unit) == expected


def test_all_reasons_are_reported_together():
    need = _need(
        _req("max_load", "GTE", 20),  # 맞춘다
        _req("max_load", "GTE", 100),  # 같은 속성의 조건은 사유 하나로
        _req("max_load", "GTE", 200),
        _req("usage", "CONTAINS", "블록"),
        rtype="GANTRY",
        zone="F",
    )
    assert _codes(need, _resource(available_intervals=()), "UB") == [
        ("TYPE_MISMATCH", None),
        ("NOT_ALLOWED", None),
        ("NO_AVAILABILITY", None),
        ("ZONE_NOT_ALLOWED", None),
        ("REQUIREMENT_NOT_MET", "max_load"),
        ("REQUIREMENT_NOT_MET", "usage"),
    ]


def test_all_zones_and_unknown_zone():
    star = _resource(allowed_zone_ids=(ALL_ZONES,))
    assert exclusion_reasons(_need(zone="H"), star, "UA") == []
    # 구역이 정해지지 않은 조회(zone_id None)는 구역을 보지 않는다
    assert exclusion_reasons(_need(zone=None), _resource(), "UA") == []


def test_boundary_values_meet():
    assert meets(_req("max_load", "GTE", 25), _resource())
    assert meets(_req("max_load", "LTE", 25), _resource())
    assert meets(_req("max_load", "GTE", 24.5), _resource())


def test_missing_or_wrong_typed_attribute_fails_closed():
    bare = _resource(attributes={})
    assert not meets(_req("max_load", "GTE", 1), bare)
    assert not meets(_req("max_load", "LTE", 1000), bare)
    assert not meets(_req("usage", "CONTAINS", "일반"), bare)
    assert not meets(_req("usage", "GTE", 1), _resource())  # 목록 속성에 수치 비교
    assert not meets(_req("max_load", "CONTAINS", "25"), _resource())  # 수치 속성에 목록 비교


def test_exclusion_dump_omits_empty_attribute():
    assert Exclusion(reason="NOT_ALLOWED").model_dump(exclude_none=True) == {
        "reason": "NOT_ALLOWED"
    }


@pytest.mark.parametrize(
    ("req", "expected"),
    [
        (_req("max_load", "GTE", 20), None),
        (_req("max_load", "LTE", 20.5), None),
        (_req("usage", "CONTAINS", "블록"), None),
        (_req("reach", "GTE", 1), "undeclared attribute 'reach'"),
        (_req("max_load", "CONTAINS", "x"), "is NUMBER"),
        (_req("max_load", "GTE", "20"), "is NUMBER"),
        (_req("usage", "GTE", 1), "is LIST"),
        (_req("usage", "CONTAINS", 1), "is LIST"),
    ],
)
def test_requirement_error(req, expected):
    error = requirement_error(req, ATTRIBUTES)
    assert error is None if expected is None else expected in error
