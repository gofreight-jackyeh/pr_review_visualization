"""Interface rings (spec §8, owner 2026-11-11): start from interfaces — the more outer (closer to users / clients) an
interface is, the less it should change; changes should stay inner. Pure.

Detection (one side; the system map lists the base side):
  outer   http_api        Django / DRF / Django Ninja routes that are API views or sit under an API path (API_PREFIXES /
                          API_SEGMENTS); contract = allowed HTTP methods + the fields of the serializers the view uses
                          (when resolvable), or a Ninja operation's parameters and schema classes; plus every operation
                          of a committed OpenAPI document (RepoSurface.api_specs), merged into the route it describes
                          (same method + path, or Ninja's default operationId) — the document is the contract then
          web_route       front-end pages / routes (RepoSurface.web_routes)
          sdk_export      top-level exports of published packages (RepoSurface.packages)
          outbound_format files that shape data leaving the system (OUTBOUND_PATTERNS, or `.pr-explorer.yml outer.paths`)
  middle  module_api      modules of a capability used by other capabilities, or across a layer boundary of an
                          import-linter layers contract; contract = signatures of the symbols those users call / import
          task            background tasks; contract = the task signature
`.pr-explorer.yml` wins over detection: `outer` adds interfaces (detected_by=config), `exclude_outer` removes them.

Diff (PR overlay): base vs head contracts → one `interface_change` check per changed interface: added → compatible;
removed route / export / module, removed or renamed field, new required field or parameter, removed HTTP method,
changed task signature → breaking (verified); a change whose effect cannot be decided statically → unknown
(needs confirmation). Verdict: compatible → follows, breaking → violates, unknown → unclear.
OpenAPI / Ninja operations (OP_RULES): removed operation, parameter or request / response field, a parameter or request
field that becomes required, a parameter that moves (query → path) → breaking; added operation, optional parameter or
field, response field, something that becomes optional in the request → compatible; type changes and a response field
that may now be missing → unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from ..model import ids
from ..model.convention import ImportContract, RepoConfig, RepoSurface
from ..model.document import ArchCheck, Interface, InterfaceChange, Text
from ..model.graph import CodeRef, Evidence, Producer, SideGraph
from .architecture import check_order, module_dotted, source_prefix
from .system_structure import Structure

PRODUCER = Producer("interfaces", "1")
API_PREFIXES = ("/api",)  # a route whose URL starts with one of these is public HTTP API ...
API_SEGMENT = re.compile(r"^api(?:[-_]?v\d+)?$")  # ... or that has an `api` / `api-v2` path segment (e.g. /payments/api/x/)
OUTBOUND_PATTERNS = (  # files whose content leaves the system (EDI, webhooks, e-mail / notification templates)
    "**/edi/**", "**/edifact/**", "**/x12/**", "**/*_edi.py", "**/edi_*.py", "**/outbound/**", "**/webhooks/**",
    "**/email_templates/**", "**/templates/email/**", "**/templates/emails/**", "**/notifications/templates/**",
)
MAX_MODULE_INTERFACES = 1500  # middle-ring module APIs listed in the map: the most used first (ceiling on huge repos)
MAX_OUTBOUND = 500
MAX_SYMBOLS = 40
HTTP = ("get", "post", "put", "patch", "delete")
ACTION_METHODS = {"list": "GET", "retrieve": "GET", "create": "POST", "update": "PUT", "partial_update": "PATCH",
                  "destroy": "DELETE"}
GENERIC_METHODS = {
    "ListAPIView": "GET", "RetrieveAPIView": "GET", "CreateAPIView": "POST", "DestroyAPIView": "DELETE",
    "UpdateAPIView": "PUT PATCH", "ListCreateAPIView": "GET POST", "RetrieveUpdateAPIView": "GET PUT PATCH",
    "RetrieveDestroyAPIView": "GET DELETE", "RetrieveUpdateDestroyAPIView": "GET PUT PATCH DELETE",
    "ModelViewSet": "GET POST PUT PATCH DELETE", "ReadOnlyModelViewSet": "GET", "ListModelMixin": "GET",
    "RetrieveModelMixin": "GET", "CreateModelMixin": "POST", "UpdateModelMixin": "PUT PATCH", "DestroyModelMixin": "DELETE",
}
READ_ONLY_TYPES = {"SerializerMethodField", "ReadOnlyField"}
SKIPPED_TYPES = {"HiddenField"}  # neither read nor written by the client
RANK = {"compatible": 0, "unknown": 1, "breaking": 2}
VERDICT = {"compatible": "follows", "breaking": "violates", "unknown": "unclear"}


def _t(lang: str, en: str, zh: str) -> str:
    return zh if lang == "zh-Hant" else en


@lru_cache(maxsize=512)
def _glob_rx(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def glob_match(pattern: str, value: str) -> bool:
    """`*` = within one path segment, `**` = any depth (also `**/x` matches `x`). Routes match with or without the
    trailing slash."""
    p = pattern.strip()
    return any(_glob_rx(x).fullmatch(y) for x in {p, p.rstrip("/") or p} for y in {value, value.rstrip("/") or value})


def _config_hit(cfg: RepoConfig, which: str, key: str, value: str) -> tuple[str, int] | None:
    for g, line in getattr(cfg, which).get(key, []):
        if glob_match(g, value):
            return g, line
    return None


def is_api_route(label: str) -> bool:
    if any(label == p or label.startswith(p + "/") for p in API_PREFIXES):
        return True
    return any(API_SEGMENT.match(s) for s in label.split("/") if s)


# ---------------------------------------------------------------- contracts

@dataclass
class _Graph:
    sg: SideGraph
    children: dict[str, list[str]] = field(default_factory=dict)
    calls: dict[str, list[str]] = field(default_factory=dict)
    inherits: dict[str, list[str]] = field(default_factory=dict)
    imports: dict[str, list[str]] = field(default_factory=dict)  # module id -> imported node ids

    @classmethod
    def of(cls, sg: SideGraph) -> "_Graph":
        g = cls(sg)
        for n in sg.nodes.values():
            if n.parent_id:
                g.children.setdefault(n.parent_id, []).append(n.id)
        for e in sorted(sg.edges.values(), key=lambda e: e.id):
            if e.kind == "calls" and e.confidence == "confirmed":
                g.calls.setdefault(e.source, []).append(e.target)
            elif e.kind == "inherits":
                g.inherits.setdefault(e.source, []).append(e.target)
            elif e.kind == "imports":
                g.imports.setdefault(e.source, []).append(e.target)
        return g

    def methods(self, handler: str | None, depth: int = 0) -> list[str] | None:
        n = self.sg.nodes.get(handler or "")
        if n is None or depth > 5:
            return None
        if not n.meta.get("is_class"):
            api = n.meta.get("api_methods")
            return None if api is None else sorted(set(api) or {"GET"})
        ms: set[str] = set()
        for c in self.children.get(n.id, []):
            short = self.sg.nodes[c].label.rsplit(".", 1)[-1]
            if short in HTTP:
                ms.add(short.upper())
            elif short in ACTION_METHODS:
                ms.add(ACTION_METHODS[short])
        for b in n.meta.get("bases") or []:
            ms |= set(GENERIC_METHODS.get(b.rsplit(".", 1)[-1], "").split())
        for b in self.inherits.get(n.id, []):
            ms |= set(self.methods(b, depth + 1) or [])
        allowed = n.meta.get("http_method_names")
        if allowed:
            ms &= set(allowed)
        return sorted(ms) or None

    def serializers(self, handler: str | None) -> list[str]:
        n = self.sg.nodes.get(handler or "")
        if n is None:
            return []
        found = {n.meta["serializer_class"]} if n.meta.get("serializer_class") else set()
        for src in [n.id] + self.children.get(n.id, []):
            found |= {t for t in self.calls.get(src, []) if t in self.sg.nodes and self.sg.nodes[t].kind == "serializer"}
        return sorted(x for x in found if x in self.sg.nodes)

    def file_columns(self, handler: str | None) -> dict[str, dict] | None:
        """For a handler that returns a file (its module, or a module it imports, writes CSV / spreadsheets or names a
        file content type): the column definitions of those modules, `path::scope` → {keys, dynamic}. None otherwise."""
        n = self.sg.nodes.get(handler or "")
        if n is None:
            return None
        hm = handler.split("::", 1)[0]
        reach = [m for m in dict.fromkeys([hm, *(t.split("::", 1)[0] for t in self.imports.get(hm, []))]) if m in self.sg.nodes]
        if not any(self.sg.nodes[m].meta.get("file_writer") for m in reach):
            return None
        out: dict[str, dict] = {}
        for m in reach:
            node = self.sg.nodes[m]
            path = node.code_refs[0].path if node.code_refs else m
            for scope, d in (node.meta.get("column_defs") or {}).items():
                out[f"{path}::{scope}"] = {"keys": list(d["keys"]), "dynamic": bool(d.get("dynamic"))}
        return out

    def fields(self, sid: str, depth: int = 0) -> dict[str, dict] | None:
        """Serializer fields → {read_only, write_only, required (True / False / None = decided by the model)};
        None when the field list cannot be resolved statically (Meta.fields = "__all__", exclude, computed)."""
        n = self.sg.nodes.get(sid)
        spec = n.meta.get("serializer") if n is not None else None
        if spec is None or depth > 5:
            return None
        out: dict[str, dict] = {}
        for b in self.inherits.get(sid, []):
            got = self.fields(b, depth + 1)
            if got is None and self.sg.nodes.get(b) is not None and self.sg.nodes[b].meta.get("serializer"):
                return None
            out.update(got or {})
        declared: dict[str, dict] = spec.get("declared") or {}
        listed = spec.get("fields")
        if spec.get("exclude") is not None or listed in ("__all__", "dynamic") or (spec.get("has_meta_model") and listed is None
                                                                                 and not out):
            return None
        ro = spec.get("read_only_fields") if isinstance(spec.get("read_only_fields"), list) else []
        extra = spec.get("extra_kwargs") or {}
        names = list(listed) if isinstance(listed, list) else []
        names += [k for k in declared if k not in names and not isinstance(listed, list)]
        for name in names:
            o = {**declared.get(name, {"type": None}), **extra.get(name, {})}
            if o.get("type") in SKIPPED_TYPES:
                continue
            read_only = bool(o.get("read_only")) or name in ro or o.get("type") in READ_ONLY_TYPES
            if read_only:
                req = False
            elif "required" in o:
                req = bool(o["required"])
            elif o.get("default"):
                req = False
            else:
                req = None if o.get("type") is None else True
            out[name] = {"read_only": read_only, "write_only": bool(o.get("write_only")), "required": req}
        return out


    def schema_fields(self, sid: str | None, depth: int = 0) -> dict[str, dict] | None:
        """Fields of a pydantic / Ninja schema class (inherited ones included) → {required, type}; None when not
        resolvable statically (ModelSchema fields come from the model)."""
        n = self.sg.nodes.get(sid or "")
        spec = n.meta.get("schema") if n is not None else None
        if spec is None or spec.get("model_schema") or depth > 5:
            return None
        out: dict[str, dict] = {}
        for b in self.inherits.get(n.id, []):
            if self.sg.nodes.get(b) is not None and self.sg.nodes[b].meta.get("schema") is not None:
                got = self.schema_fields(b, depth + 1)
                if got is None:
                    return None
                out.update(got)
        out.update(spec["fields"])
        return out

    def ninja_shape(self, handler: str | None) -> dict | None:
        """A Ninja operation's contract in the OpenAPI operation shape (see ApiOperation): params, request, response."""
        n = self.sg.nodes.get(handler or "")
        meta = n.meta.get("ninja") if n is not None else None
        if meta is None:
            return None
        params = {k: dict(v) for k, v in meta["params"].items()}
        for pname, sid in meta["query_schemas"].items():
            fields = self.schema_fields(sid)
            if fields is None:
                params[pname] = {"in": "query", "required": None, "type": "unresolved schema"}
            else:
                params.update({f: {"in": "query", "required": o["required"], "type": o["type"]} for f, o in fields.items()})
        body = meta["body"]
        request: dict | None = {}
        if len(body) == 1:
            request = self.schema_fields(next(iter(body.values())))
        elif body:  # several body parameters: Ninja nests each under its name
            request = {}
            for pname, sid in body.items():
                fields = self.schema_fields(sid)
                if fields is None:
                    request = None
                    break
                request.update({f"{pname}.{f}": o for f, o in fields.items()})
        response = self.schema_fields(meta["response"]) if meta["response_declared"] else None
        return {"params": params, "request": request, "response": response}


def _signature(n) -> dict | None:
    sig = n.meta.get("signature") if n is not None else None
    if sig is None:
        return None
    if sig.get("positional", [])[:1] == ["self"]:  # bound task (bind=True): self is not passed by the caller
        sig = {**sig, "positional": sig["positional"][1:], "required_positional": max(sig.get("required_positional", 0) - 1, 0)}
    return sig


def _symbols(sg: SideGraph, ids_: list[str]) -> dict[str, dict | None]:
    return {sg.nodes[i].label: _signature(sg.nodes[i]) for i in sorted(ids_)[:MAX_SYMBOLS] if i in sg.nodes}


# ---------------------------------------------------------------- detection

@dataclass
class InterfaceSet:
    interfaces: dict[str, Interface] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)


def detect_interfaces(sg: SideGraph, st: Structure, surface: RepoSurface, config: RepoConfig,
                      contracts: list[ImportContract] | None = None, lang: str = "en") -> InterfaceSet:
    """`config`: the base side's `.pr-explorer.yml` (both sides of a PR use it, so a PR editing it does not move the
    rings until merged)."""
    side, caps = sg.side, st.capabilities
    out = InterfaceSet()
    g = _Graph.of(sg)

    def ev(kind: str, path: str, line: int, desc: str, *disc: object) -> str:
        eid = ids.evidence_id(kind, side, path, line, "iface", *disc)
        out.evidence.setdefault(eid, Evidence(eid, kind, desc[:500], [CodeRef(side, path, line)], PRODUCER))
        return eid

    def config_ev(hit: tuple[str, int]) -> str:
        return ev("doc", config.path or ".pr-explorer.yml", hit[1], f"{config.path}:{hit[1]} declares '{hit[0]}' as outer interface",
                  "config", hit[0])

    def excluded(key: str, value: str) -> bool:
        return _config_hit(config, "exclude_outer", key, value) is not None

    def put(i: Interface) -> None:
        if i.capability_id in caps and i.id not in out.interfaces:
            out.interfaces[i.id] = i

    def desc(text: str, evs: list[str]) -> Text:
        return Text(text, "static", evs[:3])

    # ---- outer: HTTP API (routes) ----
    route_modules: set[str] = set()
    for epid, ep in sorted(st.entry_points.items()):
        fact = st.facts.get(epid)
        if fact is None or ep.kind not in ("api", "route"):
            continue
        handler = sg.nodes.get(fact.handler_id or "")
        hpath = handler.code_refs[0].path if handler is not None and handler.code_refs else fact.ref.path
        hit = (_config_hit(config, "outer", "routes", fact.label) or _config_hit(config, "outer", "paths", fact.ref.path)
               or _config_hit(config, "outer", "paths", hpath))
        if not (hit or ep.kind == "api" or is_api_route(fact.label)):
            continue
        if excluded("routes", fact.label) or excluded("paths", fact.ref.path) or excluded("paths", hpath):
            continue
        route_modules |= {fact.ref.path, hpath}
        methods = g.methods(fact.handler_id)
        sers = g.serializers(fact.handler_id)
        fields = {sg.nodes[s].meta.get("dotted") or s: g.fields(s) for s in sers}
        resolved = bool(sers) and all(v is not None for v in fields.values())
        evs = [fact.evidence_id] + ([config_ev(hit)] if hit else []) + [e for s in sers for e in sg.nodes[s].evidence_refs[:1]]
        refs = [fact.ref] + [r for s in sers for r in sg.nodes[s].code_refs[:1]]
        shown = sorted({f for v in fields.values() if v for f in v})
        files = g.file_columns(fact.handler_id)
        cols = sorted({k for d in (files or {}).values() for k in d["keys"]})
        text = (_t(lang, f"{' '.join(methods) if methods else 'HTTP'} {fact.label}; fields: ", f"{' '.join(methods) if methods else 'HTTP'} {fact.label}；欄位：")
                + (", ".join(shown[:20]) if resolved else _t(lang, "not resolvable statically", "無法靜態判定"))
                + (_t(lang, f"; returns a file with {len(cols)} column key(s)", f"；回傳檔案，{len(cols)} 個欄位鍵") if files is not None else ""))
        ninja = g.ninja_shape(fact.handler_id)
        name = f"{' '.join(methods or [])} {fact.label}".strip() if ninja is not None else fact.label  # one per operation
        put(Interface(f"iface:{epid}", name, "outer", "http_api", ep.capability_id, "config" if hit else "static",
                      [CodeRef(r.side, r.path, r.start_line, r.end_line) for r in refs[:4]], evs[:6], entry_point_id=epid,
                      description=desc(text, evs),
                      meta={"contract": {"methods": methods, "serializers": fields if resolved else None, "file_columns": files,
                                         "ninja": ninja},
                            "handler": fact.handler_id, "route": fact.label, "watch": [fact.handler_id or "", *sers]}))

    # ---- outer: committed OpenAPI documents (merged into the route each operation describes) ----
    _api_specs(out, sg, g, st, surface, lang, ev, excluded, put)

    # ---- outer: web app routes ----
    for w in surface.web_routes:
        cid = st.caps.cap_id(w.path)
        if cid is None or excluded("routes", w.route) or excluded("paths", w.path):
            continue
        e = ev("framework_rule", w.path, w.line, f"Web app route {w.route} ({w.how.replace('_', ' ')}) in {w.path}:{w.line}",
               "web", w.route)
        put(Interface(f"iface:web:{w.route}", w.route, "outer", "web_route", cid, "static", [CodeRef(side, w.path, w.line)], [e],
                      meta={"contract": {"route": w.route}}))

    # ---- outer: published package exports ----
    for p in surface.packages:
        hit = _config_hit(config, "outer", "packages", p.name)
        if not (p.published or hit) or excluded("packages", p.name):
            continue
        for x in p.exports:
            cid = st.caps.cap_id(x.path)
            if cid is None:
                continue
            node = _export_node(sg, g, x.path, x.name)
            e = ev("ast", x.path, x.line, f"Package {p.name} ({p.manifest}: {'declared in .pr-explorer.yml' if hit else p.reason}) "
                                          f"exports {x.name} from {x.path}:{x.line}", "sdk", p.name, x.name)
            evs = [e] + ([config_ev(hit)] if hit else [])
            put(Interface(f"iface:sdk:{p.name}:{x.name}", f"{p.name}.{x.name}", "outer", "sdk_export", cid,
                          "config" if hit else "static", [CodeRef(side, x.path, x.line)], evs,
                          meta={"contract": {"signature": _signature(sg.nodes.get(node)) if node else None},
                                "watch": [node] if node else []}))

    # ---- outer: outbound formats ----
    n_out = 0
    for path in sorted(sg.files):
        if n_out >= MAX_OUTBOUND or path in route_modules:
            continue
        hit = _config_hit(config, "outer", "paths", path)
        pat = next((x for x in OUTBOUND_PATTERNS if glob_match(x, path)), None)
        if not (hit or pat) or excluded("paths", path):
            continue
        cid = st.caps.cap_id(path)
        if cid is None or cid not in caps:
            continue
        mod = next((n for n in (sg.nodes.get(ids.module_id(lang_, path)) for lang_ in ("py", "ts")) if n is not None), None)
        evs = ([config_ev(hit)] if hit else []) + (mod.evidence_refs[:1] if mod is not None else [
            ev("framework_rule", path, 1, f"{path} matches outbound-format pattern {pat}", "outbound", pat)])
        syms = [c for c in g.children.get(mod.id, []) if not sg.nodes[c].label.startswith("_")] if mod is not None else []
        n_out += 1
        put(Interface(f"iface:outbound:{path}", path, "outer", "outbound_format", cid, "config" if hit else "static",
                      [CodeRef(side, path, 1)], evs,
                      description=desc(_t(lang, f"Data leaving the system is shaped here ({'declared in .pr-explorer.yml' if hit else 'path pattern ' + str(pat)})",
                                          f"送往系統外部的資料格式在這裡產生（{'.pr-explorer.yml 宣告' if hit else '路徑規則 ' + str(pat)}）"), evs),
                      meta={"contract": {"symbols": _symbols(sg, syms)}, "watch": []}))

    # ---- middle: module APIs (used by other capabilities / across an import-linter layer boundary) ----
    users: dict[str, set[str]] = {}  # module node id -> capabilities (or layer names) that use it
    used: dict[str, set[str]] = {}  # module node id -> symbol node ids used from outside
    edge_ev: dict[str, list[str]] = {}
    nodes = sg.nodes

    def module_of(nid: str) -> str:
        return nid.split("::", 1)[0]

    layer_pairs = _layer_boundaries(sg, contracts or [])
    for e in sorted(sg.edges.values(), key=lambda e: e.id):
        if e.kind not in ("imports", "calls") or e.confidence != "confirmed" or e.target not in nodes:
            continue
        sm, tm = module_of(e.source), module_of(e.target)
        if sm == tm or tm not in nodes or nodes[tm].level != "module":
            continue
        s, t = st.node_cap.get(e.source), st.node_cap.get(e.target)
        if not s or not t or s not in caps or t not in caps or "engineering" in (caps[s].category, caps[t].category):
            continue
        who = caps[s].technical_name if s != t else layer_pairs.get((sm, tm))
        if who is None:
            continue
        users.setdefault(tm, set()).add(who)
        if nodes[e.target].level == "symbol":
            used.setdefault(tm, set()).add(e.target)
        if len(edge_ev.setdefault(tm, [])) < 2:
            edge_ev[tm] += [x for x in e.evidence_refs[:1] if x not in edge_ev[tm]]
    ranked = sorted(users, key=lambda m: (-len(users[m]), -len(used.get(m, ())), m))[:MAX_MODULE_INTERFACES]
    for mid in ranked:
        n = nodes[mid]
        cid = st.node_cap.get(mid)
        path = n.code_refs[0].path
        syms = sorted(used.get(mid, ()))
        evs = n.evidence_refs[:1] + edge_ev.get(mid, [])
        names = [nodes[x].label for x in syms]
        who = sorted(users[mid])
        put(Interface(f"iface:module:{path}", n.meta.get("dotted") or path, "middle", "module_api", cid, "static",
                      [CodeRef(side, path, 1)], evs,
                      description=desc(_t(lang, f"Used by {', '.join(who[:5])}" + (f": {', '.join(names[:8])}" if names else ""),
                                          f"被 {'、'.join(who[:5])} 使用" + (f"：{'、'.join(names[:8])}" if names else "")), evs),
                      meta={"contract": {"symbols": _symbols(sg, syms)}, "symbol_ids": syms[:MAX_SYMBOLS], "watch": []}))

    # ---- middle: background tasks ----
    for epid, ep in sorted(st.entry_points.items()):
        fact = st.facts.get(epid)
        if fact is None or ep.kind != "task" or fact.handler_id not in nodes:
            continue
        put(Interface(f"iface:{epid}", fact.label.removeprefix("task "), "middle", "task", ep.capability_id, "static",
                      [fact.ref], [fact.evidence_id], entry_point_id=epid,
                      meta={"contract": {"signature": _signature(nodes[fact.handler_id])}, "watch": []}))
    return out


def _export_node(sg: SideGraph, g: _Graph, path: str, name: str) -> str | None:
    """Node of a package export: defined in the entry module, or re-exported from another module (import edge)."""
    lang = "py" if path.endswith(".py") else "ts"
    own = ids.symbol_id(lang, path, name)
    if own in sg.nodes:
        return own
    for t in g.imports.get(ids.module_id(lang, path), []):
        if t.endswith("::" + name) or t.endswith("." + name):
            return t
    return None


def _layer_boundaries(sg: SideGraph, contracts: list[ImportContract]) -> dict[tuple[str, str], str]:
    """(importer module id, imported module id) → '<contract>: <layer>' for imports from a higher to a lower layer of
    an import-linter layers contract (the lower layer's module is that layer's interface)."""
    dotted = {n.meta["dotted"]: n.id for n in sg.nodes.values()
              if n.level == "module" and n.id.startswith("node:py:") and n.meta.get("dotted")}
    out: dict[tuple[str, str], str] = {}
    layered = [c for c in contracts if c.kind == "layers"]
    if not layered:
        return out
    imps = [(e.source, e.target, a, b) for e in sg.edges.values() if e.kind == "imports"
            and (a := module_dotted(sg.nodes, e.source)) and (b := module_dotted(sg.nodes, e.target))]
    for c in layered:
        prefix = source_prefix(set(dotted), c.root_packages)
        for cont in [prefix + x for x in c.containers] or [prefix.rstrip(".")]:
            scope = [x for x in imps if not cont or (x[2].startswith(cont + ".") and x[3].startswith(cont + "."))]
            for src, tgt, a, b in scope:
                la, lb = _layer(c, cont, a), _layer(c, cont, b)
                if la is not None and lb is not None and la[0] < lb[0]:  # importer's layer uses the lower layer
                    out[(src.split("::", 1)[0], tgt.split("::", 1)[0])] = f"{la[1]} ({c.name})"
    return out


def _layer(c: ImportContract, container: str, mod: str) -> tuple[int, str] | None:
    for i, layer in enumerate(c.layers):
        for name in layer:
            pkg = f"{container}.{name}" if container else name
            if mod == pkg or mod.startswith(pkg + "."):
                return i, name
    return None


# ---------------------------------------------------------------- diff (PR overlay)

def sig_change(b: dict | None, h: dict | None, lang: str) -> tuple[str, str] | None:
    """(compatibility, detail) for a callable whose signature changed; None when unchanged."""
    if b == h:
        return None
    if b is None or h is None:
        return "unknown", _t(lang, "signature not resolvable on one side", "參數無法在其中一個版本靜態判定")
    req = lambda s: set(s["positional"][: s.get("required_positional", 0)]) | set(s.get("required_kwonly", []))  # noqa: E731
    params = lambda s: set(s["positional"]) | set(s.get("kwonly", []))  # noqa: E731
    new_req = sorted(req(h) - req(b))
    gone = sorted(params(b) - params(h)) if not h.get("kwarg") else []
    if new_req:
        return "breaking", _t(lang, f"new required parameter(s) {', '.join(new_req)}", f"新增必填參數 {'、'.join(new_req)}")
    if gone:
        return "breaking", _t(lang, f"parameter(s) {', '.join(gone)} removed", f"移除參數 {'、'.join(gone)}")
    common = [p for p in b["positional"] if p in h["positional"]]
    if common != [p for p in h["positional"] if p in b["positional"]] or (b.get("vararg") and not h.get("vararg")):
        return "unknown", _t(lang, "parameters reordered or *args changed", "參數順序或 *args 改變")
    added = sorted(params(h) - params(b))
    if added:
        return "compatible", _t(lang, f"new optional parameter(s) {', '.join(added)}", f"新增選填參數 {'、'.join(added)}")
    return "unknown", _t(lang, "signature changed", "參數定義改變")


def field_changes(b: dict[str, dict], h: dict[str, dict], lang: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    gone, new = sorted(set(b) - set(h)), sorted(set(h) - set(b))
    if len(gone) == 1 and len(new) == 1:
        out.append(("breaking", _t(lang, f"field {gone[0]} renamed to {new[0]} (clients reading {gone[0]} break)",
                                   f"欄位 {gone[0]} 改名為 {new[0]}（讀取 {gone[0]} 的客戶端會出錯）")))
        new = []
    else:
        for f in gone:
            out.append(("breaking", _t(lang, f"field {f} removed", f"移除欄位 {f}")))
    for f in new:
        o = h[f]
        if o["read_only"] or o["required"] is False:
            out.append(("compatible", _t(lang, f"new {'response ' if o['read_only'] else 'optional '}field {f}",
                                         f"新增{'回應' if o['read_only'] else '選填'}欄位 {f}")))
        elif o["required"]:
            out.append(("breaking", _t(lang, f"new required field {f}", f"新增必填欄位 {f}")))
        else:
            out.append(("unknown", _t(lang, f"new field {f} (whether it is required depends on the model)",
                                      f"新增欄位 {f}（是否必填取決於資料模型）")))
    for f in sorted(set(b) & set(h)):
        if b[f] == h[f]:
            continue
        need_b = not b[f]["read_only"] and b[f]["required"] is True
        need_h = not h[f]["read_only"] and h[f]["required"] is True
        if need_h and not need_b:
            out.append(("breaking", _t(lang, f"field {f} is now required", f"欄位 {f} 改為必填")))
        else:
            out.append(("unknown", _t(lang, f"field {f} options changed (read-only / required)", f"欄位 {f} 的唯讀 / 必填設定改變")))
    return out


def _compare(b: Interface, h: Interface | None, head_sg: SideGraph, changed_nodes: set[str], changed_paths: set[str],
             lang: str) -> tuple[str, str, str] | None:
    """(change, compatibility, detail) or None when the contract is unchanged."""
    bc = b.meta.get("contract", {})
    items: list[tuple[str, str]] = []
    if b.kind == "module_api":  # the base users' symbols, looked up on head (head may no longer list the module)
        mod = ids.module_id("py" if b.refs[0].path.endswith(".py") else "ts", b.refs[0].path)
        if mod not in head_sg.nodes:
            return "removed", "breaking", _t(lang, f"module {b.technical_name} removed", f"模組 {b.technical_name} 已移除")
        for sid in b.meta.get("symbol_ids", []):
            label = sid.split("::", 1)[-1]
            if sid not in head_sg.nodes:
                items.append(("breaking", _t(lang, f"{label} removed (other capabilities use it)", f"移除 {label}（其他能力仍在使用）")))
            elif (c := sig_change((bc.get("symbols") or {}).get(label), _signature(head_sg.nodes[sid]), lang)):
                items.append((c[0], f"{label}: {c[1]}"))
    elif h is None:
        return "removed", "breaking", _t(lang, f"{b.technical_name} removed", f"{b.technical_name} 已移除")
    else:
        hc = h.meta.get("contract", {})
        if b.kind == "http_api":  # the committed OpenAPI document first, then the Ninja signature, then the code
            bo, ho, bn, hn = bc.get("openapi"), hc.get("openapi"), bc.get("ninja"), hc.get("ninja")
            if bo is not None and ho is not None:
                items += openapi_changes(bo, ho, lang)
            if not items and bn is not None and hn is not None:
                items += op_changes(bn, hn, lang)
                if not items and bn["response"] is not None and hn["response"] is not None:
                    return None  # a declared, resolvable Ninja contract that did not change: the body is behaviour
            if not items and not (b.meta.get("handler") or h.meta.get("handler")):
                return None  # an operation known only from the OpenAPI document, which did not change
        if b.kind == "http_api" and not items:
            bm, hm = bc.get("methods"), hc.get("methods")
            if bm and hm:
                items += [("breaking", _t(lang, f"HTTP method {m} no longer accepted", f"不再接受 HTTP {m}")) for m in bm if m not in hm]
                items += [("compatible", _t(lang, f"HTTP method {m} added", f"新增 HTTP {m}")) for m in hm if m not in bm]
            elif bm != hm and (bm or hm):
                items.append(("unknown", _t(lang, "allowed HTTP methods not resolvable on one side", "可用的 HTTP 方法無法靜態判定")))
            bs, hs = bc.get("serializers"), hc.get("serializers")
            bf, hf = bc.get("file_columns"), hc.get("file_columns")
            handler_changed = any(w and (w in changed_nodes or any(x.startswith(w + ".") for x in changed_nodes))
                                  for w in h.meta.get("watch", []))
            if bf is not None and hf is not None:  # a file export: its column definitions are its outbound format
                items += export_changes(bf, hf, changed_paths, handler_changed, lang)
            if bs is not None and hs is not None:
                if set(bs) != set(hs):
                    items.append(("unknown", _t(lang, f"uses different serializers ({', '.join(sorted(hs)) or 'none'})",
                                                f"改用不同的 Serializer（{'、'.join(sorted(hs)) or '無'}）")))
                for k in sorted(set(bs) & set(hs)):
                    items += field_changes(bs[k], hs[k], lang)
            elif handler_changed and not (bf is not None and hf is not None):
                items.append(("unknown", _t(lang, "the handler changed and its request / response shape is not resolvable statically",
                                            "處理程式有改動，且請求 / 回應格式無法靜態判定")))
        elif b.kind in ("sdk_export", "task"):
            c = sig_change(bc.get("signature"), hc.get("signature"), lang)
            if c:
                items.append(c if b.kind == "sdk_export" or c[0] != "unknown" else ("breaking", c[1]))
        elif b.kind == "outbound_format" and b.refs[0].path in changed_paths:
            bs, hs = bc.get("symbols") or {}, hc.get("symbols") or {}
            parts = [_t(lang, f"{x} removed", f"移除 {x}") for x in sorted(set(bs) - set(hs))]
            parts += [_t(lang, f"{x} added", f"新增 {x}") for x in sorted(set(hs) - set(bs))]
            parts += [_t(lang, f"{x} changed", f"{x} 改變") for x in sorted(set(bs) & set(hs)) if bs[x] != hs[x]]
            items.append(("unknown", _t(lang, "content sent outside the system changed; compatibility cannot be decided statically",
                                        "送往外部的內容有改動，無法靜態判定是否相容") + (f"（{'；'.join(parts)}）" if parts and lang == "zh-Hant"
                                                                                    else f" ({'; '.join(parts)})" if parts else "")))
    if not items:
        return None
    worst = max(items, key=lambda x: RANK[x[0]])[0]
    return "modified", worst, ("；" if lang == "zh-Hant" else "; ").join(d for _, d in items)


def export_changes(b: dict[str, dict], h: dict[str, dict], changed_paths: set[str], handler_changed: bool,
                   lang: str) -> list[tuple[str, str]]:
    """Column keys of a file export, base vs head, over the definition files this PR changed (union per side, so moving
    a key between two methods is no change; a module merely newly imported by the handler is not a column change):
    added keys → compatible, removed / renamed → breaking, a changed definition without key changes → unknown naming
    the file."""
    total = len({k for d in h.values() for k in d["keys"]})
    b = {k: d for k, d in b.items() if k.split("::", 1)[0] in changed_paths}
    h = {k: d for k, d in h.items() if k.split("::", 1)[0] in changed_paths}
    bk = {k for d in b.values() for k in d["keys"]}
    hk = {k for d in h.values() for k in d["keys"]}
    added, removed = sorted(hk - bk), sorted(bk - hk)
    where = sorted({k.split("::", 1)[0] for k in {*b, *h}})
    at = ", ".join(where[:2]) + (f" +{len(where) - 2}" if len(where) > 2 else "")
    runtime = sorted(k.split("::", 1)[1] for k, d in h.items() if d["dynamic"] and not b.get(k, {}).get("dynamic"))
    out: list[tuple[str, str]] = []
    src = _t(lang, f" (defined in {at})", f"（定義於 {at}）") if at else ""
    if removed:
        out.append(("breaking", _t(lang, f"export file loses {len(removed)} column key(s): {', '.join(removed[:8])}",
                                   f"匯出檔少了 {len(removed)} 個欄位鍵：{'、'.join(removed[:8])}") + src
                    + _t(lang, " — clients reading them (spreadsheets, BI imports) break", "（讀取這些欄位的試算表、BI 匯入會出錯）")))
    if added:
        out.append(("compatible", _t(lang, f"export file adds {len(added)} column key(s): {', '.join(added[:8])}",
                                     f"匯出檔新增 {len(added)} 個欄位鍵：{'、'.join(added[:8])}") + src))
    if runtime:
        out.append(("compatible", _t(lang, f"plus columns named at runtime in {', '.join(runtime[:2])}",
                                     f"另有執行時才命名的欄位（{'、'.join(runtime[:2])}）")))
    if not out and where:
        out.append(("unknown", _t(lang, f"export column definitions in {at} changed but no column key was added or removed "
                                        f"({len(hk)} keys): values or formatting may differ",
                                  f"匯出欄位定義 {at} 有改動，但沒有新增或移除欄位鍵（共 {len(hk)} 個）：內容或格式可能不同")))
    elif not out and handler_changed:
        out.append(("unknown", _t(lang, f"the handler changed; its export columns are unchanged ({total} keys) but the values "
                                        "written may differ", f"處理程式有改動；匯出欄位不變（{total} 個鍵），但寫入的內容可能不同")))
    return out


def _norm_route(route: str) -> str:
    """/api/items/{id}/, /api/items/<int:pk>/ and /api/items/{int:pk} → /api/items/{}"""
    r = re.sub(r"\(\?P<[^>]+>[^)]*\)|\{[^}]*\}|<[^>]*>", "{}", route.split("?", 1)[0])
    return "/" + r.strip("/")


def _where(lang: str, w: str) -> str:
    return _t(lang, f"{w} parameter", {"query": "查詢參數", "path": "路徑參數", "header": "標頭參數", "cookie": "Cookie 參數"}.get(w, "參數"))


def op_changes(b: dict, h: dict, lang: str) -> list[tuple[str, str]]:
    """One API operation, base vs head (OpenAPI operation shape: params / request / response). Rules in the module doc."""
    out: list[tuple[str, str]] = []
    bp, hp = b.get("params") or {}, h.get("params") or {}
    for n in sorted(set(bp) - set(hp)):
        out.append(("breaking", _t(lang, f"{_where(lang, bp[n]['in'])} {n} removed", f"移除{_where(lang, bp[n]['in'])} {n}")))
    for n in sorted(set(hp) - set(bp)):
        o = hp[n]
        if o["required"]:
            out.append(("breaking", _t(lang, f"new required {_where(lang, o['in'])} {n}", f"新增必填{_where(lang, o['in'])} {n}")))
        elif o["required"] is None:
            out.append(("unknown", _t(lang, f"new {_where(lang, o['in'])} {n} (required or not is unknown)",
                                      f"新增{_where(lang, o['in'])} {n}（無法判定是否必填）")))
        else:
            out.append(("compatible", _t(lang, f"new optional {_where(lang, o['in'])} {n}", f"新增選填{_where(lang, o['in'])} {n}")))
    for n in sorted(set(bp) & set(hp)):
        x, y = bp[n], hp[n]
        if x["in"] != y["in"]:
            out.append(("breaking", _t(lang, f"parameter {n} moved from {x['in']} to {y['in']}", f"參數 {n} 從 {x['in']} 移到 {y['in']}")))
        elif y["required"] and not x["required"]:
            out.append(("breaking", _t(lang, f"{_where(lang, y['in'])} {n} is now required", f"{_where(lang, y['in'])} {n} 改為必填")))
        elif x["required"] and y["required"] is False:
            out.append(("compatible", _t(lang, f"{_where(lang, y['in'])} {n} is now optional", f"{_where(lang, y['in'])} {n} 改為選填")))
        elif x.get("type") != y.get("type"):
            out.append(("unknown", _t(lang, f"type of {n} changed ({x.get('type')} → {y.get('type')})",
                                      f"參數 {n} 的型別改變（{x.get('type')} → {y.get('type')}）")))
    for part, zh, en in (("request", "請求", "request"), ("response", "回應", "response")):
        bf, hf = b.get(part), h.get(part)
        if bf == hf:
            continue
        if bf is None or hf is None:
            out.append(("unknown", _t(lang, f"{en} body not resolvable on one side", f"{zh}內容在其中一個版本無法靜態判定")))
            continue
        for f in sorted(set(bf) - set(hf)):
            out.append(("breaking", _t(lang, f"{en} field {f} removed", f"移除{zh}欄位 {f}")))
        for f in sorted(set(hf) - set(bf)):
            if part == "request" and hf[f]["required"]:
                out.append(("breaking", _t(lang, f"new required request field {f}", f"新增必填請求欄位 {f}")))
            else:
                out.append(("compatible", _t(lang, f"new {'optional ' if part == 'request' else ''}{en} field {f}",
                                             f"新增{'選填' if part == 'request' else ''}{zh}欄位 {f}")))
        for f in sorted(set(bf) & set(hf)):
            x, y = bf[f], hf[f]
            if part == "request" and y["required"] and not x["required"]:
                out.append(("breaking", _t(lang, f"request field {f} is now required", f"請求欄位 {f} 改為必填")))
            elif part == "request" and x["required"] and not y["required"]:
                out.append(("compatible", _t(lang, f"request field {f} is now optional", f"請求欄位 {f} 改為選填")))
            elif part == "response" and x["required"] and not y["required"]:
                out.append(("unknown", _t(lang, f"response field {f} may now be missing", f"回應欄位 {f} 可能不再出現")))
            elif x.get("type") != y.get("type"):
                out.append(("unknown", _t(lang, f"type of {en} field {f} changed ({x.get('type')} → {y.get('type')})",
                                          f"{zh}欄位 {f} 的型別改變（{x.get('type')} → {y.get('type')}）")))
    return out


def openapi_changes(b: dict[str, dict], h: dict[str, dict], lang: str) -> list[tuple[str, str]]:
    """{METHOD: operation} per side (an interface may carry several operations of one route)."""
    out: list[tuple[str, str]] = []
    for m in sorted(set(b) - set(h)):
        out.append(("breaking", _t(lang, f"operation {m} removed from the OpenAPI document", f"OpenAPI 文件移除了 {m} 操作")))
    for m in sorted(set(h) - set(b)):
        out.append(("compatible", _t(lang, f"operation {m} added", f"新增 {m} 操作")))
    for m in sorted(set(b) & set(h)):
        out += [(c, f"{m}: {d}") if len(b) > 1 else (c, d) for c, d in op_changes(b[m], h[m], lang)]
    return out


def _op_shape(op) -> dict:
    return {"params": op.params, "request": op.request, "response": op.response}


def _spec_cap(st: Structure, spec_path: str, route: str, matched: list[str]) -> str | None:
    """Capability of an operation no route describes: the URL segment naming a capability directory, else the
    capability holding the document, else the one most of the document's matched operations belong to, else the only
    non-engineering capability. None (operation not listed) otherwise."""
    by_dir: dict[str, str] = {}
    for c in st.capabilities.values():
        by_dir.setdefault(c.path.rsplit("/", 1)[-1].lower().replace("-", "_"), c.id)
    for seg in route.strip("/").split("/"):
        cid = by_dir.get(seg.lower().replace("-", "_"))
        if cid and not seg.startswith("{"):
            return cid
    cid = st.caps.cap_id(spec_path)
    if cid in st.capabilities:
        return cid
    if matched:
        return max(sorted(set(matched)), key=matched.count)
    product = [c.id for c in st.capabilities.values() if c.category != "engineering"]
    return product[0] if len(product) == 1 else None


def _api_specs(out: InterfaceSet, sg: SideGraph, g: _Graph, st: Structure, surface: RepoSurface, lang: str,
               ev: Callable, excluded: Callable, put: Callable) -> None:
    routes: dict[str, list[Interface]] = {}
    by_op_id: dict[str, Interface] = {}
    for i in out.interfaces.values():
        if i.kind != "http_api":
            continue
        routes.setdefault(_norm_route(i.meta.get("route") or i.technical_name), []).append(i)
        hn = sg.nodes.get(i.meta.get("handler") or "")
        dotted = hn.meta.get("dotted") if hn is not None else None
        if dotted:
            parts = dotted.split(".")
            for k in range(len(parts)):  # Ninja's default operationId = "<module>_<function>" with dots as "_"
                by_op_id.setdefault("_".join(parts[k:]), i)
    for spec in surface.api_specs:
        if excluded("paths", spec.path):
            continue
        matched: list[str] = []
        pending: list = []
        for op in spec.operations:
            if excluded("routes", op.route):
                continue
            cands = routes.get(_norm_route(op.route), [])
            hit = (next((i for i in cands if op.method in (i.meta["contract"].get("methods") or [])), None)
                   or next((i for i in cands if not i.meta["contract"].get("methods")), None)
                   or (by_op_id.get(op.operation_id) if op.operation_id else None))
            e = ev("doc", spec.path, op.line, f"OpenAPI document {spec.path}:{op.line} declares {op.method} {op.route}"
                   + (f" (operationId {op.operation_id})" if op.operation_id else ""), "openapi", op.method, op.route)
            ref = CodeRef(sg.side, spec.path, op.line)
            if hit is not None:
                hit.meta["contract"].setdefault("openapi", {})[op.method] = _op_shape(op)
                matched.append(hit.capability_id)
                if ref.path not in {r.path for r in hit.refs}:
                    hit.refs.insert(1, ref)
                    hit.evidence_refs.insert(1, e)
            else:
                pending.append((op, e, ref))
        for op, e, ref in pending:
            cid = _spec_cap(st, spec.path, op.route, matched)
            if cid is None:
                continue
            fields = sorted({*op.params, *(op.request or {})})
            text = (f"{op.method} {op.route}" + _t(lang, f" (OpenAPI document {spec.path}); parameters / request fields: ",
                                                   f"（OpenAPI 文件 {spec.path}）；參數 / 請求欄位：")
                    + (", ".join(fields[:20]) or _t(lang, "none", "無")))
            put(Interface(f"iface:openapi:{op.method} {op.route}", f"{op.method} {op.route}", "outer", "http_api", cid, "static",
                          [ref], [e], description=Text(text, "static", [e]),
                          meta={"contract": {"openapi": {op.method: _op_shape(op)}}, "watch": []}))


KIND_LABEL = {
    "http_api": ("API route", "API 路由"), "web_route": ("web page route", "網頁路由"), "sdk_export": ("package export", "套件匯出"),
    "outbound_format": ("outbound format", "對外格式"), "module_api": ("module API", "模組介面"), "task": ("background task", "背景工作"),
    "event": ("event", "事件"),
}
WHO = {
    "http_api": ("callers of this API (front end, partners, other services)", "呼叫這個 API 的前端、合作夥伴或其他服務"),
    "web_route": ("people using this page (bookmarks, links)", "使用這個頁面的使用者（書籤、連結）"),
    "sdk_export": ("programs that use this published package", "使用這個發佈套件的外部程式"),
    "outbound_format": ("external systems or recipients receiving this data", "接收這份資料的外部系統或收件人"),
    "module_api": ("other capabilities that use this module", "使用這個模組的其他能力"),
    "task": ("code that queues this background job (including jobs already queued)", "排入這個背景工作的程式（含已在佇列中的工作）"),
}


def diff_interfaces(base: dict[str, Interface], head: dict[str, Interface], *, head_sg: SideGraph, known: set[str],
                    changed_nodes: set[str], changed_paths: set[str], iface_ref: Callable[[Interface], str | None],
                    cap_ref: Callable[[str | None], str | None], diff_ev: dict[str, str], lang: str = "en") -> list[ArchCheck]:
    """One interface_change check per interface whose contract changed. `known`: interface ids of the system map
    (modified / removed ones must be listed there); `iface_ref` registers a head-only interface in head_additions."""
    out: list[ArchCheck] = []
    for iid in sorted(set(base) | set(head)):
        b, h = base.get(iid), head.get(iid)
        if b is None:
            assert h is not None
            if (ref := iface_ref(h)) is None:
                continue
            change, compat = "added", "compatible"
            detail = _t(lang, f"new {KIND_LABEL[h.kind][0]} {h.technical_name}", f"新增{KIND_LABEL[h.kind][1]} {h.technical_name}")
            cur = h
        else:
            if iid not in known:
                continue
            got = _compare(b, h, head_sg, changed_nodes, changed_paths, lang)
            if got is None:
                continue
            change, compat, detail = got
            ref, cur = iid, (h if h is not None and change != "removed" else b)
        out.append(_check(cur, ref, change, compat, detail, cap_ref, diff_ev, lang))
    out.sort(key=check_order)
    return out


def _check(i: Interface, ref: str, change: str, compat: str, detail: str, cap_ref, diff_ev: dict[str, str], lang: str) -> ArchCheck:
    outer = i.ring == "outer"
    name = i.technical_name
    title = {
        "breaking": _t(lang, f"May break {'clients' if outer else 'other capabilities'}: {name}",
                       f"可能破壞{'客戶端' if outer else '其他能力'}：{name}"),
        "unknown": _t(lang, f"Interface change, compatibility unknown: {name}", f"介面變動，無法判定相容性：{name}"),
        "compatible": _t(lang, f"Compatible interface {'addition' if change == 'added' else 'change'}: {name}",
                         f"相容的介面{'新增' if change == 'added' else '變動'}：{name}"),
    }[compat]
    ring_txt = _t(lang, "outer ring (used directly by clients)" if outer else "middle ring (between capabilities)",
                  "外圈（客戶端直接使用）" if outer else "中圈（能力之間）")
    ask = ""
    if outer and change != "added":
        ask = _t(lang, " To confirm: is this change necessary, could it be made further inside, does it need versioning "
                       "or a notice to clients?", "需要確認：這個改動是否必要、能否改在更內層、是否需要版本控管或通知客戶端。")
    desc = _t(lang, f"{ring_txt} {KIND_LABEL[i.kind][0]} {name}: {detail}.{ask}", f"{ring_txt}{KIND_LABEL[i.kind][1]} {name}：{detail}。{ask}")
    paths = list(dict.fromkeys(r.path for r in i.refs))
    evs = list(dict.fromkeys([*i.evidence_refs, *(diff_ev[p] for p in paths if p in diff_ev)]))[:8]
    return ArchCheck(
        id=f"arch:iface:{ids.short_hash(i.id, length=12)}", origin="observed", source="static", check="interface_change",
        ring=i.ring, verdict=VERDICT[compat], status="needs_confirmation" if compat == "unknown" else "verified",
        title=title[:200], description=desc, capability_ids=[c for c in [cap_ref(i.capability_id)] if c],
        pr_refs=[CodeRef(r.side, r.path, r.start_line, r.end_line) for r in i.refs[:5]], existing_refs=[], evidence_refs=evs,
        business_impact=_t(lang, *WHO[i.kind]),
        interface_change=InterfaceChange(ref, change, compat, detail[:1500]))


__all__ = ["API_PREFIXES", "API_SEGMENT", "InterfaceSet", "MAX_MODULE_INTERFACES", "OUTBOUND_PATTERNS", "detect_interfaces",
           "diff_interfaces", "field_changes", "glob_match", "is_api_route", "op_changes", "openapi_changes", "sig_change"]
