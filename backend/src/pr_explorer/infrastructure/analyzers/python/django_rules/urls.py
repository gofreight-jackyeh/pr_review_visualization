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
    include_target: ast.expr | None  # module string / module / pattern-list variable mounted under `pattern`
    name: str | None
    is_regex: bool
    list_name: str = "urlpatterns"  # the module-level pattern list this entry belongs to
    display: str | None = None  # pattern with inline include() prefixes already joined and cleaned


def _const_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _route_call(el: ast.expr) -> bool:
    if not isinstance(el, ast.Call) or not el.args:
        return False
    fname = el.func.id if isinstance(el.func, ast.Name) else (el.func.attr if isinstance(el.func, ast.Attribute) else "")
    return fname in ROUTE_FUNCS


def _pattern_lists(tree: ast.Module) -> list[tuple[str, ast.expr]]:
    """(name, value) of `urlpatterns` and of every other module-level list holding path()/re_path()/url() calls
    (`internal_api_urlpatterns = [...]`, mounted elsewhere with `include(internal_api_urlpatterns)`)."""
    out: list[tuple[str, ast.expr]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            names, value = [t.id for t in stmt.targets if isinstance(t, ast.Name)], stmt.value
        elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)) and isinstance(stmt.target, ast.Name) and stmt.value:
            names, value = [stmt.target.id], stmt.value
        else:
            continue
        for n in names:
            if n == "urlpatterns" or any(_route_call(el) for el in _elements(value)):
                out.append((n, value))
    return out


def _elements(value: ast.expr) -> list[ast.expr]:
    if isinstance(value, (ast.List, ast.Tuple)):
        return list(value.elts)
    if isinstance(value, ast.BinOp):  # [...] + router.urls
        return _elements(value.left) + _elements(value.right)
    return [value]


def _clean(pattern: str, is_regex: bool) -> str:
    return pattern.lstrip("^").rstrip("$") if is_regex else pattern


def include_targets(expr: ast.expr | None) -> list[ast.expr]:
    """What an include() argument mounts: `"app.urls"`, `module`, `pattern_list`, `(pattern_list, "ns")`, `a + b`."""
    if expr is None:
        return []
    if isinstance(expr, ast.Tuple) and expr.elts:  # (patterns, app_namespace)
        return include_targets(expr.elts[0])
    if isinstance(expr, ast.BinOp):
        return include_targets(expr.left) + include_targets(expr.right)
    return [expr]


def _entries(els: list[ast.expr], list_name: str, outer: str, outer_disp: str, depth: int) -> list[RouteEntry]:
    entries: list[RouteEntry] = []
    for el in els:
        if isinstance(el, (ast.Name, ast.Attribute)) and not (isinstance(el, ast.Attribute) and el.attr == "urls"):
            # `urlpatterns = [...] + other_list`: the other list is mounted here with no prefix
            entries.append(RouteEntry(outer, el.lineno, el.end_lineno or el.lineno, None, el, None, False, list_name,
                                      outer_disp or None))
            continue
        if not _route_call(el):
            continue
        assert isinstance(el, ast.Call)
        pattern = _const_str(el.args[0])
        if pattern is None:
            continue
        is_regex = getattr(el.func, "id", getattr(el.func, "attr", None)) != "path"
        target = el.args[1] if len(el.args) > 1 else None
        name = next((_const_str(k.value) for k in el.keywords if k.arg == "name"), None)
        full, disp = outer + pattern, outer_disp + _clean(pattern, is_regex)
        nested = outer != ""
        if isinstance(target, ast.Call) and getattr(target.func, "id", getattr(target.func, "attr", None)) == "include":
            for inc in include_targets(target.args[0] if target.args else None):
                if isinstance(inc, (ast.List, ast.Tuple)) and depth < 6:  # include([path(...), ...]): inline patterns
                    entries += _entries(list(inc.elts), list_name, full, disp, depth + 1)
                else:
                    entries.append(RouteEntry(full, el.lineno, el.end_lineno or el.lineno, None, inc, name, is_regex,
                                              list_name, disp if nested else None))
            continue
        entries.append(RouteEntry(full, el.lineno, el.end_lineno or el.lineno, target, None, name, is_regex, list_name,
                                  disp if nested else None))
    return entries


def extract_routes(tree: ast.Module) -> list[RouteEntry]:
    entries: list[RouteEntry] = []
    for list_name, value in _pattern_lists(tree):
        entries += _entries(_elements(value), list_name, "", "", 0)
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
    for _name, value in _pattern_lists(tree):
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
    p = entry.display if entry.display is not None else _clean(entry.pattern, entry.is_regex)
    return "/" + (prefix + p).lstrip("/")
