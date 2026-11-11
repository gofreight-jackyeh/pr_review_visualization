"""docs/architecture.md §1: domain imports nothing external; application imports only domain + stdlib."""

import ast
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "pr_explorer"


def imports(layer: str):
    for f in (PKG / layer).rglob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    yield f, a.name, 0
            elif isinstance(node, ast.ImportFrom):
                yield f, node.module or "", node.level


def test_domain_is_pure():
    for f, mod, level in imports("domain"):
        if level:  # relative import must stay inside domain
            assert level <= len(f.relative_to(PKG / "domain").parts), (f, mod)
            continue
        assert mod.split(".")[0] in sys.stdlib_module_names, (f, mod)
        assert mod.split(".")[0] not in {"subprocess", "sqlite3", "socket", "urllib", "http"}, (f, mod)


def test_application_depends_only_inward():
    for f, mod, level in imports("application"):
        if level == 0:
            assert mod.split(".")[0] in sys.stdlib_module_names, (f, mod)
        else:
            assert "infrastructure" not in mod and "interface" not in mod, (f, mod)
