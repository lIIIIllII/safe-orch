"""소스 import 경계 검사 (설계서 §11.2, §14)."""

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


def test_no_prebuilt_create_agent_or_checkpointer():
    bad = []
    for f in _py_files(APP):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rel = f.relative_to(BACKEND)
        mods = _imports(f, tree)
        bad += [f"{rel}: import {m}" for m in sorted(_hits(mods, "langgraph.prebuilt"))]
        bad += [f"{rel}: import {m}" for m in sorted(_hits(mods, "langgraph.checkpoint"))]
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.asname or node.name.rsplit(".", 1)[-1]
            elif isinstance(node, ast.keyword):
                name = node.arg
            if name in ("create_agent", "checkpointer"):
                bad.append(f"{rel}:{getattr(node, 'lineno', '?')}: {name}")
    assert bad == []


def test_import_scanner_detects_relative_import():
    """검사기가 빈 골격에서 공허하게 통과하지 않는지 확인."""
    fake = APP / "validator" / "check.py"
    tree = ast.parse("from ..solver import model\nimport app.solver.cpsat\n")
    mods = _imports(fake, tree)
    assert _hits(mods, "app.solver") == {"app.solver", "app.solver.model", "app.solver.cpsat"}
