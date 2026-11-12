"""Pass 1: parse Python source with the stdlib `ast` module ONLY (analyzed code is never imported or executed)
and reduce each file to a compact, index-independent summary: definitions, imports, framework declarations and
per-function statement/call records. The AST itself is dropped right after, so memory stays O(summaries)
instead of O(all ASTs) — an AST costs ~3 KiB per source line, far too much for a 10k+ file repository.

Summaries depend only on file content, so they are cached by content digest and shared across sides/commits.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ....domain.model.graph import CallShape
from ...security.repo_files import read_repo_bytes
from .django_rules.urls import RouteEntry, extract_routes, extract_routers

SKIP_SEGMENTS = {"migrations", ".venv", "venv", "node_modules", "site-packages", "__pycache__", ".git", ".tox",
                 "tests", "test"}
MAX_FILES = 40_000
MAX_BYTES = 1_000_000
MANAGER_ATTRS = {"objects", "_default_manager", "_base_manager"}
ENQUEUE_ATTRS = {"delay", "apply_async", "s", "si"}  # task.delay() / .apply_async() / signatures task.s() / .si()


def text(node: ast.AST | None, limit: int = 200) -> str:
    if node is None:
        return ""
    try:
        s = ast.unparse(node)
    except Exception:  # pragma: no cover - unparse failure on exotic nodes
        s = type(node).__name__
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def node_hash(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()[:16]


def composite_hash(head: list[ast.AST], body: list[ast.stmt], known: dict[int, str]) -> str:
    """Content hash of a class/module that reuses the hashes already computed for its functions/classes, so every
    statement is dumped once (ast.dump dominated parse time on a 10k+ file repository). Formatting/comments still don't count."""
    h = hashlib.sha256()
    for n in head:
        h.update(ast.dump(n, include_attributes=False).encode())
    for stmt in body:
        h.update(b"\0")
        h.update(known[id(stmt)].encode() if id(stmt) in known else ast.dump(stmt, include_attributes=False).encode())
    return h.hexdigest()[:16]


def signature(fn: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool, decorators: list[str]) -> dict:
    a = fn.args
    positional = [x.arg for x in a.posonlyargs + a.args]
    if is_method and "staticmethod" not in decorators and positional:
        positional = positional[1:]
    n_defaults = len(a.defaults)
    required = max(len(positional) - n_defaults, 0)
    kwonly = [x.arg for x in a.kwonlyargs]
    required_kwonly = [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    return {
        "positional": positional, "required_positional": required, "kwonly": kwonly,
        "required_kwonly": required_kwonly, "vararg": a.vararg is not None, "kwarg": a.kwarg is not None,
    }


def decorator_name(d: ast.expr) -> str:
    target = d.func if isinstance(d, ast.Call) else d
    return text(target).rsplit(".", 1)[-1]


def attr_chain(expr: ast.expr) -> tuple[ast.expr, list[str]]:
    attrs: list[str] = []
    while isinstance(expr, ast.Attribute):
        attrs.append(expr.attr)
        expr = expr.value
    return expr, list(reversed(attrs))


# ---------------- records (index-independent) ----------------

@dataclass(frozen=True)
class CallRec:
    """One call site. `target` is a small descriptor tuple resolved in pass 2:
    ("name", n) | ("chain", root, attrs) | ("getattr", root|None, attrs, regex, explicit) | ("subscript", dict_name|None)
    | ("instance", root, model_attrs, method) | ("enqueue", root, attrs)
    | ("ctor_method", root, attrs, method): `x = <root.attrs>(...); x.method()` or `<root.attrs>(...).method()`"""

    line: int
    end: int
    col: int
    label: str
    shape: CallShape
    conds: tuple[str, ...]
    target: tuple


@dataclass(frozen=True)
class StmtRec:
    kind: str  # branch | return | raise
    line: int
    end: int
    label: str
    conds: tuple[str, ...]


@dataclass
class FuncInfo:
    name: str
    qualname: str
    lineno: int
    end_lineno: int
    decorators: list[str]
    signature: dict
    hash: str
    first_param: str | None
    events: list = field(default_factory=list)  # CallRec | StmtRec in source order
    class_name: str | None = None
    api_methods: list[str] | None = None  # DRF @api_view(["GET", ...]); [] = @api_view without a list (GET)
    uses_param_user: bool = False  # the body reads `<first param>.user` (a view acting on request.user)
    # Django Ninja: `@<var>.get("/path", response=Schema)` decorators (kept only when <var> is a Ninja instance, pass 2)
    # and the handler's parameters as Ninja reads them (location, required, annotation)
    ninja_ops: list[dict] = field(default_factory=list)  # {var, methods, path, line, response: (root, attrs) | None}
    ninja_params: list[dict] = field(default_factory=list)  # {name, where, required, ann: (root, attrs) | None, type}
    # imports inside the body (`from app.x import Y` in a task, to avoid an import cycle): (level, module, name, alias)
    local_imports: list[tuple[int, str | None, str, str]] = field(default_factory=list)


@dataclass
class ClassInfo:
    name: str
    lineno: int
    end_lineno: int
    bases: list[ast.expr]
    base_texts: list[str]
    decorators: list[str]
    hash: str
    methods: dict[str, FuncInfo] = field(default_factory=dict)
    fields: dict[str, int] = field(default_factory=dict)  # name -> line (Django-style Field() assignments)
    meta_model: ast.expr | None = None  # class Meta: model = X
    abstract: bool = False
    action_methods: set[str] = field(default_factory=set)  # DRF @action
    # DRF serializer contract (interface detection, spec §8): declared fields with their options, and class Meta lists
    field_opts: dict[str, dict] = field(default_factory=dict)  # name -> {type, read_only, write_only, required, default}
    meta_lists: dict[str, object] = field(default_factory=dict)  # Meta.fields / exclude / read_only_fields, extra_kwargs
    class_refs: dict[str, tuple] = field(default_factory=dict)  # attr = Name / a.b (e.g. serializer_class) -> (root, attrs)
    str_lists: dict[str, list[str]] = field(default_factory=dict)  # attr = ["a", "b"] (e.g. http_method_names)
    # pydantic / Ninja schema: annotated fields -> {required (no default), type}; None = not a schema class
    schema_fields: dict[str, dict] | None = None


@dataclass
class ModuleInfo:
    path: str
    dotted: str
    is_package: bool
    n_lines: int
    hash: str
    imports: dict[str, tuple[str, int]] = field(default_factory=dict)  # alias -> (dotted target, line)
    functions: dict[str, FuncInfo] = field(default_factory=dict)
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    assigns: dict[str, ast.expr] = field(default_factory=dict)  # module-level NAME = <dict literal> (dispatch tables)
    signals: set[str] = field(default_factory=set)  # module-level NAME = Signal(...)
    routes: list[RouteEntry] = field(default_factory=list)
    routers: dict = field(default_factory=dict)  # see django_rules.urls.extract_routers
    beat: list[tuple[str, str, int]] = field(default_factory=list)  # (schedule key, task dotted path, line)
    # file exports (spec §8 outer ring, outbound formats): column / header / field definitions → their keys; "@NAME" is a
    # reference to a string constant (resolved across modules by the graph builder)
    column_defs: dict[str, dict] = field(default_factory=dict)  # scope -> {"keys": [...], "dynamic": bool, "line": n}
    str_constants: dict[str, str] = field(default_factory=dict)  # module-level NAME = "text"
    file_content_type: bool = False  # a CSV / spreadsheet content type or "attachment; filename" literal
    # Django Ninja: module-level `api = NinjaAPI(...)` / `router = Router(...)` (var -> (constructor, line)) and
    # `api.add_router("/prefix", router)` mounts (parent var, prefix, ("chain", root, attrs) | ("str", dotted), line)
    ninja_instances: dict[str, tuple[str, int]] = field(default_factory=dict)
    ninja_mounts: list[tuple] = field(default_factory=list)


def dotted_for(path: str) -> tuple[str, bool]:
    p = path[:-3] if path.endswith(".py") else path
    is_pkg = p.endswith("/__init__") or p == "__init__"
    if is_pkg:
        p = p[: -len("/__init__")] if "/" in p else ""
    return p.replace("/", "."), is_pkg


def _resolve_relative(dotted: str, is_pkg: bool, level: int, module: str | None) -> str:
    parts = dotted.split(".") if dotted else []
    if not is_pkg:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)] if level - 1 <= len(parts) else []
    base = ".".join(parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


FIELD_FLAGS = ("read_only", "write_only", "required")


def _str_list(v: ast.expr) -> list[str] | None:
    if isinstance(v, (ast.List, ast.Tuple)) and all(isinstance(x, ast.Constant) and isinstance(x.value, str) for x in v.elts):
        return [x.value for x in v.elts]
    return None


def _field_opts(call: ast.Call) -> dict:
    """Options of `x = serializers.CharField(read_only=True)` / `items = ItemSerializer(many=True, required=False)`."""
    out: dict = {"type": text(call.func).rsplit(".", 1)[-1]}
    for k in call.keywords:
        if k.arg in FIELD_FLAGS and isinstance(k.value, ast.Constant) and isinstance(k.value.value, bool):
            out[k.arg] = k.value.value
        elif k.arg == "default":
            out["default"] = True
    return out


def _meta_lists(item: ast.ClassDef) -> dict[str, object]:
    out: dict[str, object] = {}
    for m in item.body:
        if not (isinstance(m, ast.Assign) and len(m.targets) == 1 and isinstance(m.targets[0], ast.Name)):
            continue
        name = m.targets[0].id
        if name in ("fields", "exclude", "read_only_fields"):
            lst = _str_list(m.value)
            if lst is not None:
                out[name] = lst
            elif isinstance(m.value, ast.Constant) and m.value.value == "__all__":
                out[name] = "__all__"
            else:
                out[name] = "dynamic"
        elif name == "extra_kwargs" and isinstance(m.value, ast.Dict):
            kw: dict[str, dict] = {}
            for k, v in zip(m.value.keys, m.value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Dict):
                    kw[k.value] = {kk.value: vv.value for kk, vv in zip(v.keys, v.values)
                                   if isinstance(kk, ast.Constant) and kk.value in FIELD_FLAGS
                                   and isinstance(vv, ast.Constant) and isinstance(vv.value, bool)}
            out["extra_kwargs"] = kw
    return out


def _field_call(value: ast.expr) -> bool:
    if not isinstance(value, ast.Call):
        return False
    name = text(value.func)
    return name.endswith("Field") or name.endswith("ForeignKey") or name.endswith("OneToOneField") or name.endswith("ManyToManyField")


def _shape(call: ast.Call) -> CallShape:
    return CallShape(
        positional=sum(1 for a in call.args if not isinstance(a, ast.Starred)),
        keywords=tuple(k.arg for k in call.keywords if k.arg is not None),
        star_args=any(isinstance(a, ast.Starred) for a in call.args),
        star_kwargs=any(k.arg is None for k in call.keywords),
    )


def _postorder_calls(node: ast.AST) -> list[ast.Call]:
    """Calls in (approximate) evaluation order: arguments before the call that consumes them."""
    out: list[ast.Call] = []

    def visit(n: ast.AST) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(n, ast.Lambda):  # deferred, but still this function's call: on_commit(lambda: task.delay(...))
            visit(n.body)
            return
        if isinstance(n, ast.Call):
            visit(n.func)
            for a in n.args:
                visit(a)
            for k in n.keywords:
                visit(k.value)
            out.append(n)
            return
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return out


def _name_pattern(expr: ast.expr) -> tuple[str, bool]:
    """Regex for a getattr name argument; bool = whether the pattern is explicit (has constant parts)."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return re.escape(expr.value), True
    if isinstance(expr, ast.JoinedStr):
        parts = [re.escape(v.value) if isinstance(v, ast.Constant) else ".+" for v in expr.values]
        return "".join(parts), any(isinstance(v, ast.Constant) for v in expr.values)
    return r"[A-Za-z]\w*", False


def _chain_desc(expr: ast.expr) -> tuple[str | None, tuple[str, ...]]:
    root, attrs = attr_chain(expr)
    return (root.id if isinstance(root, ast.Name) else None), tuple(attrs)


class _Recorder:
    """Walks one function body in source order, recording calls and notable statements with their enclosing
    (unconfirmed) branch conditions. Mirrors evaluation order closely enough for business-step ordering."""

    def __init__(self) -> None:
        self.events: list = []
        self.alias: dict[str, tuple] = {}  # local name -> dynamic descriptor (getattr / subscript)
        self.instances: dict[str, tuple] = {}  # local name -> (root, model attrs) of a Model.objects.<x>() result
        self.ctors: dict[str, tuple] = {}  # local name -> (root, attrs) of the callee of `x = Callee(...)` (maybe a class)
        self.imports: list[tuple[int, str | None, str, str]] = []  # see FuncInfo.local_imports

    def walk(self, stmts: list[ast.stmt], conds: tuple[str, ...]) -> None:
        for s in stmts:
            if isinstance(s, ast.If):
                cond = text(s.test)
                self.calls_in(s.test, conds)
                self.stmt("branch", s, f"if {cond}", conds)
                self.walk(s.body, conds + (cond,))
                if s.orelse:
                    self.walk(s.orelse, conds + (f"not ({cond})",))
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                self.calls_in(s.iter, conds)
                c = f"for {text(s.target)} in {text(s.iter)}"
                self.stmt("branch", s, c, conds)
                self.walk(s.body, conds + (f"{c} (loop body may run zero times)",))
                if s.orelse:
                    self.walk(s.orelse, conds + (f"{c} completed without break",))
            elif isinstance(s, ast.While):
                cond = text(s.test)
                self.calls_in(s.test, conds)
                self.stmt("branch", s, f"while {cond}", conds)
                self.walk(s.body, conds + (f"while {cond}",))
            elif isinstance(s, (ast.Try, ast.TryStar)):
                self.walk(s.body, conds)
                for h in s.handlers:
                    self.walk(h.body, conds + (f"except {text(h.type) or 'Exception'} raised",))
                if s.orelse:
                    self.walk(s.orelse, conds + ("try block raised no exception",))
                self.walk(s.finalbody, conds)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                for item in s.items:
                    self.calls_in(item.context_expr, conds)
                self.walk(s.body, conds)
            elif isinstance(s, ast.Match):
                self.calls_in(s.subject, conds)
                for case in s.cases:
                    self.walk(case.body, conds + (f"match {text(s.subject)} case {text(case.pattern)}",))
            elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            elif isinstance(s, ast.Return):
                if s.value is not None:
                    self.calls_in(s.value, conds)
                self.stmt("return", s, f"return {text(s.value, 80)}".strip(), conds)
            elif isinstance(s, ast.Raise):
                if s.exc is not None:
                    self.calls_in(s.exc, conds)
                self.stmt("raise", s, f"raise {text(s.exc, 80)}".strip(), conds)
            elif isinstance(s, ast.Assign):
                self.calls_in(s.value, conds)
                t = s.targets[0] if len(s.targets) == 1 else None
                if isinstance(t, ast.Name):
                    self.track_alias(t.id, s.value)
                elif isinstance(t, ast.Tuple) and t.elts and isinstance(t.elts[0], ast.Name):
                    self.track_alias(t.elts[0].id, s.value)  # obj, created = Model.objects.get_or_create(...)
            elif isinstance(s, ast.ImportFrom):
                self.imports += [(s.level, s.module, a.name, a.asname or a.name) for a in s.names if a.name != "*"]
            elif isinstance(s, ast.Import):
                self.imports += [(0, None, a.name, a.asname or a.name.split(".")[0]) for a in s.names]
            else:
                self.calls_in(s, conds)

    def track_alias(self, name: str, v: ast.expr) -> None:
        self.alias.pop(name, None)
        self.instances.pop(name, None)
        self.ctors.pop(name, None)
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "getattr" and len(v.args) >= 2:
            self.alias[name] = self.getattr_desc(v)
        elif isinstance(v, ast.Subscript):
            self.alias[name] = ("subscript", v.value.id if isinstance(v.value, ast.Name) else None)
        elif isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute):
            root, attrs = _chain_desc(v.func)
            # x = Model.objects.get(...) / .create(...) / .filter(...).first()
            if root and len(attrs) >= 2 and attrs[-2] in MANAGER_ATTRS:
                self.instances[name] = (root, attrs[:-2])
            elif root and attrs[-1][:1].isupper():  # x = module.Class(...)
                self.ctors[name] = (root, attrs)
        elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id[:1].isupper():  # x = Class(...)
            self.ctors[name] = (v.func.id, ())

    @staticmethod
    def getattr_desc(dyn: ast.Call) -> tuple:
        root, attrs = _chain_desc(dyn.args[0])
        pattern, explicit = _name_pattern(dyn.args[1])
        return ("getattr", root, attrs, pattern, explicit)

    def stmt(self, kind: str, s: ast.stmt, label: str, conds: tuple[str, ...]) -> None:
        end = s.end_lineno or s.lineno
        if kind == "branch":
            end = getattr(getattr(s, "test", None) or getattr(s, "iter", None) or s, "end_lineno", s.lineno) or s.lineno
        self.events.append(StmtRec(kind, s.lineno, end, label, conds))

    def calls_in(self, node: ast.AST, conds: tuple[str, ...]) -> None:
        for call in _postorder_calls(node):
            desc = self.describe(call)
            if desc is not None:
                self.events.append(CallRec(call.lineno, call.end_lineno or call.lineno, call.col_offset,
                                           text(call.func, 60), _shape(call), conds, desc))

    def describe(self, call: ast.Call) -> tuple | None:
        func = call.func
        if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr":
            return self.getattr_desc(func) if len(func.args) >= 2 else None
        if isinstance(func, ast.Name):
            if func.id in self.alias:
                return self.alias[func.id]
            return None if func.id == "getattr" else ("name", func.id)
        if isinstance(func, ast.Subscript):
            return ("subscript", func.value.id if isinstance(func.value, ast.Name) else None)
        if not isinstance(func, ast.Attribute):
            return None
        root, attrs = _chain_desc(func)
        if root is None:
            inner = func.value
            if isinstance(inner, ast.Call) and isinstance(inner.func, (ast.Name, ast.Attribute)):  # Class(...).method()
                r, a = _chain_desc(inner.func)
                if r is not None and (a[-1] if a else r)[:1].isupper():
                    return ("ctor_method", r, a, func.attr)
            return None
        if root in self.ctors and len(attrs) == 1 and attrs[0] not in ENQUEUE_ATTRS:  # x = Class(...); x.method()
            r, a = self.ctors[root]
            return ("ctor_method", r, a, attrs[0])
        if attrs[-1] in ENQUEUE_ATTRS and len(attrs) >= 1:
            return ("enqueue", root, attrs[:-1])
        if root in self.instances and len(attrs) == 1:
            r, model_attrs = self.instances[root]
            return ("instance", r, model_attrs, attrs[0])
        return ("chain", root, attrs)


NINJA_VERBS = {"get", "post", "put", "patch", "delete"}
NINJA_PARAM_FUNCS = {"Query", "Path", "Body", "Form", "File", "Header", "Cookie", "UploadedFile"}
SCHEMA_BASES = ("Schema", "BaseModel", "ModelSchema", "FilterSchema")
_PATH_PARAM = re.compile(r"{(?:[^}:]+:)?([^}]+)}")


def _type_ref(expr: ast.expr | None, depth: int = 0) -> tuple[str, tuple[str, ...]] | None:
    """The class a type expression names: `X`, `mod.X`, `List[X]`, `Optional[X]`, `X | None`, `{200: X}` → X."""
    if expr is None or depth > 4:
        return None
    if isinstance(expr, (ast.Name, ast.Attribute)):
        root, attrs = _chain_desc(expr)
        return (root, attrs) if root and root not in ("None", "dict", "Dict", "Any", "str", "int", "bool", "float") else None
    if isinstance(expr, ast.Subscript):
        inner = expr.slice.elts[0] if isinstance(expr.slice, ast.Tuple) and expr.slice.elts else expr.slice
        return _type_ref(inner, depth + 1)
    if isinstance(expr, ast.BinOp):  # X | None
        return _type_ref(expr.left, depth + 1) or _type_ref(expr.right, depth + 1)
    if isinstance(expr, ast.Dict):  # response={200: X, 404: Error}
        pick = next((v for k, v in zip(expr.keys, expr.values) if isinstance(k, ast.Constant) and k.value in (200, 201)),
                    expr.values[0] if expr.values else None)
        return _type_ref(pick, depth + 1)
    return None


def _ninja_ops(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict]:
    out = []
    for d in fn.decorator_list:
        if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)):
            continue
        verb, args = d.func.attr, list(d.args)
        if verb in NINJA_VERBS:
            methods = [verb.upper()]
        elif verb == "api_operation" and args and _str_list(args[0]) is not None:
            methods = [m.upper() for m in _str_list(args.pop(0)) or []]
        else:
            continue
        kw = {k.arg: k.value for k in d.keywords if k.arg}
        path = args[0] if args else kw.get("path")
        if not (isinstance(path, ast.Constant) and isinstance(path.value, str)):
            continue
        out.append({"var": d.func.value.id, "methods": methods, "path": path.value, "line": d.lineno,
                    "response": _type_ref(kw.get("response"))})
    return out


def _ninja_params(fn: ast.FunctionDef | ast.AsyncFunctionDef, paths: list[str]) -> list[dict]:
    """Parameters after `request`, as Django Ninja reads them: `x: int` (required query), `x: str = None` (optional),
    `x = Query(...)` / `Query[X]` / `Annotated[X, Query(...)]` (explicit location), `payload: SomeSchema` (body),
    and `{x}` in the operation path (path)."""
    a = fn.args
    args = (a.posonlyargs + a.args)[1:]
    defaults: list[ast.expr | None] = [None] * (len(a.posonlyargs + a.args) - len(a.defaults)) + list(a.defaults)
    pairs = list(zip(args, defaults[1:])) + list(zip(a.kwonlyargs, a.kw_defaults))
    in_path = {m.group(1) for p in paths for m in _PATH_PARAM.finditer(p)}
    out = []
    for arg, default in pairs:
        ann, where, required = arg.annotation, "auto", default is None
        marker = default if isinstance(default, ast.Call) else None
        if isinstance(ann, ast.Subscript) and text(ann.value).rsplit(".", 1)[-1] == "Annotated" and isinstance(ann.slice, ast.Tuple):
            elts = ann.slice.elts
            ann = elts[0] if elts else None
            marker = next((e for e in elts[1:] if isinstance(e, ast.Call)), marker)
        elif isinstance(ann, ast.Subscript) and text(ann.value).rsplit(".", 1)[-1] in NINJA_PARAM_FUNCS:
            where = text(ann.value).rsplit(".", 1)[-1].lower()  # Query[FilterSchema]
            ann = ann.slice
        if marker is not None and text(marker.func).rsplit(".", 1)[-1] in NINJA_PARAM_FUNCS:
            where = text(marker.func).rsplit(".", 1)[-1].lower()
            first = marker.args[0] if marker.args else next((k.value for k in marker.keywords if k.arg == "default"), None)
            required = first is None or (isinstance(first, ast.Constant) and first.value is Ellipsis)
        if arg.arg in in_path:
            where, required = "path", True
        out.append({"name": arg.arg, "where": where, "required": required, "ann": _type_ref(ann), "type": text(ann, 80) or None})
    return out


def _schema_fields(item: ast.ClassDef, base_texts: list[str]) -> dict[str, dict] | None:
    if not any(b.rsplit(".", 1)[-1] in SCHEMA_BASES for b in base_texts):
        return None
    out: dict[str, dict] = {}
    for st in item.body:
        if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and not st.target.id.startswith("_"):
            if text(st.annotation).startswith(("ClassVar", "typing.ClassVar")):
                continue
            v = st.value
            required = v is None or (isinstance(v, ast.Call) and text(v.func).rsplit(".", 1)[-1] == "Field"
                                     and (not v.args or (isinstance(v.args[0], ast.Constant) and v.args[0].value is Ellipsis))
                                     and not any(k.arg in ("default", "default_factory") for k in v.keywords))
            out[st.target.id] = {"required": required, "type": text(st.annotation, 80)}
    return out


def _func_info(fn: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str, is_method: bool, class_name: str | None) -> FuncInfo:
    decs = [decorator_name(d) for d in fn.decorator_list]
    rec = _Recorder()
    rec.walk(fn.body, ())
    first = fn.args.args[0].arg if fn.args.args else None
    api = None
    for d in fn.decorator_list:
        if decorator_name(d) == "api_view":
            api = [m.upper() for m in (_str_list(d.args[0]) or [])] if isinstance(d, ast.Call) and d.args else []
    uses_user = first is not None and any(
        isinstance(n, ast.Attribute) and n.attr == "user" and isinstance(n.value, ast.Name) and n.value.id == first
        for stmt in fn.body for n in ast.walk(stmt))
    ninja = _ninja_ops(fn) if not is_method else []
    return FuncInfo(fn.name, qualname, fn.lineno, fn.end_lineno or fn.lineno, decs, signature(fn, is_method, decs),
                    node_hash(fn), first, rec.events, class_name, api, uses_user, ninja,
                    _ninja_params(fn, [o["path"] for o in ninja]) if ninja else [], rec.imports)


def local_import_map(mod: "ModuleInfo", f: FuncInfo) -> dict[str, tuple[str, int]]:
    """A function's own imports in `ModuleInfo.imports` form (alias -> (dotted target, line)); relative ones resolved."""
    out: dict[str, tuple[str, int]] = {}
    for level, module, name, alias in f.local_imports:
        if module is None and level == 0:  # import a.b [as c]
            out[alias] = (name if alias != name.split(".")[0] else alias, f.lineno)
            continue
        base = _resolve_relative(mod.dotted, mod.is_package, level, module) if level else (module or "")
        out[alias] = (f"{base}.{name}" if base else name, f.lineno)
    return out


def _beat_entries(value: ast.expr) -> list[tuple[str, str, int]]:
    out = []
    if not isinstance(value, ast.Dict):
        return out
    for k, v in zip(value.keys, value.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Dict):
            for kk, vv in zip(v.keys, v.values):
                if isinstance(kk, ast.Constant) and kk.value == "task" and isinstance(vv, ast.Constant) and isinstance(vv.value, str):
                    out.append((k.value, vv.value, vv.lineno))
    return out


COLUMN_HINT = re.compile(r"column|header|field", re.I)  # cheap pre-check of a module's source
HINT_WORDS = {"column", "columns", "header", "headers", "field", "fields", "fieldnames"}
NOT_COLUMNS = {"update", "read", "readonly", "only", "exclude", "search", "ordering", "filter", "prefetch", "select", "raw"}
HINT_KEYWORDS = {"fieldnames", "columns", "headers", "header"}  # csv.DictWriter(fieldnames=[...]), DataFrame(columns=...)


def _hinted(name: str) -> bool:
    """`column_set`, `HEADERS`, `ReportCsvField`, `export_fieldnames` — not `update_fields` / `search_fields`."""
    words = [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)]
    return any(w in HINT_WORDS for w in words) and not (words and words[0] in NOT_COLUMNS)
FILE_CONTENT_TYPE = re.compile(r"text/csv|spreadsheetml|application/vnd\.ms-excel|attachment;\s*filename", re.I)
MAX_COLUMN_KEYS = 500


def _key(e: ast.expr) -> str | None:
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return e.value
    if isinstance(e, ast.Name):
        return "@" + e.id
    if isinstance(e, ast.Attribute):
        return text(e, 120)  # Columns.NAME: unresolved, but stable enough to notice a rename
    return None


def _column_keys(v: ast.expr | None) -> tuple[list[str], bool] | None:
    """Keys of a column definition literal: a dict's keys, a list / tuple of strings (or names), or a list of tuples
    whose first item is the key (`[("name", "Name"), ...]`). (keys, has non-literal keys) or None."""
    if isinstance(v, ast.Dict):
        keys = [_key(k) for k in v.keys if k is not None]  # None key = `**spread` (its keys are defined elsewhere)
        return [k for k in keys if k], any(k is None for k in keys)
    if isinstance(v, (ast.List, ast.Tuple, ast.Set)) and v.elts:
        first = [e.elts[0] if isinstance(e, (ast.Tuple, ast.List)) and e.elts else e for e in v.elts]
        keys = [_key(e) for e in first]
        if sum(1 for k in keys if k) * 2 >= len(keys):
            return [k for k in keys if k], any(k is None for k in keys)
    return None


def _scan_columns(mod: "ModuleInfo", tree: ast.Module, src: str) -> None:
    """Column definitions reachable from an export: literals assigned to / returned from scopes (functions, classes,
    variables, call keywords like `fieldnames=`) whose name says column / header / field. Subscript writes with a
    computed key in such a scope (`cols[f"{region}_tax"] = ...`) mark the scope as also naming columns at runtime."""
    mod.file_content_type = bool(FILE_CONTENT_TYPE.search(src))
    for st in tree.body:  # constants a column definition elsewhere may import
        if (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)):
            mod.str_constants[st.targets[0].id] = st.value.value[:200]
    if not COLUMN_HINT.search(src):
        return

    def put(scope: tuple[str, ...], got: tuple[list[str], bool] | None, line: int, dynamic: bool = False) -> None:
        if got is None and not dynamic:
            return
        d = mod.column_defs.setdefault(".".join(scope) or "<module>", {"keys": [], "dynamic": False, "line": line})
        keys, dyn = got or ([], False)
        d["keys"] = list(dict.fromkeys(d["keys"] + keys))[:MAX_COLUMN_KEYS]
        d["dynamic"] = d["dynamic"] or dyn or dynamic

    def keywords(node: ast.AST, scope: tuple[str, ...]) -> None:
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                for k in n.keywords:
                    if k.arg in HINT_KEYWORDS:
                        put(scope + (k.arg,), _column_keys(k.value), n.lineno)

    def walk(stmts: list[ast.stmt], scope: tuple[str, ...], hinted: bool) -> None:
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(st.body, scope + (st.name,), hinted or _hinted(st.name))
                continue
            if isinstance(st, (ast.Assign, ast.AnnAssign)):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in targets:
                    if isinstance(t, ast.Name) and (hinted or _hinted(t.id)):
                        put(scope + (t.id,), _column_keys(st.value), st.lineno)
                    elif isinstance(t, ast.Subscript) and hinted and isinstance(t.value, ast.Name):
                        k = _key(t.slice)
                        put(scope + (t.value.id,), ([k], False) if k and not k.startswith("@") else None, st.lineno,
                            dynamic=not (k and not k.startswith("@")))
            elif isinstance(st, ast.Return) and hinted:
                put(scope, _column_keys(st.value), st.lineno)
            for body in ("body", "orelse", "finalbody", "handlers"):
                inner = getattr(st, body, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], (ast.stmt, ast.ExceptHandler)):
                    walk([x for h in inner for x in (h.body if isinstance(h, ast.ExceptHandler) else [h])], scope, hinted)
            if isinstance(st, (ast.Expr, ast.Assign, ast.Return)) and st.value is not None:
                keywords(st.value, scope)

    walk(tree.body, (), False)


def parse_source(path: str, src: str) -> ModuleInfo:
    tree = ast.parse(src, filename=path)  # SyntaxError propagates to the caller (recorded as a coverage note)
    dotted, is_pkg = dotted_for(path)
    mod = ModuleInfo(path, dotted, is_pkg, max(src.count("\n") + (0 if src.endswith("\n") else 1), 1), "")
    known: dict[int, str] = {}  # id(def statement) -> its hash

    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                if a.asname:
                    mod.imports[a.asname] = (a.name, stmt.lineno)
                else:
                    top = a.name.split(".")[0]
                    mod.imports[top] = (top, stmt.lineno)
        elif isinstance(stmt, ast.ImportFrom):
            base = _resolve_relative(dotted, is_pkg, stmt.level, stmt.module) if stmt.level else (stmt.module or "")
            for a in stmt.names:
                if a.name == "*":
                    continue
                mod.imports[a.asname or a.name] = (f"{base}.{a.name}" if base else a.name, stmt.lineno)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod.functions[stmt.name] = _func_info(stmt, stmt.name, False, None)
            known[id(stmt)] = mod.functions[stmt.name].hash
        elif isinstance(stmt, ast.ClassDef):
            ci = ClassInfo(stmt.name, stmt.lineno, stmt.end_lineno or stmt.lineno, list(stmt.bases),
                           [text(b) for b in stmt.bases], [decorator_name(d) for d in stmt.decorator_list], "")
            ci.schema_fields = _schema_fields(stmt, ci.base_texts)
            members: dict[int, str] = {}
            for item in stmt.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fi = _func_info(item, f"{stmt.name}.{item.name}", True, stmt.name)
                    ci.methods[item.name] = fi
                    members[id(item)] = fi.hash
                    if "action" in fi.decorators:
                        ci.action_methods.add(item.name)
                elif isinstance(item, ast.Assign) and _field_call(item.value):
                    for t in item.targets:
                        if isinstance(t, ast.Name):
                            ci.fields[t.id] = item.lineno
                            ci.field_opts[t.id] = _field_opts(item.value)
                elif (isinstance(item, ast.Assign) and isinstance(item.value, ast.Call)
                      and text(item.value.func).rsplit(".", 1)[-1].endswith("Serializer")):  # nested serializer field
                    for t in item.targets:
                        if isinstance(t, ast.Name):
                            ci.field_opts[t.id] = _field_opts(item.value)
                elif (isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name)
                      and isinstance(item.value, (ast.Name, ast.Attribute))):
                    root, attrs = _chain_desc(item.value)
                    if root:
                        ci.class_refs[item.targets[0].id] = (root, attrs)
                elif (isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name)
                      and _str_list(item.value) is not None):
                    ci.str_lists[item.targets[0].id] = _str_list(item.value)
                elif isinstance(item, ast.AnnAssign) and item.value is not None and _field_call(item.value):
                    if isinstance(item.target, ast.Name):
                        ci.fields[item.target.id] = item.lineno
                elif isinstance(item, ast.ClassDef) and item.name == "Meta":
                    ci.meta_lists = _meta_lists(item)
                    for m in item.body:
                        if isinstance(m, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "model" for t in m.targets):
                            ci.meta_model = m.value
                        if (isinstance(m, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "abstract" for t in m.targets)
                                and isinstance(m.value, ast.Constant) and m.value.value is True):
                            ci.abstract = True
            ci.hash = composite_hash([ast.Name(stmt.name), *stmt.bases, *stmt.keywords, *stmt.decorator_list], stmt.body, members)
            known[id(stmt)] = ci.hash
            mod.classes[stmt.name] = ci
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            value = stmt.value
            for t in targets:
                if not isinstance(t, ast.Name) or value is None:
                    continue
                if isinstance(value, ast.Dict):
                    mod.assigns[t.id] = value
                    if t.id in ("CELERY_BEAT_SCHEDULE", "beat_schedule"):
                        mod.beat += _beat_entries(value)
                elif isinstance(value, ast.Call) and text(value.func).rsplit(".", 1)[-1] == "Signal":
                    mod.signals.add(t.id)
                elif isinstance(value, ast.Call) and text(value.func).rsplit(".", 1)[-1] in ("NinjaAPI", "Router"):
                    mod.ninja_instances[t.id] = (text(value.func), stmt.lineno)
        if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute)
                and stmt.value.func.attr == "add_router" and isinstance(stmt.value.func.value, ast.Name)
                and len(stmt.value.args) >= 2 and isinstance(stmt.value.args[0], ast.Constant)
                and isinstance(stmt.value.args[0].value, str)):
            target = stmt.value.args[1]
            desc = (("str", target.value) if isinstance(target, ast.Constant) and isinstance(target.value, str)
                    else ("chain", *_chain_desc(target)))
            mod.ninja_mounts.append((stmt.value.func.value.id, stmt.value.args[0].value, desc, stmt.lineno))
        # app.conf.beat_schedule = {...}
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "beat_schedule" for t in stmt.targets):
            mod.beat += _beat_entries(stmt.value)
    mod.hash = composite_hash([], tree.body, known)
    # `Router` is a common class name: keep it only when imported from django-ninja (NinjaAPI is unambiguous)
    mod.ninja_instances = {k: v for k, v in mod.ninja_instances.items() if v[0].rsplit(".", 1)[-1] == "NinjaAPI"
                           or v[0].startswith("ninja.") or mod.imports.get(v[0].split(".")[0], ("",))[0].startswith("ninja")}
    _scan_columns(mod, tree, src)
    mod.routes = extract_routes(tree)
    mod.routers = extract_routers(tree)
    return mod


def file_digest(raw: bytes) -> str:
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


def read_source(root: Path, path: str) -> tuple[bytes, str]:
    raw = read_repo_bytes(root, path, MAX_BYTES)
    if raw is None:  # missing, symlink, non-regular or outside the root
        raise FileNotFoundError(path)
    return raw, raw.decode("utf-8", "replace")


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def python_files(files: set[str]) -> tuple[list[str], list[str]]:
    """(analyzable .py paths, notes). Tests and migrations are not part of the system map."""
    out = sorted(
        f for f in files
        if f.endswith(".py") and not (set(f.split("/")[:-1]) & SKIP_SEGMENTS) and not is_test_file(f)
    )
    notes: list[str] = []
    if len(out) > MAX_FILES:
        notes.append(f"analyzed the first {MAX_FILES} of {len(out)} Python files (size cap)")
        out = out[:MAX_FILES]
    return out, notes
