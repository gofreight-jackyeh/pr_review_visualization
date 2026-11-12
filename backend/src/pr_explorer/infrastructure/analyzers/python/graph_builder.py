"""Pass 2: Python/Django side graph from pass-1 summaries: modules, classes, functions, calls, routes, DRF routers,
Celery tasks/schedules, management commands, ORM usage, emits and external calls.

Resolution is static and conservative. Direct names, `self.method`, imported names and `module.attr` resolve to
confirmed edges. Dynamic dispatch (getattr, subscript dispatch, attributes missing on a project module) becomes
confidence=unknown edges. Never imports or executes analyzed code.
"""

from __future__ import annotations

import ast
import re
import time
from pathlib import Path

from ....domain.model import ids
from ....domain.model.graph import CodeRef, Edge, EntryFact, Evidence, FlowStep, Node, Producer, SideGraph, UnresolvedRef
from .ast_analyzer import (
    CallRec, ClassInfo, FuncInfo, ModuleInfo, StmtRec, attr_chain, file_digest, parse_source, python_files, read_source, text,
)
from .django_rules.classify import (
    EXTERNAL_CALLS, EXTERNAL_MODULES, HTTP_METHODS, INSTANCE_ACTIONS, MANAGER_ATTRS, TASK_DECORATORS, VIEWSET_ACTIONS,
    classify_class, command_name, is_drf, is_function_view, is_viewset, orm_action,
)
from .django_rules.urls import display_pattern

AST = Producer("python_ast", "2")
DJANGO = Producer("django_rules", "2")
MAX_CANDIDATES = 10
LANG = "py"
FILE_WRITER_LIBS = {"csv", "openpyxl", "xlsxwriter", "xlwt", "tablib", "pyexcelerate"}  # top-level imports that write files


class _Index:
    """dotted name -> definition, across all project modules of one side."""

    def __init__(self, modules: dict[str, ModuleInfo]):
        self.by_dotted: dict[str, tuple] = {}
        self.modules = modules
        self.roots: set[str] = set()
        for mod in modules.values():
            self.by_dotted[mod.dotted] = ("module", mod)
            if mod.dotted:
                self.roots.add(mod.dotted.split(".")[0])
            for f in mod.functions.values():
                self.by_dotted[f"{mod.dotted}.{f.name}"] = ("function", mod, f)
            for c in mod.classes.values():
                self.by_dotted[f"{mod.dotted}.{c.name}"] = ("class", mod, c)
                for m in c.methods.values():
                    self.by_dotted[f"{mod.dotted}.{c.name}.{m.name}"] = ("method", mod, m)

    def lookup(self, dotted: str, depth: int = 0) -> tuple | None:
        hit = self.by_dotted.get(dotted)
        if hit or depth > 5 or "." not in dotted:
            return hit
        head, attr = dotted.rsplit(".", 1)
        parent = self.by_dotted.get(head) or self.lookup(head, depth + 1)
        if parent and parent[0] == "module" and attr in parent[1].imports:  # re-export
            return self.lookup(parent[1].imports[attr][0], depth + 1)
        return None

    def is_project(self, dotted: str) -> bool:
        return dotted.split(".")[0] in self.roots


def _node_id(mod: ModuleInfo, qual: str | None = None) -> str:
    return ids.symbol_id(LANG, mod.path, qual) if qual else ids.module_id(LANG, mod.path)


def _def_id(hit: tuple) -> str | None:
    kind, mod = hit[0], hit[1]
    if kind == "module":
        return _node_id(mod)
    if kind in ("function", "method"):
        return _node_id(mod, hit[2].qualname)
    if kind == "class":
        return _node_id(mod, hit[2].name)
    return None


class PythonAnalyzer:
    name = "python"
    language = "python"

    def __init__(self) -> None:
        # (path, content digest) -> pass-1 summary. Holds the most recently analyzed side only, so the next side or
        # commit re-parses just the files whose content differs (summaries are read-only after parsing).
        self._cache: dict[tuple[str, str], ModuleInfo] = {}
        self.last_stats: dict = {}

    def available(self) -> bool:
        return True

    def claims(self, path: str) -> bool:
        return path.endswith(".py") or path.endswith(".pyi")

    def analyze(self, root: Path, files: set[str], side: str) -> SideGraph:
        t0 = time.perf_counter()
        g = SideGraph(side=side, files=files)
        g.ordered_languages.add("python")
        paths, notes = python_files(files)
        modules: dict[str, ModuleInfo] = {}
        cache: dict[tuple[str, str], ModuleInfo] = {}
        reused = 0
        for p in paths:
            try:
                raw, src = read_source(root, p)
            except OSError as exc:
                notes.append(f"could not read {p}: {type(exc).__name__}")
                continue
            key = (p, file_digest(raw))
            mod = self._cache.get(key)
            if mod is None:
                try:
                    mod = parse_source(p, src)
                except (SyntaxError, ValueError, RecursionError) as exc:
                    notes.append(f"could not parse {p}: {type(exc).__name__} (structure unknown)")
                    continue
            else:
                reused += 1
            cache[key] = mod
            modules[p] = mod
        self._cache = cache
        t1 = time.perf_counter()
        _Builder(g, side, modules).build()
        t2 = time.perf_counter()
        unknown = sum(1 for e in g.edges.values() if e.confidence == "unknown")
        notes.append(f"{len(modules)} Python modules parsed with ast (never imported or executed); tests and migrations excluded")
        if unknown:
            notes.append(f"{unknown} dynamic call edge(s) marked unknown (getattr / dispatch / unresolved attribute)")
        g.notes["python"] = notes
        self.last_stats = {"side": side, "files": len(modules), "parsed": len(modules) - reused, "reused": reused,
                           "parse_s": round(t1 - t0, 3), "build_s": round(t2 - t1, 3)}
        return g


class _Builder:
    def __init__(self, g: SideGraph, side: str, modules: dict[str, ModuleInfo]):
        self.g = g
        self.side = side
        self.modules = modules
        self.idx = _Index(modules)
        self.class_kind: dict[str, str] = {}  # class node id -> kind

    # ---------- helpers ----------
    def ref(self, path: str, start: int, end: int | None = None, symbol: str | None = None) -> CodeRef:
        return CodeRef(self.side, path, start, end, symbol)

    def ev(self, kind: str, path: str, line: int, desc: str, *disc: object, end: int | None = None, producer=AST) -> str:
        eid = ids.evidence_id(kind, self.side, path, line, *disc)
        if eid not in self.g.evidence:
            self.g.evidence[eid] = Evidence(eid, kind, desc, [self.ref(path, line, end)], producer)
        return eid

    def edge(self, kind: str, src: str, dst: str, confidence: str, ev: str) -> str:
        eid = ids.edge_id(kind, src, dst)
        self.g.add_edge(Edge(eid, src, dst, kind, confidence, [ev]))
        return eid

    def resolve_name(self, mod: ModuleInfo, name: str) -> str | None:
        if name in mod.functions or name in mod.classes or name in mod.signals:
            return f"{mod.dotted}.{name}" if mod.dotted else name
        if name in mod.imports:
            return mod.imports[name][0]
        return None

    def resolve_chain(self, mod: ModuleInfo, root: str | None, attrs: tuple[str, ...]) -> tuple | None:
        if root is None:
            return None
        d = self.resolve_name(mod, root)
        return self.idx.lookup(".".join([d, *attrs])) if d else None

    # ---------- build ----------
    def build(self) -> None:
        for mod in self.modules.values():
            self.add_module(mod)
        for mod in self.modules.values():
            self.classify(mod)
        for mod in self.modules.values():
            self.add_symbols(mod)
        for mod in self.modules.values():
            self.add_imports(mod)
            for c in mod.classes.values():
                self.add_class_relations(mod, c)
        self.add_routes()
        self.add_tasks_and_commands()
        for mod in self.modules.values():
            for f in mod.functions.values():
                self.add_flow(mod, f, None)
            for c in mod.classes.values():
                for m in c.methods.values():
                    self.add_flow(mod, m, c)

    def add_module(self, mod: ModuleInfo) -> None:
        nid = _node_id(mod)
        ev = self.ev("ast", mod.path, 1, f"Python module {mod.dotted or mod.path}", "module", end=mod.n_lines)
        # non-project modules this module imports (top-level statements): architecture checks look for mechanisms
        # (threading, multiprocessing...) that bypass the repository's own task framework
        ext = {t: ln for t, ln in sorted(mod.imports.values()) if not self.idx.is_project(t)}
        meta = {"hash": mod.hash, "dotted": mod.dotted, "ext_imports": ext}
        if mod.file_content_type or any(t.split(".")[0] in FILE_WRITER_LIBS for t in ext):
            meta["file_writer"] = True  # writes CSV / spreadsheet files (outbound file formats, spec §8)
        if mod.column_defs:
            meta["column_defs"] = {scope: {**d, "keys": [self.constant(mod, k) for k in d["keys"]]}
                                   for scope, d in sorted(mod.column_defs.items())}
        self.g.add_node(Node(nid, "module", "module", mod.dotted or mod.path, None, [self.ref(mod.path, 1, mod.n_lines)], [ev],
                             language="python", meta=meta))

    def constant(self, mod: ModuleInfo, key: str) -> str:
        """'@NAME' → the string constant NAME (this module's, or imported from a project module); else NAME."""
        if not key.startswith("@"):
            return key
        name = key[1:]
        if name in mod.str_constants:
            return mod.str_constants[name]
        target = mod.imports.get(name, (None,))[0]
        if target and "." in target:
            head, attr = target.rsplit(".", 1)
            hit = self.idx.by_dotted.get(head)
            if hit and hit[0] == "module" and attr in hit[1].str_constants:
                return hit[1].str_constants[attr]
        return name

    def classify(self, mod: ModuleInfo) -> None:
        # Resolve project base classes recursively so subclasses of project models/views inherit the kind.
        def kind_of(m: ModuleInfo, c: ClassInfo, seen: set[str]) -> str:
            cid = _node_id(m, c.name)
            if cid in self.class_kind:
                return self.class_kind[cid]
            if cid in seen:
                return "class"
            seen.add(cid)
            base_kinds = []
            for b in c.bases:
                root, attrs = attr_chain(b)
                if isinstance(root, ast.Name):
                    hit = self.resolve_chain(m, root.id, tuple(attrs))
                    if hit and hit[0] == "class":
                        base_kinds.append(kind_of(hit[1], hit[2], seen))
            k = classify_class(c.base_texts, base_kinds)
            if k == "class" and c.name == "Command" and command_name(m.path):
                k = "command"
            self.class_kind[cid] = k
            return k

        for c in mod.classes.values():
            kind_of(mod, c, set())

    def add_symbols(self, mod: ModuleInfo) -> None:
        mid = _node_id(mod)
        for c in mod.classes.values():
            cid = _node_id(mod, c.name)
            kind = self.class_kind.get(cid, "class")
            ev = self.ev("ast", mod.path, c.lineno, f"class {c.name} ({kind}) in {mod.path}", "class", c.name, end=c.end_lineno)
            contract: dict = {}  # interface facts (spec §8): DRF serializer fields, view → serializer, allowed methods
            if kind == "serializer":
                contract["serializer"] = {"declared": dict(c.field_opts), **c.meta_lists, "has_meta_model": c.meta_model is not None}
            if kind == "view":
                ref = c.class_refs.get("serializer_class")
                hit = self.resolve_chain(mod, ref[0], tuple(ref[1])) if ref else None
                if hit and hit[0] == "class":
                    contract["serializer_class"] = _def_id(hit)
                if "http_method_names" in c.str_lists:
                    contract["http_method_names"] = [m.upper() for m in c.str_lists["http_method_names"]]
            self.g.add_node(Node(
                cid, kind, "symbol", c.name, mid, [self.ref(mod.path, c.lineno, c.end_lineno, c.name)], [ev], language="python",
                tags=["django"] if kind != "class" else [],
                meta={"hash": c.hash, "dotted": f"{mod.dotted}.{c.name}", "bases": c.base_texts, "decorators": c.decorators,
                      "fields": dict(c.fields), "is_class": True, "abstract": c.abstract,
                      "drf": kind == "view" and is_drf(c.base_texts, []), **contract},
            ))
            for m in c.methods.values():
                self.add_function_node(mod, m, cid, "method")
        for f in mod.functions.values():
            if set(f.decorators) & TASK_DECORATORS:
                kind = "task"
            elif is_function_view(mod.path, f.first_param):
                kind = "view"
            else:
                kind = "function"
            self.add_function_node(mod, f, mid, kind)

    def add_function_node(self, mod: ModuleInfo, f: FuncInfo, parent: str, kind: str) -> None:
        nid = _node_id(mod, f.qualname)
        ev = self.ev("ast", mod.path, f.lineno, f"def {f.qualname} in {mod.path}", "def", f.qualname, end=f.end_lineno)
        self.g.add_node(Node(
            nid, kind, "symbol", f.qualname, parent, [self.ref(mod.path, f.lineno, f.end_lineno, f.qualname)], [ev],
            language="python", tags=["django"] if kind in ("view", "task") else [],
            meta={"hash": f.hash, "dotted": f"{mod.dotted}.{f.qualname}", "signature": f.signature,
                  "decorators": f.decorators, "is_class": False, "drf": kind == "view" and "api_view" in f.decorators,
                  **({"uses_request_user": True} if kind == "view" and f.uses_param_user else {}),
                  **({"api_methods": f.api_methods} if f.api_methods is not None else {})},
        ))

    def add_imports(self, mod: ModuleInfo) -> None:
        mid = _node_id(mod)
        for alias, (target, line) in sorted(mod.imports.items()):
            if not self.idx.is_project(target):
                continue
            hit = self.idx.lookup(target)
            if hit is None:
                head = target.rsplit(".", 1)[0] if "." in target else ""
                parent = self.idx.lookup(head) if head else None
                if parent and parent[0] == "module" and target.rsplit(".", 1)[-1] not in parent[1].signals:
                    self.g.unresolved.append(UnresolvedRef(target, self.ref(mod.path, line), mid))
                continue
            tid = _def_id(hit)
            if tid and tid != mid:
                ev = self.ev("import", mod.path, line, f"{mod.path}:{line} imports {target}", target)
                self.edge("imports", mid, tid, "confirmed", ev)

    def add_class_relations(self, mod: ModuleInfo, c: ClassInfo) -> None:
        cid = _node_id(mod, c.name)
        for b in c.bases:
            root, attrs = attr_chain(b)
            if not isinstance(root, ast.Name):
                continue
            hit = self.resolve_chain(mod, root.id, tuple(attrs))
            if hit and hit[0] == "class":
                ev = self.ev("ast", mod.path, b.lineno, f"{c.name} inherits {text(b)}", "inherits", text(b))
                self.edge("inherits", cid, _def_id(hit), "confirmed", ev)
        if c.meta_model is not None:
            root, attrs = attr_chain(c.meta_model)
            if isinstance(root, ast.Name):
                hit = self.resolve_chain(mod, root.id, tuple(attrs))
                if hit and hit[0] == "class" and self.class_kind.get(_def_id(hit)) == "model":
                    ev = self.ev("framework_rule", mod.path, c.meta_model.lineno,
                                 f"{c.name}.Meta.model = {text(c.meta_model)} (Django)", "meta_model", producer=DJANGO)
                    self.edge("uses_model", cid, _def_id(hit), "confirmed", ev)
        if self.class_kind.get(cid) != "view":
            return
        steps = []
        dispatch = [(m, f'request.method == "{m.upper()}"', "View.dispatch()") for m in HTTP_METHODS]
        if is_viewset(c.base_texts):
            dispatch += [(a, f'viewset action == "{a}"', "ViewSet routing") for a in VIEWSET_ACTIONS]
            dispatch += [(a, f'@action "{a}" requested', "ViewSet @action routing") for a in sorted(c.action_methods)]
        for m, cond, how in dispatch:
            if m not in c.methods:
                continue
            mi = c.methods[m]
            tid = _node_id(mod, mi.qualname)
            ev = self.ev("framework_rule", mod.path, mi.lineno, f"Django {how} calls {c.name}.{m}() when {cond}", "dispatch", m,
                         producer=DJANGO)
            eid = self.edge("calls", cid, tid, "confirmed", ev)
            steps.append(FlowStep(mod.path, mi.lineno, mi.lineno, "call", f"{how} → {c.name}.{m}()", [tid], [eid],
                                  conditions=(cond,), evidence_refs=[ev]))
        if steps:
            self.g.flows[cid] = steps

    # ---------- entry points: django urls + DRF routers ----------
    def add_routes(self) -> None:
        urls = {p: m for p, m in self.modules.items() if m.routes or m.routers}
        included: dict[str, list[tuple[str, str]]] = {}  # child path -> [(parent path, prefix)]
        for p, m in urls.items():
            for e in m.routes:
                if e.include_target is None:
                    continue
                target = self._module_for_include(m, e.include_target)
                if target:
                    included.setdefault(target, []).append((p, e.pattern))
        prefix_cache: dict[str, str] = {}

        def prefix(p: str, depth: int = 0) -> str:
            if p in prefix_cache:
                return prefix_cache[p]
            parents = sorted(included.get(p, []))
            pre = "" if not parents or depth > 8 else prefix(parents[0][0], depth + 1) + parents[0][1]
            prefix_cache[p] = pre
            return pre

        for p, mod in sorted(urls.items()):
            seen: dict[str, int] = {}
            for e in mod.routes:
                if e.view_expr is None:
                    continue
                target = self._view_target(mod, e.view_expr)
                idx = seen.get(e.pattern, 0)
                seen[e.pattern] = idx + 1
                label = display_pattern(prefix(p), e)
                self._route(p, mod, ids.route_id(p, e.pattern, idx), label, e.line, e.end_line, target,
                            f"Django route {label} declared in {p}:{e.line}", ("route", e.pattern, idx), e.name)
            routers = mod.routers
            for i, (var, reg_prefix, expr, line, end) in enumerate(routers.get("registrations", [])):
                mount = routers.get("mounts", {}).get(var, "")
                label = "/" + (prefix(p) + mount + reg_prefix.strip("^$")).lstrip("/").rstrip("/") + "/"
                target = self._view_target(mod, expr)
                self._route(p, mod, ids.route_id(p, f"router:{reg_prefix}", i), label, line, end, target,
                            f"DRF router {var}.register({reg_prefix!r}) at {p}:{line} → {label}", ("router", reg_prefix, i),
                            None, api=True)

    def _route(self, p: str, mod: ModuleInfo, rid: str, label: str, line: int, end: int, target: str | None, desc: str,
               disc: tuple, url_name: str | None, api: bool = False) -> None:
        ev = self.ev("framework_rule", p, line, desc, *disc, end=end, producer=DJANGO)
        self.g.add_node(Node(rid, "route", "symbol", label, _node_id(mod), [self.ref(p, line, end)], [ev], language="python",
                             tags=["django", "entry_point"], meta={"hash": ids.short_hash(label, target), "url_name": url_name}))
        if target is not None:
            tnode = self.g.nodes[target]
            if tnode.kind in ("function", "method"):
                tnode.kind = "view"
            api = api or bool(tnode.meta.get("drf"))
            eid = self.edge("routes_to", rid, target, "confirmed", ev)
            self.g.flows[rid] = [FlowStep(p, line, end, "call", f"{label} → {tnode.label}", [target], [eid], evidence_refs=[ev])]
        self.g.entry_points.append(EntryFact(rid, "api" if api else "route", label, target, self.ref(p, line, end), ev))

    def _module_for_include(self, mod: ModuleInfo, expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            hit = self.idx.lookup(expr.value)
        else:
            root, attrs = attr_chain(expr)
            hit = self.resolve_chain(mod, root.id, tuple(attrs)) if isinstance(root, ast.Name) else None
        return hit[1].path if hit and hit[0] == "module" else None

    def _view_target(self, mod: ModuleInfo, expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "as_view":
            expr = expr.func.value
        root, attrs = attr_chain(expr)
        if not isinstance(root, ast.Name):
            return None
        hit = self.resolve_chain(mod, root.id, tuple(attrs))
        if hit and hit[0] in ("function", "class", "method"):
            return _def_id(hit)
        return None

    # ---------- entry points: celery tasks, beat schedules, management commands ----------
    def add_tasks_and_commands(self) -> None:
        for mod in self.modules.values():
            for f in mod.functions.values():
                decs = set(f.decorators) & TASK_DECORATORS
                if not decs:
                    continue
                nid = _node_id(mod, f.qualname)
                kind = "schedule" if "periodic_task" in decs else "task"
                ev = self.ev("framework_rule", mod.path, f.lineno, f"@{sorted(decs)[0]} makes {mod.dotted}.{f.name} a background task",
                             "task", f.name, producer=DJANGO)
                self.g.entry_points.append(EntryFact(nid, kind, f"task {mod.dotted}.{f.name}", nid,
                                                     self.ref(mod.path, f.lineno, f.end_lineno), ev))
            for key, task, line in mod.beat:
                hit = self.idx.lookup(task)
                target = _def_id(hit) if hit and hit[0] in ("function", "method") else None
                ev = self.ev("framework_rule", mod.path, line, f"Celery beat schedule '{key}' runs {task}", "beat", key, producer=DJANGO)
                self.g.entry_points.append(EntryFact(f"node:schedule:{mod.path}::{key}", "schedule", f"schedule '{key}' → {task}",
                                                     target, self.ref(mod.path, line), ev))
            name = command_name(mod.path)
            if name and "Command" in mod.classes:
                c = mod.classes["Command"]
                cid = _node_id(mod, "Command")
                handler = _node_id(mod, c.methods["handle"].qualname) if "handle" in c.methods else cid
                ev = self.ev("framework_rule", mod.path, c.lineno, f"Django management command `manage.py {name}`", "command", name,
                             producer=DJANGO)
                self.g.entry_points.append(EntryFact(cid, "command", f"manage.py {name}", handler,
                                                     self.ref(mod.path, c.lineno, c.end_lineno), ev))

    # ---------- statement facts ----------
    def add_flow(self, mod: ModuleInfo, f: FuncInfo, cls: ClassInfo | None) -> None:
        nid = _node_id(mod, f.qualname)
        r = _Resolver(self, mod, f, cls, nid)
        r.run()
        if r.steps or nid not in self.g.flows:
            self.g.flows[nid] = r.steps


class _Resolver:
    def __init__(self, b: _Builder, mod: ModuleInfo, f: FuncInfo, cls: ClassInfo | None, nid: str):
        self.b, self.mod, self.f, self.cls, self.nid = b, mod, f, cls, nid
        self.steps: list[FlowStep] = []

    def run(self) -> None:
        for ev in self.f.events:
            if isinstance(ev, StmtRec):
                if ev.kind == "raise":
                    e = self.b.ev("ast", self.mod.path, ev.line, f"{self.mod.path}:{ev.line} {ev.label}", "raise", ev.label)
                    self.steps.append(FlowStep(self.mod.path, ev.line, ev.end, "raise", ev.label, conditions=ev.conds,
                                               evidence_refs=[e]))
                continue  # branch/return statements carry no business step
            self.resolve(ev)

    # -- emitters --
    def _step(self, c: CallRec, kind: str, label: str, targets: list[str], edges: list[str], conf: str, ev: list[str],
              action: str | None = None) -> None:
        self.steps.append(FlowStep(self.mod.path, c.line, c.end, kind, label, targets, edges, conf, c.conds, c.shape, ev, action))

    def confirmed_call(self, c: CallRec, target: str) -> None:
        b = self.b
        tl = b.g.nodes[target].label if target in b.g.nodes else target
        ev = b.ev("ast", self.mod.path, c.line, f"{self.mod.path}:{c.line} calls {tl}", "call", target)
        eid = b.edge("calls", self.nid, target, "confirmed", ev)
        self._step(c, "call", f"{c.label}()", [target], [eid], "confirmed", [ev])

    def query(self, c: CallRec, model_id: str, action: str) -> None:
        b = self.b
        ev = b.ev("framework_rule", self.mod.path, c.line, f"{c.label}() {action}s {b.g.nodes[model_id].label} via the Django ORM",
                  "orm", model_id, action, producer=DJANGO)
        eid = b.edge("uses_model", self.nid, model_id, "confirmed", ev)
        self._step(c, "query", f"{c.label}()", [model_id], [eid], "confirmed", [ev], action)

    def external(self, c: CallRec, what: str) -> None:
        ev = self.b.ev("ast", self.mod.path, c.line, f"{self.mod.path}:{c.line} calls {what} (outside this repository)", "external",
                       c.label)
        self._step(c, "external", f"{c.label}()", [], [], "confirmed", [ev])

    def emit(self, c: CallRec, target: str | None, what: str) -> None:
        b = self.b
        ev = b.ev("ast", self.mod.path, c.line, f"{self.mod.path}:{c.line} {what}", "emit", c.label)
        edges = [b.edge("emits", self.nid, target, "confirmed", ev)] if target else []
        self._step(c, "emit", f"{c.label}()", [target] if target else [], edges, "confirmed", [ev])

    def unknown(self, c: CallRec, why: str, candidates: list[str], ext_key: str | None = None) -> None:
        b = self.b
        targets = candidates[:MAX_CANDIDATES]
        ev = b.ev("ast", self.mod.path, c.line, f"{self.mod.path}:{c.line} {why}; target cannot be resolved statically",
                  "dynamic", c.label)
        if not targets:
            key = ext_key or f"dynamic:{self.mod.path}:{c.line}:{c.col}"
            xid = ids.external_id(key)
            # evidence = the call that created this placeholder, so the UI can explain why it exists
            b.g.add_node(Node(xid, "external", "symbol", f"unresolved: {c.label}", None, [], [ev], language="python",
                              tags=["unknown"], meta={"hash": key}))
            targets = [xid]
        edges = [b.edge("calls", self.nid, t, "unknown", ev) for t in targets]
        names = ", ".join(b.g.nodes[t].label for t in targets[:4])
        self._step(c, "call", f"dynamic call {c.label}() → one of: {names}", targets, edges, "unknown", [ev])

    # -- candidates for dynamic dispatch --
    def _candidates(self, root: str | None, attrs: tuple[str, ...], pattern: str, explicit: bool) -> list[str]:
        if root is None:
            return []
        if root == "self" and self.cls is not None and not attrs:
            members = {m: _node_id(self.mod, mi.qualname) for m, mi in self.cls.methods.items()}
        else:
            hit = self.b.resolve_chain(self.mod, root, attrs) if root != "self" else None
            if hit is None:
                return []
            if hit[0] == "module":
                m = hit[1]
                members = {n: _node_id(m, n) for n in list(m.functions) + list(m.classes)}
            elif hit[0] == "class":
                members = {n: _node_id(hit[1], mi.qualname) for n, mi in hit[2].methods.items()}
            else:
                return []
        rx = re.compile(pattern)
        return [nid for n, nid in sorted(members.items()) if rx.fullmatch(n) and (explicit or not n.startswith("_"))][:MAX_CANDIDATES]

    def _dict_candidates(self, name: str | None) -> list[str]:
        val = self.mod.assigns.get(name) if name else None
        if not isinstance(val, ast.Dict):
            return []
        out = set()
        for v in val.values:
            root, attrs = attr_chain(v)
            if isinstance(root, ast.Name):
                hit = self.b.resolve_chain(self.mod, root.id, tuple(attrs))
                if hit and hit[0] in ("function", "class", "method"):
                    out.add(_def_id(hit))
        return sorted(out)

    def _is_signal(self, dotted: str) -> bool:
        head, _, name = dotted.rpartition(".")
        hit = self.b.idx.lookup(head) if head else None
        return bool(hit and hit[0] == "module" and name in hit[1].signals)

    # -- resolution --
    def resolve(self, c: CallRec) -> None:
        b = self.b
        t = c.target
        kind = t[0]
        if kind == "getattr":
            _, root, attrs, pattern, explicit = t
            obj = ".".join([root or "?", *attrs])
            self.unknown(c, f"calls getattr({obj}, <name>)", self._candidates(root, attrs, pattern, explicit))
            return
        if kind == "subscript":
            self.unknown(c, f"dispatches through {t[1] or 'a subscript'}[...]", self._dict_candidates(t[1]))
            return
        if kind == "enqueue":
            hit = b.resolve_chain(self.mod, t[1], t[2])
            if hit and hit[0] in ("function", "method"):
                tid = _def_id(hit)
                self.emit(c, tid, f"enqueues background task {b.g.nodes[tid].label if tid in b.g.nodes else tid}")
            return
        if kind == "instance":
            _, root, model_attrs, method = t
            hit = b.resolve_chain(self.mod, root, model_attrs)
            if hit and hit[0] == "class" and b.class_kind.get(_def_id(hit)) == "model" and method in INSTANCE_ACTIONS:
                self.query(c, _def_id(hit), INSTANCE_ACTIONS[method])
            return
        if kind == "name":
            d = b.resolve_name(self.mod, t[1])
            if d is None:
                return
            if not b.idx.is_project(d):
                if d in EXTERNAL_CALLS or d.split(".")[0] in EXTERNAL_MODULES:
                    self.external(c, d)
                return
            hit = b.idx.lookup(d)
            if hit and hit[0] in ("function", "class", "method"):
                self.confirmed_call(c, _def_id(hit))
            elif hit is None and not self._is_signal(d):
                b.g.unresolved.append(UnresolvedRef(d, b.ref(self.mod.path, c.line), self.nid))
                self.unknown(c, f"calls {d} which is not defined in the project", [], ext_key=f"unresolved:{d}")
            return
        # chain
        _, root, attrs = t
        if root in ("self", "cls") and self.cls is not None:
            if len(attrs) == 1:
                target = self._method_in_hierarchy(self.mod, self.cls, attrs[0], set())
                if target:
                    self.confirmed_call(c, target)
            return
        d = b.resolve_name(self.mod, root)
        if d is None:
            return
        if not b.idx.is_project(d):
            full = ".".join([d, *attrs])
            if full in EXTERNAL_CALLS or d.split(".")[0] in EXTERNAL_MODULES:
                self.external(c, full)
            return
        if attrs[-1] in ("send", "send_robust") and self._is_signal(".".join([d, *attrs[:-1]])):
            self.emit(c, None, f"sends signal {'.'.join([d, *attrs[:-1]])}")
            return
        attrs = list(attrs)
        hit = b.idx.lookup(d)
        if hit is None:
            # maybe `import pkg.mod` style: walk the dotted prefix
            for i in range(len(attrs), 0, -1):
                h = b.idx.lookup(".".join([d] + attrs[:i]))
                if h:
                    hit, d, attrs = h, ".".join([d] + attrs[:i]), attrs[i:]
                    break
        if hit is None:
            return
        if hit[0] == "class":
            cid = _def_id(hit)
            if b.class_kind.get(cid) == "model" and attrs and attrs[0] in MANAGER_ATTRS:
                self.query(c, cid, orm_action(attrs[1]) if len(attrs) > 1 else "read")
                return
            if len(attrs) == 1:
                target = self._method_in_hierarchy(hit[1], hit[2], attrs[0], set())
                if target:
                    self.confirmed_call(c, target)
            return
        if hit[0] == "module" and attrs:
            sub = b.idx.lookup(f"{d}.{attrs[0]}")
            if sub is None:
                full = f"{hit[1].dotted}.{attrs[0]}"
                if attrs[0] in hit[1].signals:
                    if len(attrs) > 1 and attrs[1] in ("send", "send_robust"):
                        self.emit(c, None, f"sends signal {full}")
                    return
                b.g.unresolved.append(UnresolvedRef(full, b.ref(self.mod.path, c.line), self.nid))
                self.unknown(c, f"calls {full}, an attribute not defined in that module", [], ext_key=f"unresolved:{full}")
                return
            if sub[0] == "class":
                sub_id = _def_id(sub)
                if b.class_kind.get(sub_id) == "model" and len(attrs) > 1 and attrs[1] in MANAGER_ATTRS:
                    self.query(c, sub_id, orm_action(attrs[2]) if len(attrs) > 2 else "read")
                    return
                if len(attrs) == 1:
                    self.confirmed_call(c, sub_id)
                elif len(attrs) == 2:
                    tt = self._method_in_hierarchy(sub[1], sub[2], attrs[1], set())
                    if tt:
                        self.confirmed_call(c, tt)
                return
            if sub[0] in ("function", "method") and len(attrs) == 1:
                self.confirmed_call(c, _def_id(sub))

    def _method_in_hierarchy(self, mod: ModuleInfo, cls: ClassInfo, name: str, seen: set[str]) -> str | None:
        key = f"{mod.path}::{cls.name}"
        if key in seen:
            return None
        seen.add(key)
        if name in cls.methods:
            return _node_id(mod, cls.methods[name].qualname)
        for base in cls.bases:
            root, attrs = attr_chain(base)
            if not isinstance(root, ast.Name):
                continue
            hit = self.b.resolve_chain(mod, root.id, tuple(attrs))
            if hit and hit[0] == "class":
                t = self._method_in_hierarchy(hit[1], hit[2], name, seen)
                if t:
                    return t
        return None
