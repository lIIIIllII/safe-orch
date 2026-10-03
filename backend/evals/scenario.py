"""시나리오 정의 읽기. Pack 밖(evals/scenarios/<pack>/)에 두고, Pack ID는 LoadedPack과 대조한다."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.domain.canonical import sha256_hex
from app.packs.loader import LoadedPack

SCENARIO_DIR = Path(__file__).with_name("scenarios")
START_KINDS = ("INTAKE", "FORM_NEW_TASK", "CONSULTING")
OUTCOME_KINDS = ("INTAKE_COMMITTED", "FACT_UPDATE", "NO_SOLUTION_ESCALATED")


class ScenarioError(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    path: Path
    file_hash: str
    hidden: bool
    data: dict[str, Any]


def scenario_path(pack_name: str, name: str, hidden: bool) -> Path:
    base = SCENARIO_DIR / pack_name
    return (base / "hidden" / f"{name}.yaml") if hidden else (base / f"{name}.yaml")


def scenario_names(pack_name: str, hidden: bool) -> list[str]:
    base = SCENARIO_DIR / pack_name / ("hidden" if hidden else "")
    return sorted(p.stem for p in base.glob("*.yaml"))


def _referenced(data: dict[str, Any]) -> dict[str, set[str]]:
    """시나리오가 가리키는 Pack ID. 종류별로 모은다."""
    start = data.get("start", {})
    truth = data.get("truth", {})
    humans = data.get("humans", {})
    actors = {start.get("requester"), *humans.get("free_text", {}), *humans.get("values_check", {})}
    tasks: set[Any] = set()
    resources: set[Any] = set()
    for rule in humans.get("rules", []):
        when = rule.get("when", {})
        actors.add(when.get("to"))
        tasks.add(when.get("task"))
        resources.update(when.get("values", []))
    for event in data.get("events", []):
        actors.add(event.get("actor"))
    expect = start.get("expect", {})
    actors.add(expect.get("to"))
    tasks.add(expect.get("task"))
    task = truth.get("task", {})
    resources.add(task.get("requested_resource_id"))
    tasks.add(truth.get("fact", {}).get("task"))
    tasks.add(data.get("outcome", {}).get("task"))
    quoted = data.get("quoted_instruction", {})
    actors.add(quoted.get("after", {}).get("to"))
    forbidden = quoted.get("forbidden", {})
    tasks.add(forbidden.get("change_request", {}).get("task"))
    tasks.add(forbidden.get("resource_use", {}).get("task"))
    resources.add(forbidden.get("resource_use", {}).get("resource_id"))
    return {
        "actor": {a for a in actors if a},
        "task": {t for t in tasks if t},
        "resource": {r for r in resources if r},
        "zone": {task["zone_id"]} if task.get("zone_id") else set(),
        "work_type": {task["work_type"]} if task.get("work_type") else set(),
        "resource_type": {task["required_resource_type"]}
        if task.get("required_resource_type")
        else set(),
    }


def validate(data: dict[str, Any], pack: LoadedPack) -> list[str]:
    reasons = []
    for key in ("id", "site_now", "start", "outcome"):
        if key not in data:
            reasons.append(f"missing {key}")
    start = data.get("start", {})
    if start.get("kind") not in START_KINDS:
        reasons.append(f"start.kind must be one of {START_KINDS}")
    if data.get("outcome", {}).get("kind") not in OUTCOME_KINDS:
        reasons.append(f"outcome.kind must be one of {OUTCOME_KINDS}")
    known = {
        "actor": {a.actor_id for a in pack.actors},
        "task": {t.task_id for t in pack.tasks} | {pack.new_task.task_id},
        "resource": {r.resource_id for r in pack.resources},
        "zone": {z.zone_id for z in pack.zones},
        "work_type": set(pack.work_types),
        "resource_type": set(pack.resource_types),
    }
    new_task = start.get("task_id")
    if start.get("kind") == "INTAKE":
        if not new_task or not start.get("text"):
            reasons.append("INTAKE start needs task_id and text")
        elif new_task in known["task"]:
            reasons.append(f"start.task_id {new_task} already exists in pack")
    for kind, ids in _referenced(data).items():
        for missing in sorted(ids - known[kind] - ({new_task} if kind == "task" else set())):
            reasons.append(f"unknown {kind} id in pack {pack.name}: {missing}")
    return reasons


def load(path: Path, pack: LoadedPack, hidden: bool = False) -> Scenario:
    """YAML을 읽고 Pack ID를 대조한다. 없는 ID가 있으면 ScenarioError(실행을 거절한다)."""
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ScenarioError([f"{path}: not a mapping"])
    reasons = validate(data, pack)
    if reasons:
        raise ScenarioError([f"{Path(path).name}: {r}" for r in reasons])
    return Scenario(str(data["id"]), Path(path), sha256_hex(raw), hidden, data)
