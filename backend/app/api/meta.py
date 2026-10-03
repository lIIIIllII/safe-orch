"""현장 목록 GET /sites와 Pack 표시 정보 GET /sites/{id}/meta.

화면이 Pack 값(작업 유형·Rule 이름, 시간대, 근무 달력, 구역, 자원, 자원 속성 선언, 작업 유형 기본 요구 조건)을
하드코딩하지 않도록 내려준다.
Pack은 기동 시 한 번 읽고 바뀌지 않으므로 DB를 읽지 않는다.
"""

from typing import Any

from fastapi import APIRouter

from app.api.deps import ActorDep, PackDep, check_site

router = APIRouter()


@router.get("/sites")
def get_sites(pack: PackDep) -> dict[str, Any]:
    """현장 목록. 화면이 site_id·첫 Actor를 정하는 입구라 health처럼 X-Actor 없이 읽는다.

    Actor 목록은 데모 인증(X-Actor 선택)에 쓰는 공개 정보다. 현장은 1개다.
    """
    return {
        "sites": [
            {
                "site_id": pack.site_id,
                "pack": pack.name,
                "actors": [
                    {"actor_id": a.actor_id, "name": a.name, "roles": list(a.roles)}
                    for a in pack.actors
                ],
            }
        ]
    }


@router.get("/sites/{site_id}/meta")
def get_meta(site_id: str, pack: PackDep, actor: ActorDep) -> dict[str, Any]:
    check_site(site_id, pack)
    return {
        "pack": pack.name,
        "pack_hash": pack.pack_hash,
        "resource_types": dict(pack.resource_types),  # 코드 → 표시 이름
        "resource_attributes": [a.model_dump() for a in pack.resource_attributes.values()],
        "currency": pack.currency,
        "work_types": {
            wt_id: {
                "display_name": wt.display_name,
                "hazard_tags": list(wt.hazard_tags),
                "critical_fields": list(wt.critical_fields),
                "resource_requirements": [r.model_dump() for r in wt.resource_requirements],
            }
            for wt_id, wt in pack.work_types.items()
        },
        "rules": [
            {"rule_id": r.rule_id, "type": r.type, "display_name": r.display_name}
            for r in pack.rules
        ],
        "timezone": pack.timezone,
        "horizon_start_utc": pack.horizon_start_utc,
        "horizon_minutes": pack.horizon_minutes,
        "work_intervals": [list(iv) for iv in pack.work_intervals],
        "zones": [z.zone_id for z in pack.zones],
        "zone_relations": [r.model_dump() for r in pack.zone_relations],
        "resources": [r.model_dump(mode="json") for r in pack.resources],
    }
