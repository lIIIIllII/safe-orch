"""소스 import 경계 검사."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(BACKEND).with_suffix("").parts)


def _imports(path: Path, tree: ast.AST) -> set[str]:
    """파일이 import하는 모듈 이름 (상대 import는 절대 이름으로 해석)."""
    package = _module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[: len(package) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def _hits(modules: set[str], forbidden: str) -> set[str]:
    return {m for m in modules if m == forbidden or m.startswith(forbidden + ".")}


def _py_files(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def _violations(files: list[Path], forbidden: list[str]) -> list[str]:
    out = []
    for f in files:
        mods = _imports(f, ast.parse(f.read_text(encoding="utf-8")))
        for target in forbidden:
            for m in sorted(_hits(mods, target)):
                out.append(f"{f.relative_to(BACKEND)}: {m}")
    return out


def test_validator_does_not_import_solver():
    assert _violations(_py_files(APP / "validator"), ["app.solver"]) == []


def test_agent_graph_and_specs_do_not_import_store_or_commands():
    files = _py_files(APP / "agents" / "graph.py", APP / "agents" / "specs")
    assert _violations(files, ["app.store", "app.commands"]) == []


def test_domain_imports_no_app_modules():
    """의존 방향 app.domain ← app.packs ← app.store.repos."""
    files = _py_files(APP / "domain")
    bad = []
    for f in files:
        mods = _imports(f, ast.parse(f.read_text(encoding="utf-8")))
        bad += [
            f"{f.relative_to(BACKEND)}: {m}"
            for m in sorted(_hits(mods, "app"))
            if not (m == "app.domain" or m.startswith("app.domain."))
        ]
    assert files and bad == []


def test_packs_do_not_import_store():
    files = _py_files(APP / "packs")
    assert files and _violations(files, ["app.store"]) == []


def test_domain_and_packs_scanner_detects_violation():
    tree = ast.parse("from ..store import db\nfrom app.packs.loader import load_pack\n")
    mods = _imports(APP / "domain" / "models.py", tree)
    assert _hits(mods, "app.store") and _hits(mods, "app.packs")


def test_solver_does_not_import_rules_or_validator():
    """제약 생성(solver)과 검사(rules·validator) 코드를 나눈다."""
    files = _py_files(APP / "solver")
    assert files and _violations(files, ["app.rules", "app.validator"]) == []


def test_agent_specs_do_not_import_solver():
    """Solver는 Tool Gateway로만 부른다. AgentSpec은 순수 데이터다."""
    files = _py_files(APP / "agents" / "specs")
    assert files and _violations(files, ["app.solver"]) == []


def test_agent_graph_does_not_import_agent_store_modules():
    """graph.py는 port로만 DB에 닿는다. store를 쓰는 agents 모듈도 import하지 않는다."""
    files = [APP / "agents" / "graph.py"]
    forbidden = [
        "app.agents.runtime",
        "app.agents.tool_gateway",
        "app.agents.observe",
        "app.agents.registry",
        "app.agents.observers",
        "app.agents.executors",
    ]
    assert _violations(files, forbidden) == []


# ── agent_type 등록 구조 ───────────────────────────


AGENTS = APP / "agents"


def test_agent_specs_and_prompts_are_pure():
    """specs·prompts는 store·commands·solver를 import하지 않는다 (graph가 받는 순수 데이터)."""
    files = _py_files(AGENTS / "specs", AGENTS / "prompts")
    assert files and _violations(files, ["app.store", "app.commands", "app.solver"]) == []


def _importers(target: str, allowed: set[Path]) -> list[str]:
    bad = []
    for f in _py_files(APP):
        if f in allowed:
            continue
        mods = _imports(f, ast.parse(f.read_text(encoding="utf-8")))
        bad += [f"{f.relative_to(BACKEND)}: {m}" for m in sorted(_hits(mods, target))]
    return bad


def test_only_registry_imports_observers_and_executors():
    allowed = {AGENTS / "registry.py"}
    assert _importers("app.agents.observers", allowed) == []
    assert _importers("app.agents.executors", allowed) == []


def test_only_runtime_imports_registry():
    assert _importers("app.agents.registry", {AGENTS / "runtime.py"}) == []


def test_tool_gateway_does_not_import_registry_or_agent_modules():
    """ToolGateway는 runtime이 넘긴 binding을 쓴다."""
    forbidden = ["app.agents.registry", "app.agents.observers", "app.agents.executors"]
    assert _violations([AGENTS / "tool_gateway.py"], forbidden) == []


def _attribute_sites(attr: str) -> set[tuple[str, str]]:
    """app 코드에서 `<x>.<attr>`를 읽는 (파일, 감싼 함수) 목록."""
    sites = set()
    for f in _py_files(APP):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == attr:
                    sites.add((f.relative_to(APP).as_posix(), fn.name))
    return sites


def test_executor_is_called_only_inside_tool_gateway_execute():
    """실행기는 ToolGateway가 만들고(__init__) execute 안에서만 부른다. 도구 실행 경로는 하나다."""
    assert _attribute_sites("executor") == {
        ("agents/tool_gateway.py", "__init__"),
        ("agents/tool_gateway.py", "execute"),
    }


def test_gateway_and_executors_have_no_authority_functions():
    """승인·확정·Hold 해제·Proposal 확인·미응답 수용·고정·고정 해제 함수가 코드상 없다."""
    words = ("approve", "commit", "release", "confirm", "waive", "pin")
    bad = []
    for f in _py_files(AGENTS / "tool_gateway.py", AGENTS / "executors"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                w in node.name.lower() for w in words
            ):
                bad.append(f"{f.relative_to(BACKEND)}: {node.name}")
    assert bad == []


def test_commands_do_not_import_agents():
    files = _py_files(APP / "commands")
    assert files and _violations(files, ["app.agents"]) == []


def test_coordinator_imports_only_agents_runtime():
    """Coordinator는 Run 호출에 agents.runtime만 쓴다."""
    bad = []
    for f in _py_files(APP / "coordinator"):
        mods = _imports(f, ast.parse(f.read_text(encoding="utf-8")))
        bad += [
            f"{f.relative_to(BACKEND)}: {m}"
            for m in sorted(_hits(mods, "app.agents"))
            if m not in ("app.agents", "app.agents.runtime")
            and not m.startswith("app.agents.runtime.")
        ]
    assert bad == []


def test_coordinator_does_not_import_solver():
    """Solver는 Replanning Run이 부른다. Coordinator는 결정론 단계만 한다."""
    files = _py_files(APP / "coordinator")
    assert files and _violations(files, ["app.solver"]) == []


def test_rules_do_not_import_solver():
    files = _py_files(APP / "rules")
    assert files and _violations(files, ["app.solver"]) == []


FORBIDDEN_NAMES = ("create_agent", "checkpointer", "interrupt")
LANGGRAPH_TYPES = "langgraph.types"
FORBIDDEN_TYPES = ("interrupt", "Command")


def _forbidden_usages(rel: Path, tree: ast.AST) -> list[str]:
    """금지 이름(create_agent·checkpointer·interrupt)과 langgraph.types의 interrupt·Command 사용 위치."""
    bad = []
    type_modules: set[str] = set()  # langgraph.types 모듈을 가리키는 표현식
    bound: set[str] = set()  # interrupt·Command에 바인딩된 로컬 이름 (별칭 포함)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == LANGGRAPH_TYPES:
                    type_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                if node.module == "langgraph" and alias.name == "types":
                    type_modules.add(alias.asname or alias.name)
                elif node.module == LANGGRAPH_TYPES and alias.name in (*FORBIDDEN_TYPES, "*"):
                    bound.add(alias.asname or alias.name)
                    bad.append(f"{rel}:{node.lineno}: import {LANGGRAPH_TYPES}.{alias.name}")
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
            if name in bound:
                bad.append(f"{rel}:{node.lineno}: {name}")
        elif isinstance(node, ast.Attribute):
            name = node.attr
            if name in FORBIDDEN_TYPES and ast.unparse(node.value) in type_modules:
                bad.append(f"{rel}:{node.lineno}: {ast.unparse(node)}")
        elif isinstance(node, ast.alias):
            name = node.asname or node.name.rsplit(".", 1)[-1]
        elif isinstance(node, ast.keyword):
            name = node.arg
        if name in FORBIDDEN_NAMES:
            bad.append(f"{rel}:{getattr(node, 'lineno', '?')}: {name}")
    return bad


def test_no_prebuilt_create_agent_checkpointer_or_interrupt():
    bad = []
    for f in _py_files(APP):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rel = f.relative_to(BACKEND)
        mods = _imports(f, tree)
        bad += [f"{rel}: import {m}" for m in sorted(_hits(mods, "langgraph.prebuilt"))]
        bad += [f"{rel}: import {m}" for m in sorted(_hits(mods, "langgraph.checkpoint"))]
        bad += _forbidden_usages(rel, tree)
    assert bad == []


def test_import_scanner_detects_relative_import():
    """검사기가 빈 골격에서 공허하게 통과하지 않는지 확인."""
    fake = APP / "validator" / "check.py"
    tree = ast.parse("from ..solver import model\nimport app.solver.cpsat\n")
    mods = _imports(fake, tree)
    assert _hits(mods, "app.solver") == {"app.solver", "app.solver.model", "app.solver.cpsat"}


def test_usage_scanner_detects_interrupt_and_command():
    """langgraph.types의 interrupt·Command 검사가 별칭·모듈 경유까지 실제로 잡아내는지 확인."""
    src = (
        "from langgraph.types import interrupt as ask\n"  # 1
        "from langgraph.types import Command as C\n"  # 2
        "import langgraph.types as lt\n"  # 3
        "import langgraph.types\n"  # 4
        "from langgraph import types as T\n"  # 5
        "answer = ask('q')\n"  # 6
        "graph.invoke(C(**kwargs))\n"  # 7
        "lt.interrupt('q')\n"  # 8
        "graph.invoke(lt.Command(goto='next'))\n"  # 9
        "graph.invoke(langgraph.types.Command(resume='ok'))\n"  # 10
        "graph.invoke(T.Command(update={}))\n"  # 11
        "from langgraph.types import *\n"  # 12
        "from langgraph.types import StreamMode\n"  # 13: 허용
        "other.Command()\n"  # 14: langgraph와 무관 → 허용
    )
    bad = _forbidden_usages(Path("fake.py"), ast.parse(src))
    assert "fake.py:1: import langgraph.types.interrupt" in bad
    assert "fake.py:2: import langgraph.types.Command" in bad
    assert "fake.py:6: ask" in bad
    assert "fake.py:7: C" in bad
    assert "fake.py:8: lt.interrupt" in bad
    assert "fake.py:9: lt.Command" in bad
    assert "fake.py:10: langgraph.types.Command" in bad
    assert "fake.py:11: T.Command" in bad
    assert "fake.py:12: import langgraph.types.*" in bad
    assert not any(b.startswith(("fake.py:13:", "fake.py:14:")) for b in bad)
