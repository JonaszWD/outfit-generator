"""The engine is pure Python: no database, web framework or app-layer imports."""

import ast
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[2] / "app" / "engine"
FORBIDDEN = (
    "sqlmodel",
    "sqlalchemy",
    "fastapi",
    "starlette",
    "psycopg",
    "pydantic",
    "app.models",
    "app.db",
    "app.routes",
    "app.services",
    "app.deps",
    "app.schemas",
    "app.main",
)


def imported_modules(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", sorted(ENGINE.glob("*.py")), ids=lambda p: p.name)
def test_engine_module_has_no_forbidden_imports(path: Path) -> None:
    for name in imported_modules(path):
        for banned in FORBIDDEN:
            clean = name != banned and not name.startswith(banned + ".")
            assert clean, f"{path.name} imports {name}"


def test_importing_the_engine_does_not_load_the_web_stack() -> None:
    import subprocess
    import sys

    code = (
        "import sys, app.engine.generator, app.engine.rules, app.engine.config;"
        "banned=('sqlmodel','sqlalchemy','fastapi','starlette','pydantic');"
        "bad=[m for m in banned if m in sys.modules];"
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, f"engine pulled in: {result.stdout}"
