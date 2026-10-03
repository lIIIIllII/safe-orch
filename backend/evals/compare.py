"""결과 파일 둘을 비교한다. 같은 모델·설정·Pack·시나리오·하네스에서만 비교한다 (EV-04).

    cd backend && uv run python -m evals.compare a.jsonl b.jsonl [--force]

조건이 다르면 거절한다(--force면 차이를 표시하고 진행). prompt·실행 계약 버전 차이는 나란히 보여 준다.
"""

import argparse
import json
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evals.run import distribution

# 같아야 비교할 수 있는 설정
MUST_MATCH = ("harness_version", "model_settings", "model_ids", "pack_hash", "agent_flags")
SHOWN = ("eval_stage", "prompt_versions", "exec_contract_versions")


def read(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x]
    return lines[0], lines[1:]


def differences(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """비교를 막는 설정 차이. 두 파일에 함께 있는 시나리오의 hash·SITE_NOW도 본다."""
    out = [f"{k}: {a.get(k)} ≠ {b.get(k)}" for k in MUST_MATCH if a.get(k) != b.get(k)]
    for key in ("scenario_hashes", "site_now"):
        for sid in sorted(set(a[key]) & set(b[key])):
            if a[key][sid] != b[key][sid]:
                out.append(f"{key}[{sid}] 다름")
    if not set(a["scenario_hashes"]) & set(b["scenario_hashes"]):
        out.append("함께 있는 시나리오가 없음")
    return out


def summary(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """시나리오별 등급 분포와 지표 중앙값(유효 회차)."""
    out: dict[str, dict[str, Any]] = {}
    for sid, counts in distribution(records).items():
        valid = [r for r in records if r["scenario"] == sid and r["valid"]]
        steps = [r["metrics"]["steps_total"] for r in valid]
        asks = [sum(r["metrics"]["questions_by_actor"].values()) for r in valid]
        out[sid] = {
            **counts,
            "steps_median": statistics.median(steps) if steps else None,
            "questions_median": statistics.median(asks) if asks else None,
        }
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.compare")
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    parser.add_argument("--force", action="store_true", help="조건이 달라도 표시하고 비교한다")
    args = parser.parse_args(argv)
    (ha, ra), (hb, rb) = read(args.a), read(args.b)
    diffs = differences(ha, hb)
    if diffs:
        print("조건이 다릅니다:", *diffs, sep="\n  ", file=sys.stderr)
        if not args.force:
            return 1
        print("--force: 조건이 다른 채로 비교합니다. 지침 효과로 읽지 마세요.")
    for key in SHOWN:
        print(f"{key}:\n  a {ha.get(key)}\n  b {hb.get(key)}")
    sa, sb = summary(ra), summary(rb)
    for sid in sorted(set(sa) | set(sb)):
        for name, s in (("a", sa.get(sid)), ("b", sb.get(sid))):
            if s is None:
                print(f"{sid} {name}: 없음")
                continue
            print(
                f"{sid} {name}: 통과 {s['PASS']} / 미달 {s['SHORT']} / 실패 {s['FAIL']}"
                f" (무효 {s['INVALID']}) step {s['steps_median']} 질문 {s['questions_median']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
