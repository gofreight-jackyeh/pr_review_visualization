"""Static extraction of Django urlpatterns (path()/re_path()/url(), include() prefixes) and DRF routers."""

from __future__ import annotations

import ast
from dataclasses import dataclass

ROUTE_FUNCS = {"path", "re_path", "url"}


@dataclass
class RouteEntry:
    pattern: str
    line: int
    end_line: int
    view_expr: ast.expr | None  # expression naming the view
    include_target: ast.expr | None
    name: str | None
    is_regex: bool


def _const_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _pattern_lists(tree: ast.Module) -> list[ast.expr]:
    out: list[ast.expr] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "urlpatterns" for t in stmt.targets):
            out.append(stmt.value)
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "urlpatterns":
            out.append(stmt.value)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "urlpatterns" and stmt.value:
            out.append(stmt.value)
    return out


def _elements(value: ast.expr) -> list[ast.expr]:
    if isinstance(value, (ast.List, ast.Tuple)):
        return list(value.elts)
    if isinstance(value, ast.BinOp):  # [...] + router.urls
        return _elements(value.left) + _elements(value.right)
    return [value]


def extract_routes(tree: ast.Module) -> list[RouteEntry]:
    entries: list[RouteEntry] = []
    for value in _pattern_lists(tree):
        for el in _elements(value):
            if not isinstance(el, ast.Call):
                continue
            fname = el.func.id if isinstance(el.func, ast.Name) else (el.func.attr if isinstance(el.func, ast.Attribute) else "")
            if fname not in ROUTE_FUNCS or not el.args:
                continue
            pattern = _const_str(el.args[0])
            if pattern is None:
                continue
            target = el.args[1] if len(el.args) > 1 else None
            name = next((_const_str(k.value) for k in el.keywords if k.arg == "name"), None)
            include = None
            if isinstance(target, ast.Call) and getattr(target.func, "id", getattr(target.func, "attr", None)) == "include":
                include = target.args[0] if target.args else None
                target = None
            entries.append(RouteEntry(pattern, el.lineno, el.end_lineno or el.lineno, target, include, name, fname != "path"))
    return entries


def extract_routers(tree: ast.Module) -> dict:
    """DRF routers: {"vars": {router_var}, "mounts": {router_var: url prefix}, "registrations": [(var, prefix, expr, line, end)]}.

    `router = DefaultRouter()`; `router.register(r"payments", PaymentViewSet)`; mounted by
    `path("api/", include(router.urls))` or `urlpatterns = router.urls` / `+= router.urls` (prefix "")."""
    router_vars: set[str] = set()
    regs: list[tuple] = []
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Call)):
            f = stmt.value.func
            fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            if fname.endswith("Router"):
                router_vars.add(stmt.targets[0].id)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            c = stmt.value
            if (isinstance(c.func, ast.Attribute) and c.func.attr == "register" and isinstance(c.func.value, ast.Name)
                    and len(c.args) >= 2 and _const_str(c.args[0]) is not None):
                regs.append((c.func.value.id, _const_str(c.args[0]), c.args[1], c.lineno, c.end_lineno or c.lineno))
    if not router_vars:
        return {}
    mounts: dict[str, str] = {}
    for value in _pattern_lists(tree):
        for el in _elements(value):
            if isinstance(el, ast.Attribute) and el.attr == "urls" and isinstance(el.value, ast.Name):
                mounts.setdefault(el.value.id, "")
            if isinstance(el, ast.Call) and el.args and len(el.args) > 1 and isinstance(el.args[1], ast.Call):
                inc = el.args[1]
                if (getattr(inc.func, "id", getattr(inc.func, "attr", None)) == "include" and inc.args
                        and isinstance(inc.args[0], ast.Attribute) and inc.args[0].attr == "urls"
                        and isinstance(inc.args[0].value, ast.Name)):
                    mounts.setdefault(inc.args[0].value.id, _const_str(el.args[0]) or "")
    return {"vars": router_vars, "mounts": mounts, "registrations": [r for r in regs if r[0] in router_vars]}


def display_pattern(prefix: str, entry: RouteEntry) -> str:
    p = entry.pattern
    if entry.is_regex:
        p = p.lstrip("^").rstrip("$")
    return "/" + (prefix + p).lstrip("/")
