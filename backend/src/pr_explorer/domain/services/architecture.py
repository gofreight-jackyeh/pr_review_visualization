"""Architecture consistency (spec v2 §8) — software architecture, not code style. Pure.

Rules (system map, merge-base commit):
  documented  import-linter contracts (layers / forbidden / independence; custom types read as their shape).
              Checked on direct, top-level imports between Python modules (import-linter also follows indirect chains
              and function-level imports: those are not seen here, so a missing check is possible, a false one is not).
  inferred    repository-wide statistics, each stated in `statistic`:
              interface_only   calls into a capability from other capabilities go through a few of its modules
              layers           views of a capability do not query the ORM directly (they use the service layer)
              shared_mechanism background work runs on the repository's task framework, not on raw threads/processes
              acyclic          no dependency cycles between business capabilities
Checks (PR overlay): the PR's added import / call / ORM edges (head vs base) against those rules (violates, verified),
new import-linter ignore_imports entries (new_debt, verified), new capability cycles (cycle, verified). Rules are
recomputed from the base graph for every PR (deterministic IDs), so a cached map and its overlays always agree.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from ..model import ids
from ..model.convention import ImportContract
from ..model.document import ArchCheck, ArchRule, ViolatingRelation
from ..model.graph import CodeRef, Edge, Evidence, Graph, Node, Producer, SideGraph
from .system_structure import Structure

PRODUCER = Producer("architecture", "1")
# inferred-rule thresholds (spec §8: inferred rules must state their statistic)
INTERFACE_MIN_CALLS = 3  # cross-capability calls into a capability needed before its interface is inferred
INTERFACE_SHARE = 0.9  # ... that go through at most INTERFACE_MAX_MODULES of its modules
INTERFACE_MAX_MODULES = 3
MAX_INTERFACE_RULES = 40  # the busiest capabilities only (bounded map size on large repositories)
LAYER_MIN_VIEWS = 3  # views in a capability needed before its view → service → model layering is inferred
LAYER_SHARE = 0.8  # ... share of them that never query the ORM directly
TASK_MIN = 3  # background tasks needed before "use the task framework" is inferred
BYPASS = {"threading", "multiprocessing", "concurrent", "_thread", "sched", "subprocess"}  # raw background mechanisms
MAX_REFS = 5
MAX_EXAMPLES = 3  # existing_refs: 1–3 examples of the established way
DOC_KINDS = {"layers": "layers", "forbidden": "forbidden", "independence": "independence"}


def _t(lang: str, en: str, zh: str) -> str:
    return zh if lang == "zh-Hant" else en


def _under(module: str, pkg: str) -> bool:
    return module == pkg or module.startswith(pkg + ".")


def _match(pattern: str, name: str) -> bool:
    """import-linter wildcards: `*` = exactly one segment, `**` = any number of segments."""
    p, n = pattern.split("."), name.split(".")

    def go(i: int, j: int) -> bool:
        if i == len(p):
            return j == len(n)
        if p[i] == "**":
            return any(go(i + 1, k) for k in range(j, len(n) + 1))
        return j < len(n) and (p[i] == "*" or p[i] == n[j]) and go(i + 1, j + 1)

    return go(0, 0)


def ignored_by(entries: list[str], imp: str, imd: str, prefix: str) -> str | None:
    for e in entries:
        a, sep, b = e.partition("->")
        if sep and _match(prefix + a.strip(), imp) and _match(prefix + b.strip(), imd):
            return e
    return None


def violation(c: ImportContract, imp: str, imd: str, prefix: str = "") -> bool:
    """Does the direct import imp → imd break contract c (ignoring its ignore_imports)?"""
    P = lambda m: prefix + m  # noqa: E731
    if c.kind == "forbidden":
        return any(_under(imp, P(s)) for s in c.source_modules) and any(_under(imd, P(f)) for f in c.forbidden_modules)
    if c.kind == "independence":
        a = next((m for m in c.modules if _under(imp, P(m))), None)
        b = next((m for m in c.modules if _under(imd, P(m))), None)
        return a is not None and b is not None and a != b
    if c.kind == "layers":
        for cont in [P(x) for x in c.containers] or [None]:
            def layer_of(m: str) -> tuple[int, int] | None:
                for i, layer in enumerate(c.layers):
                    for j, name in enumerate(layer):
                        if _under(m, f"{cont}.{name}" if cont else P(name)):
                            return i, j
                return None
            a, b = layer_of(imp), layer_of(imd)
            if a is None or b is None:
                continue
            if b[0] < a[0]:
                return True  # a lower layer imports a higher one
            indep = c.layer_independent[a[0]] if a[0] < len(c.layer_independent) else False
            if a[0] == b[0] and a[1] != b[1] and indep:
                return True
        return False
    return False


def governs(c: ImportContract, imp: str, prefix: str = "") -> bool:
    mods = (c.source_modules if c.kind == "forbidden" else c.modules if c.kind == "independence" else
            [f"{cont}.{n}" for cont in (c.containers or [""]) for layer in c.layers for n in layer])
    return any(_under(imp, prefix + m.lstrip(".")) for m in mods)


# ---------------------------------------------------------------- graph helpers

def module_dotted(nodes: dict[str, Node], nid: str) -> str | None:
    n = nodes.get(nid.split("::", 1)[0])
    return n.meta.get("dotted") if n is not None and n.id.startswith("node:py:") and n.level == "module" else None


def source_prefix(dotted: set[str], roots: list[str]) -> str:
    """'' when the contract's root packages are repo-relative, else the leading source dirs (e.g. 'src.')."""
    for r in roots:
        if r in dotted or any(d.startswith(r + ".") for d in dotted):
            return ""
    for r in roots:
        for d in sorted(dotted):
            k = d.find("." + r + ".")
            if k < 0 and d.endswith("." + r):
                k = len(d) - len(r) - 1
            if k >= 0:
                return d[: k + 1]
    return ""


def _first_ref(ev: dict[str, Evidence], e: Edge, side: str) -> CodeRef | None:
    for eid in e.evidence_refs:
        x = ev.get(eid)
        for r in (x.refs if x else []):
            if r.side == side:
                return CodeRef(r.side, r.path, r.start_line, r.end_line)
    return None


def _owner(nodes: dict[str, Node], nid: str) -> str:
    n = nodes[nid]
    p = n.parent_id
    return p if n.kind == "method" and p and p in nodes and nodes[p].kind == "view" else nid


def _sccs(nodes: set[str], edges: set[tuple[str, str]]) -> list[set[str]]:
    """Strongly connected components with more than one member (iterative Tarjan)."""
    adj: dict[str, list[str]] = {}
    for a, b in sorted(edges):
        adj.setdefault(a, []).append(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[set[str]] = []
    counter = 0
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on.add(v)
            succ = adj.get(v, [])
            if i < len(succ):
                work.append((v, i + 1))
                w = succ[i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            for w in succ:
                if w in on:
                    low[v] = min(low[v], low[w])
            if low[v] == index[v]:
                comp = set()
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.add(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(comp)
    return out


def _cap_edges(st: Structure, business: set[str]) -> set[tuple[str, str]]:
    return {(r.source, r.target) for r in st.relations.values()
            if r.kind != "uses_entity" and r.source in business and r.target in business}


# ---------------------------------------------------------------- rules (system map)

@dataclass
class RuleSet:
    rules: list[ArchRule] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _caps_for_module(st: Structure, path: str) -> list[str]:
    inside = sorted(c.id for c in st.capabilities.values() if c.path == path or c.path.startswith(path + "/"))
    if inside:
        return inside
    for probe in (path + "/__init__.py", path + ".py"):
        cid = st.caps.cap_id(probe)
        if cid in st.capabilities:
            return [cid]
    return []


def _ev(rs: RuleSet, eid: str, kind: str, desc: str, refs: list[CodeRef]) -> str:
    rs.evidence.append(Evidence(eid, kind, desc[:500], refs[:6], PRODUCER))
    return eid


def infer_rules(sg: SideGraph, st: Structure, contracts: list[ImportContract], lang: str = "en") -> RuleSet:
    rs = RuleSet()
    nodes, side = sg.nodes, sg.side
    caps = st.capabilities
    dotted = {n.meta["dotted"] for n in nodes.values()
              if n.level == "module" and n.id.startswith("node:py:") and n.meta.get("dotted")}
    imports = [e for e in sorted(sg.edges.values(), key=lambda e: e.id) if e.kind == "imports"]

    # ---- documented: import-linter contracts
    for c in contracts:
        prefix = source_prefix(dotted, c.root_packages)
        rid = f"rule:importlinter:{c.path}:{c.key}"
        mods = c.source_modules + c.forbidden_modules + c.modules + \
            [f"{cont}.{n}" if cont else n for cont in (c.containers or [""]) for layer in c.layers for n in layer]
        scope = sorted({cid for m in mods for cid in _caps_for_module(st, (prefix + m).replace(".", "/"))})
        governed = broken = 0
        for e in imports:
            imp, imd = module_dotted(nodes, e.source), module_dotted(nodes, e.target)
            if imp and imd and imp != imd and governs(c, imp, prefix):
                governed += 1
                broken += violation(c, imp, imd, prefix) and not ignored_by(c.ignore_imports, imp, imd, prefix)
        what = {
            "forbidden": _t(lang, f"{', '.join(c.source_modules)} must not import {', '.join(c.forbidden_modules)}",
                            f"{'、'.join(c.source_modules)} 不得依賴 {'、'.join(c.forbidden_modules)}"),
            "independence": _t(lang, f"{', '.join(c.modules)} must not import each other",
                               f"{'、'.join(c.modules)} 彼此獨立，不得互相依賴"),
            "layers": _t(lang, "layers, top to bottom: " + " → ".join(" | ".join(x) for x in c.layers)
                         + "; a lower layer must not import a higher one",
                         "分層（由上到下）：" + " → ".join(" | ".join(x) for x in c.layers) + "；下層不得依賴上層")
                      + (_t(lang, f" (inside {', '.join(c.containers)})", f"（適用於 {'、'.join(c.containers)}）")
                         if c.containers else ""),
        }[c.kind]
        custom = "" if c.declared_type == c.kind else _t(lang, f" Custom contract type '{c.declared_type}', read as {c.kind}.",
                                                          f" 自訂契約類型 {c.declared_type}，以 {c.kind} 規則檢查。")
        desc = _t(lang,
                  f"import-linter contract '{c.name}': {what}.{custom} Checked statically on direct imports; at the base "
                  f"commit {governed} import(s) fall under it, {len(c.ignore_imports)} recorded exception(s), "
                  f"{broken} unrecorded violation(s).",
                  f"import-linter 契約「{c.name}」：{what}。{custom}以靜態分析檢查直接 import；基準版本有 {governed} 個 import "
                  f"受此規則約束，已登記例外 {len(c.ignore_imports)} 條，未登記的違反 {broken} 個。")
        ref = CodeRef(side, c.path, c.line)
        ev = _ev(rs, ids.evidence_id("doc", side, c.path, c.line, "importlinter", c.key), "doc",
                 f"import-linter contract '{c.name}' (type {c.declared_type}) at {c.path}:{c.line}", [ref])
        rs.rules.append(ArchRule(rid, "documented", DOC_KINDS[c.kind], c.name, desc, scope, [ev], source_ref=ref,
                                 exception_count=len(c.ignore_imports), meta={"contract": c, "prefix": prefix}))

    # ---- inferred: interface_only
    incoming: dict[str, list[Edge]] = {}
    for e in sorted(sg.edges.values(), key=lambda e: e.id):
        if e.kind != "calls" or e.confidence != "confirmed":
            continue
        s, t = st.node_cap.get(e.source), st.node_cap.get(e.target)
        if s and t and s != t and caps[s].category != "engineering" and caps[t].category != "engineering":
            incoming.setdefault(t, []).append(e)
    module_count = Counter(st.node_cap[n.id] for n in nodes.values() if n.level == "module" and n.id in st.node_cap)
    ranked = sorted(incoming, key=lambda t: (-len(incoming[t]), t))[:MAX_INTERFACE_RULES]
    for t in ranked:
        edges = incoming[t]
        if len(edges) < INTERFACE_MIN_CALLS:
            continue
        by_mod = Counter(e.target.split("::", 1)[0] for e in edges)
        chosen, covered = [], 0
        for mid, n in sorted(by_mod.items(), key=lambda kv: (-kv[1], kv[0])):
            if covered / len(edges) >= INTERFACE_SHARE or len(chosen) >= INTERFACE_MAX_MODULES:
                break
            chosen.append(mid)
            covered += n
        if covered / len(edges) < INTERFACE_SHARE or module_count.get(t, 0) <= len(chosen):
            continue
        names = [nodes[m].meta.get("dotted") or nodes[m].label for m in chosen if m in nodes]
        cname = caps[t].technical_name
        stat = _t(lang, f"{covered} of {len(edges)} calls into {cname} from other capabilities go through {', '.join(names)}",
                  f"其他能力呼叫 {cname} 的 {len(edges)} 次中，有 {covered} 次透過 {'、'.join(names)}")
        good = [r for e in edges if e.target.split("::", 1)[0] in chosen and (r := _first_ref(sg.evidence, e, side))]
        ev = _ev(rs, f"ev:arch:{side}:interface:{t}", "ast", stat, good)
        rs.rules.append(ArchRule(
            f"rule:inferred:interface:{t}", "inferred", "interface_only",
            _t(lang, f"Other capabilities use {cname} through {', '.join(names)}", f"其他能力透過 {'、'.join(names)} 使用 {cname}"),
            _t(lang, f"Inferred from the call graph: {stat}. New calls from other capabilities should use the same entry modules.",
               f"由呼叫關係歸納：{stat}。其他能力的新呼叫應透過同樣的對外模組。"),
            [t], [ev], statistic=stat, meta={"cap": t, "modules": set(chosen), "examples": good[:MAX_REFS]}))

    # ---- inferred: layers (views do not query the ORM directly)
    orm_views: set[str] = set()
    for e in sg.edges.values():
        if e.kind == "uses_model" and e.confidence == "confirmed" and e.source in nodes and nodes[e.source].kind in ("view", "method"):
            o = _owner(nodes, e.source)
            if nodes[o].kind == "view":
                orm_views.add(o)
    views: dict[str, list[Node]] = {}
    for n in sorted(nodes.values(), key=lambda n: n.id):
        if n.kind == "view" and n.id in st.node_cap:
            views.setdefault(st.node_cap[n.id], []).append(n)
    for cid, vs in sorted(views.items()):
        clean = [v for v in vs if v.id not in orm_views]
        if len(vs) < LAYER_MIN_VIEWS or len(clean) / len(vs) < LAYER_SHARE or caps[cid].category == "engineering":
            continue
        cname = caps[cid].technical_name
        stat = _t(lang, f"{len(clean)} of {len(vs)} views in {cname} never query the database directly",
                  f"{cname} 的 {len(vs)} 個畫面中，有 {len(clean)} 個不直接查詢資料庫")
        good = [CodeRef(r.side, r.path, r.start_line, r.end_line) for v in clean for r in v.code_refs[:1] if r.side == side]
        ev = _ev(rs, f"ev:arch:{side}:layers:{cid}", "ast", stat, good)
        rs.rules.append(ArchRule(
            f"rule:inferred:layers:{cid}", "inferred", "layers",
            _t(lang, f"Views in {cname} go through the service layer", f"{cname} 的畫面透過服務層存取資料"),
            _t(lang, f"Inferred layering view → service → model: {stat}.", f"由既有程式歸納出「畫面 → 服務 → 資料」分層：{stat}。"),
            [cid], [ev], statistic=stat, meta={"cap": cid, "examples": good[:MAX_REFS]}))

    # ---- inferred: shared_mechanism (background work on the task framework)
    tasks = [f for f in sg.entry_points if f.kind == "task"]
    if len(tasks) >= TASK_MIN:
        raw = sorted(n.id for n in nodes.values() if n.level == "module" and n.id in st.node_cap
                     and caps[st.node_cap[n.id]].category != "engineering"
                     and any(t.split(".")[0] in BYPASS for t in (n.meta.get("ext_imports") or {})))
        celery = any(t.split(".")[0] == "celery" for n in nodes.values() for t in (n.meta.get("ext_imports") or {}))
        fw = "Celery" if celery else _t(lang, "the task framework", "既有的背景工作框架")
        stat = _t(lang, f"{len(tasks)} background jobs are {fw} tasks; {len(raw)} module(s) start threads or processes directly",
                  f"{len(tasks)} 個背景工作都是 {fw} task；直接啟動執行緒或子程序的模組有 {len(raw)} 個")
        good = [CodeRef(f.ref.side, f.ref.path, f.ref.start_line) for f in tasks[:MAX_REFS]]
        ev = _ev(rs, f"ev:arch:{side}:shared_mechanism:tasks", "ast", stat, good)
        scope = sorted({c for f in tasks if (c := st.node_cap.get(f.handler_id or "") or st.caps.cap_id(f.ref.path))
                        and c in caps})
        rs.rules.append(ArchRule(
            "rule:inferred:shared_mechanism:tasks", "inferred", "shared_mechanism",
            _t(lang, f"Background work runs as {fw} tasks", f"背景工作使用 {fw}"),
            _t(lang, f"Inferred: {stat}. New background work should be a task, not a raw thread or process.",
               f"由既有程式歸納：{stat}。新的背景工作應寫成 task，而不是自行開執行緒或子程序。"),
            scope, [ev], statistic=stat, meta={"examples": good}))

    # ---- inferred: acyclic between business capabilities
    business = {c.id for c in caps.values() if c.category == "business"}
    deps = _cap_edges(st, business)
    if len(business) >= 3 and deps and not _sccs(business, deps):
        stat = _t(lang, f"0 dependency cycles among {len(business)} business capabilities ({len(deps)} dependencies)",
                  f"{len(business)} 個業務能力之間的 {len(deps)} 條依賴中，沒有任何循環")
        heavy = sorted((r for r in st.relations.values() if (r.source, r.target) in deps), key=lambda r: (-r.weight, r.id))
        refs = [r0 for r in heavy[:6] for eid in r.evidence_refs[:1] for r0 in (sg.evidence[eid].refs[:1] if eid in sg.evidence else [])]
        good = [CodeRef(r.side, r.path, r.start_line, r.end_line) for r in refs]
        ev = _ev(rs, f"ev:arch:{side}:acyclic", "ast", stat, good)
        rs.rules.append(ArchRule(
            "rule:inferred:acyclic", "inferred", "acyclic",
            _t(lang, "Business capabilities have no dependency cycles", "業務能力之間沒有循環依賴"),
            _t(lang, f"Inferred from the capability dependency graph: {stat}.", f"由能力之間的依賴歸納：{stat}。"),
            sorted(business), [ev], statistic=stat, meta={"business": business, "examples": good[:MAX_EXAMPLES]}))
    return rs


# ---------------------------------------------------------------- checks (PR overlay)

@dataclass
class CheckSet:
    checks: list[ArchCheck] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


def check_pr(graph: Graph, base_st: Structure, head_st: Structure, rules: list[ArchRule],
             head_contracts: list[ImportContract], *, cap_ref: Callable[[str | None], str | None],
             name: Callable[[str], str], lang: str = "en") -> CheckSet:
    """Architecture checks for one PR. `rules` must come from `infer_rules` on the PR's base (the map's rules);
    `cap_ref` maps a capability id to one the overlay may cite (map or head_additions), or None."""
    out = CheckSet()
    nodes, ev = graph.nodes, graph.evidence

    def cap(nid: str) -> str | None:
        return cap_ref(head_st.node_cap.get(nid) or base_st.node_cap.get(nid))

    added = [e for e in sorted(graph.edges.values(), key=lambda e: e.id) if e.change_status == "added" and e.confidence == "confirmed"]
    added_imports = [(e, module_dotted(nodes, e.source), module_dotted(nodes, e.target)) for e in added if e.kind == "imports"]
    added_imports = [(e, a, b) for e, a, b in added_imports if a and b and a != b]
    by_key = {(c.path, c.key): c for c in head_contracts}

    def put(rule: ArchRule, kind: str, verdict: str, title: str, desc: str, edges: list[Edge], caps: list[str],
            rel: tuple[str, str] | None, existing: list[CodeRef], disc: str, extra_ev: list[str] = (), ring: str = "middle") -> None:
        pr_refs = list(dict.fromkeys(r for e in edges if (r := _first_ref(ev, e, "head"))))[:MAX_REFS]
        evs = list(dict.fromkeys([*extra_ev, *(x for e in edges for x in e.evidence_refs[:1]), *rule.evidence_refs]))[:8]
        out.checks.append(ArchCheck(
            id=f"arch:{rule.id.removeprefix('rule:')}:{ids.short_hash(disc, length=10)}", origin=rule.origin, source="static",
            check=kind, ring=ring, verdict=verdict, status="verified", title=title, description=desc,
            capability_ids=[c for c in dict.fromkeys(caps) if c], pr_refs=pr_refs, existing_refs=existing[:MAX_EXAMPLES],
            evidence_refs=evs, rule_id=rule.id,
            violating_relation=ViolatingRelation(*rel) if rel and rel[0] and rel[1] and rel[0] != rel[1] else None))

    for rule in rules:
        m = rule.meta
        if rule.origin == "documented":
            c: ImportContract = m["contract"]
            prefix = m["prefix"]
            head_c = by_key.get((c.path, c.key))
            new_entries = [x for x in (head_c.ignore_imports if head_c else []) if x not in c.ignore_imports]
            groups: dict[tuple, list[Edge]] = {}
            governed = 0
            for e, imp, imd in added_imports:
                if not governs(c, imp, prefix):
                    continue
                governed += 1
                if not violation(c, imp, imd, prefix) or ignored_by(c.ignore_imports, imp, imd, prefix):
                    continue
                if ignored_by(new_entries, imp, imd, prefix):
                    continue  # excused by an exception this PR adds: reported as new_debt below
                groups.setdefault((cap(e.source), cap(e.target), imp, imd), []).append(e)
            for (s, t, imp, imd), edges in sorted(groups.items(), key=lambda kv: kv[0][2:]):
                put(rule, "dependency_direction", "violates",
                    _t(lang, f"Breaks architecture rule: {rule.title}", f"違反架構規則：{rule.title}"),
                    _t(lang, f"This PR adds the import {imp} → {imd}, which import-linter contract '{c.name}' "
                             f"({c.path}:{c.line}) forbids.",
                       f"這個 PR 新增 {imp} → {imd} 的 import，違反 import-linter 契約「{c.name}」（{c.path}:{c.line}）。"),
                    edges, [s, t], (s, t), [], f"{imp}->{imd}", ring=_ring(s, t))
            if governed and not groups and not new_entries:
                put(rule, "dependency_direction", "follows",
                    _t(lang, f"Follows architecture rule: {rule.title}", f"符合架構規則：{rule.title}"),
                    _t(lang, f"The {governed} import(s) this PR adds under contract '{c.name}' all respect it.",
                       f"這個 PR 在契約「{c.name}」範圍內新增的 {governed} 個 import 都符合規則。"),
                    [e for e, imp, _ in added_imports if governs(c, imp, prefix)], list(rule.scope_capability_ids),
                    None, [], "follows", ring="inner" if c.kind == "layers" and c.containers else "middle")
            for entry in new_entries:
                line = head_c.ignore_lines.get(entry, head_c.line)
                ref = CodeRef("head", head_c.path, line)
                eid = ids.evidence_id("diff", "head", head_c.path, line, "ignore_imports", entry)
                out.evidence.append(Evidence(eid, "diff", f"{head_c.path}:{line} adds ignore_imports entry '{entry}'", [ref], PRODUCER))
                a, _, b = entry.partition("->")
                caps = [cid for x in (a, b) for cid in _caps_for_module(head_st, (prefix + x.strip().replace("*", "")).rstrip(".").replace(".", "/"))]
                debt = ArchCheck(
                    id=f"arch:{rule.id.removeprefix('rule:')}:debt:{ids.short_hash(entry, length=10)}", origin="documented",
                    source="static", check="new_debt", ring="inner" if len(set(caps)) == 1 else "middle",
                    verdict="new_debt", status="verified",
                    title=_t(lang, f"New architecture debt: an exception to '{c.name}'", f"新增架構債：契約「{c.name}」多了一條例外"),
                    description=_t(lang, f"This PR adds `{entry}` to the ignore_imports of contract '{c.name}': the dependency "
                                         f"is excused instead of fixed ({len(c.ignore_imports)} exception(s) before).",
                                   f"這個 PR 在契約「{c.name}」的 ignore_imports 新增 `{entry}`：這個依賴被列為例外，而不是被修正"
                                   f"（原本已有 {len(c.ignore_imports)} 條例外）。"),
                    capability_ids=[x for x in dict.fromkeys(cap_ref(cid) for cid in caps) if x], pr_refs=[ref],
                    existing_refs=[], evidence_refs=[eid, *rule.evidence_refs], rule_id=rule.id)
                out.checks.append(debt)

        elif rule.kind == "interface_only":
            groups: dict[tuple, list[Edge]] = {}
            for e in added:
                if e.kind != "calls" or e.target.split("::", 1)[0] in m["modules"]:
                    continue
                s, t = cap(e.source), cap(e.target)
                if t == m["cap"] and s and s != t:
                    groups.setdefault((s, t), []).append(e)
            for (s, t), edges in sorted(groups.items()):
                via = sorted({module_dotted(nodes, e.target) or e.target for e in edges})
                put(rule, "interface", "violates",
                    _t(lang, f"Bypasses the interface of {name(t)}", f"繞過 {name(t)} 的對外介面"),
                    _t(lang, f"{rule.statistic}; this PR calls {', '.join(via)} from {name(s)} directly.",
                       f"{rule.statistic}；這個 PR 讓 {name(s)} 直接呼叫 {'、'.join(via)}。"),
                    edges, [s, t], (s, t), m["examples"], f"{s}->{t}")

        elif rule.kind == "layers":
            groups: dict[str, list[Edge]] = {}
            for e in added:
                if e.kind != "uses_model" or e.source not in nodes or nodes[e.source].kind not in ("view", "method"):
                    continue
                o = _owner(nodes, e.source)
                if nodes[o].kind == "view" and cap(o) == m["cap"]:
                    groups.setdefault(o, []).append(e)
            for o, edges in sorted(groups.items()):
                label = nodes[o].label
                put(rule, "dependency_direction", "violates",
                    _t(lang, f"View queries the database directly: {label}", f"畫面直接查詢資料庫：{label}"),
                    _t(lang, f"{rule.statistic}; this PR's {label} queries the ORM itself instead of calling the service layer.",
                       f"{rule.statistic}；這個 PR 的 {label} 直接查詢資料，沒有經過服務層。"),
                    edges, [m["cap"]], None, m["examples"], o, ring="inner")

        elif rule.kind == "shared_mechanism":
            for n in sorted(nodes.values(), key=lambda n: n.id):
                if n.level != "module" or n.change_status not in ("added", "modified"):
                    continue
                head_ext = n.meta.get("ext_imports") or {}
                base_ext = (n.meta.get("base_meta") or {}).get("ext_imports") or {}
                new = sorted(t for t in head_ext if t.split(".")[0] in BYPASS and t not in base_ext)
                ref = next((r for r in n.code_refs if r.side == "head"), None)
                if not new or ref is None:
                    continue
                line = head_ext[new[0]]
                eid = ids.evidence_id("import", "head", ref.path, line, "bypass", *new)
                out.evidence.append(Evidence(eid, "import", f"{ref.path}:{line} imports {', '.join(new)}",
                                             [CodeRef("head", ref.path, line)], PRODUCER))
                rule_caps = [cap(n.id)]
                out.checks.append(ArchCheck(
                    id=f"arch:shared_mechanism:{ids.short_hash(n.id, length=10)}", origin="inferred", source="static",
                    check="shared_mechanism", ring="inner", verdict="violates", status="verified",
                    title=_t(lang, f"Background work outside the task framework: {n.label}", f"背景工作沒有使用既有機制：{n.label}"),
                    description=_t(lang, f"{rule.statistic}; this PR's {n.label} imports {', '.join(new)}.",
                                   f"{rule.statistic}；這個 PR 的 {n.label} 改用 {'、'.join(new)}。"),
                    capability_ids=[c for c in rule_caps if c], pr_refs=[CodeRef("head", ref.path, line)],
                    existing_refs=m["examples"][:MAX_EXAMPLES], evidence_refs=[eid, *rule.evidence_refs], rule_id=rule.id))

        elif rule.kind == "acyclic":
            biz = set(m["business"]) | {c for c, x in head_st.capabilities.items()
                                        if c not in base_st.capabilities and x.category == "business"}
            before = _cap_edges(base_st, biz)
            after = _cap_edges(head_st, biz)
            for comp in _sccs(biz, after):
                new_rels = sorted((a, b) for a, b in after - before if a in comp and b in comp)
                if not new_rels:
                    continue
                s, t = new_rels[0]
                edges = [e for e in added if e.kind in ("imports", "calls", "inherits")
                         and head_st.node_cap.get(e.source) == s and head_st.node_cap.get(e.target) == t]
                members = sorted(comp)
                loop = " ⇄ ".join(name(x) for x in members[:4])
                put(rule, "cycle", "violates",
                    _t(lang, f"New dependency cycle: {loop}", f"新的循環依賴：{loop}"),
                    _t(lang, f"This PR makes {name(s)} depend on {name(t)}, closing a cycle between business capabilities "
                             f"({rule.statistic} before).",
                       f"這個 PR 讓 {name(s)} 依賴 {name(t)}，在業務能力之間形成循環（修改前：{rule.statistic}）。"),
                    edges, [cap_ref(x) for x in members], (cap_ref(s), cap_ref(t)), m["examples"], "|".join(members))
    out.checks.sort(key=check_order)
    return out


RING_ORDER = {"outer": 0, "middle": 1, "inner": 2}
VERDICT_ORDER = {"violates": 0, "new_debt": 1, "unclear": 2, "follows": 3}


def check_order(c: ArchCheck) -> tuple:
    """Spec §8: outer ring first, then middle, then inner; within a ring problems before confirmations."""
    return RING_ORDER[c.ring], VERDICT_ORDER[c.verdict], c.status != "verified", c.id


def _ring(a: str | None, b: str | None) -> str:
    """A dependency inside one capability is its inner layering; between capabilities it is the middle ring."""
    return "inner" if a and a == b else "middle"


__all__ = ["CheckSet", "RuleSet", "check_order", "check_pr", "ignored_by", "infer_rules", "source_prefix", "violation"]
