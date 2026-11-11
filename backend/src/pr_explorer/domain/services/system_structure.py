"""Spec v2 Layer 4 — System Map construction, one side at a time (pure; no I/O).

Capabilities are grouped *statically* from the file layout (Claude may later name them, never regroup them):
  1. Django app: a directory containing apps.py (deepest match wins)
  2. TS/JS feature: the first directory under a `src/` or `app/` segment
  3. otherwise the top-level directory that contains code
A capability with more than SPLIT_MAX_ENTRY_POINTS entry points is split ONE level by its direct sub-directories: each
sub-directory with at least SPLIT_MIN_CHILD_ENTRY_POINTS entry points becomes a child, the rest merge into one "other"
child (path `<parent>/*`), at most SPLIT_MAX_CHILDREN children per parent (one level only: recursive
splitting makes the capabilities too fine-grained). Each capability then gets a static category (business / shared / engineering).

Everything is O(files + nodes + edges) except flow reachability, which is bounded per entry point.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Callable

from ..model import ids
from ..model.document import Capability, Entity, EntryPoint, Flow, Relation, Timeline
from ..model.graph import CodeRef, EntryFact, Evidence, Producer, SideGraph

PRODUCER = Producer("capability_grouping", "1")
NON_CODE = {"markdown", "text", "restructuredtext", "json", "yaml", "toml", "html", "css", "dotenv", "unknown", "sql",
            "dockerfile", "make", "shell"}
TS_LANGS = {"typescript", "javascript"}
SKIP_DIRS = {"node_modules", "site-packages", ".venv", "venv", "dist", "build", "__pycache__", "migrations"}
FOLLOW = {"calls", "routes_to", "uses_model", "renders"}  # 'emits' is async: the task is a flow of its own
MAX_REACH = 400
# Layer 4 split + category thresholds (spec v2 §4/§5). Deliberately small, explicit constants.
SPLIT_MAX_ENTRY_POINTS = 50  # a capability with more entry points is split by its direct sub-directories (one level)
SPLIT_MIN_CHILD_ENTRY_POINTS = 5  # a direct sub-directory with fewer entry points merges into the "other" child
SPLIT_MAX_CHILDREN = 12  # children per split parent, the "other" child included
OTHER_CHILD = "*"  # last path segment of the merged child: `<parent>/*`
OTHER_LABEL = {"zh-Hant": "其他", "en": "Other"}
# Directory names that mark tests, deployment, build config, docs and scripts. A capability whose path contains one
# of them AND has no entry points and no entities is `engineering` (hidden by default).
ENGINEERING_DIRS = frozenset({
    "bin", "build", "ci", "cypress", "deploy", "deployment", "deployments", "doc", "docker", "docs", "e2e", "helm",
    "infra", "infrastructure", "jest", "k8s", "playwright", "script", "scripts", "terraform", "test", "testing",
    "tests", "tools", "webpack", "__mocks__", "__tests__",
})
SHARED_MAX_ENTRY_POINTS = 2  # `shared`: (almost) no entry points of its own ...
SHARED_MIN_DEPENDENT_SHARE = 0.5  # ... and depended on by at least half of the other non-engineering capabilities
SHARED_MIN_DEPENDENTS = 3  # ... and by at least this many (tiny repositories have no shared layer)
LITE_REASON = "outside the flow budget: business steps are built for representative flows and for flows a PR touches"


@dataclass
class CapabilityIndex:
    django_apps: set[str]
    ts_features: set[str]
    top_dirs: set[str]
    markers: dict[str, str]  # capability path -> file used as evidence
    kinds: dict[str, str]  # capability path -> django_app | ts_feature | package | split
    subs: dict[str, str] = field(default_factory=dict)  # split sub-capability path -> parent capability path

    def of(self, path: str) -> str | None:
        """Capability path for a repository file, or None (root files, docs, CI config...)."""
        base = self._base(path)
        if base is None or not self.subs:
            return base
        parts = path.split("/")
        depth = base.count("/") + 1
        for i in range(len(parts) - 1, depth, -1):  # deepest split sub-directory wins (maps before 2.2.0 nest deeper)
            d = "/".join(parts[:i])
            if d in self.subs:
                return d
        if len(parts) - 1 > depth and merges_into_other(parts[depth]) and f"{base}/{OTHER_CHILD}" in self.subs:
            return f"{base}/{OTHER_CHILD}"
        return base

    def _base(self, path: str) -> str | None:
        parts = path.split("/")
        for i in range(len(parts) - 1, 0, -1):
            d = "/".join(parts[:i])
            if d in self.django_apps:
                return d
        ts = ts_feature(path)
        if ts and ts in self.ts_features:
            return ts
        if len(parts) > 1 and parts[0] in self.top_dirs:
            return parts[0]
        return None

    def cap_id(self, path: str) -> str | None:
        p = self.of(path)
        return ids.capability_id(p) if p is not None else None


def ts_feature(path: str) -> str | None:
    parts = path.split("/")
    for i, seg in enumerate(parts[:-1]):
        if seg in ("src", "app"):
            return "/".join(parts[: i + 2]) if i + 1 < len(parts) - 1 else "/".join(parts[: i + 1])
    return None


def merges_into_other(sub_dir: str) -> bool:
    """Engineering and skipped sub-directories (tests, migrations...) stay with the parent, never in "other"."""
    return sub_dir not in ENGINEERING_DIRS and sub_dir not in SKIP_DIRS and not sub_dir.startswith(".")


def _skipped(path: str) -> bool:
    parts = path.split("/")
    return any(p in SKIP_DIRS or p.startswith(".") for p in parts[:-1])


def group_capabilities(files: set[str], language_of: Callable[[str], str]) -> CapabilityIndex:
    apps = {f.rsplit("/", 1)[0] for f in files if f.endswith("/apps.py") and not _skipped(f)}
    idx = CapabilityIndex(apps, set(), set(), {}, {})
    for a in apps:
        idx.markers[a] = f"{a}/apps.py"
        idx.kinds[a] = "django_app"
    for f in sorted(files):
        if _skipped(f):
            continue
        lang = language_of(f)
        if lang in NON_CODE:
            continue
        parts = f.split("/")
        if any("/".join(parts[:i]) in apps for i in range(len(parts) - 1, 0, -1)):
            continue
        ts = ts_feature(f) if lang in TS_LANGS else None
        if ts:
            if ts not in idx.ts_features:
                idx.ts_features.add(ts)
                idx.markers[ts] = f
                idx.kinds[ts] = "ts_feature"
            continue
        if len(parts) > 1:
            top = parts[0]
            if top not in idx.top_dirs:
                idx.top_dirs.add(top)
                idx.markers[top] = f"{top}/__init__.py" if f"{top}/__init__.py" in files else f
                idx.kinds[top] = "package"
    return idx


@dataclass
class Structure:
    side: str
    caps: CapabilityIndex
    capabilities: dict[str, Capability] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)
    entry_points: dict[str, EntryPoint] = field(default_factory=dict)
    relations: dict[str, Relation] = field(default_factory=dict)
    lite_flows: dict[str, Flow] = field(default_factory=dict)
    node_cap: dict[str, str] = field(default_factory=dict)  # graph node id -> capability id
    entity_of_node: dict[str, str] = field(default_factory=dict)  # model node id -> entity id
    facts: dict[str, EntryFact] = field(default_factory=dict)  # entry point id -> fact
    reach: dict[str, list[str]] = field(default_factory=dict)  # flow id -> reachable node ids (BFS order)
    evidence: dict[str, Evidence] = field(default_factory=dict)  # evidence created here
    skipped_entry_points: int = 0

    def cap_name(self, cap_id: str) -> str:
        c = self.capabilities.get(cap_id)
        return c.technical_name if c else cap_id.removeprefix("cap:")


def _adjacency(sg: SideGraph) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for e in sorted(sg.edges.values(), key=lambda e: e.id):
        if e.kind in FOLLOW:
            adj.setdefault(e.source, []).append(e.target)
    return adj


def reach_from(start: str | None, adj: dict[str, list[str]], limit: int = MAX_REACH) -> list[str]:
    if not start:
        return []
    seen = {start}
    order = [start]
    q = deque([start])
    while q and len(order) < limit:
        for t in adj.get(q.popleft(), []):
            if t not in seen:
                seen.add(t)
                order.append(t)
                q.append(t)
                if len(order) >= limit:
                    break
    return order


def plan_splits(sg: SideGraph, caps: CapabilityIndex, limit: int = SPLIT_MAX_ENTRY_POINTS) -> dict[str, str]:
    """Child capability path -> parent capability path for every capability over `limit` entry points (one level).

    Direct sub-directories with >= SPLIT_MIN_CHILD_ENTRY_POINTS entry points become children (most entry points first,
    ties by path); the other code sub-directories merge into `<parent>/*`; at most SPLIT_MAX_CHILDREN children. No
    child qualifies → no split. Engineering / skipped sub-directories stay with the parent. Deterministic: depends only
    on file paths and entry points."""
    modules: dict[str, list[str]] = {}  # capability path -> module files (the code it groups)
    for n in sg.nodes.values():
        if n.level == "module" and n.code_refs:
            cap = caps.of(n.code_refs[0].path)
            if cap is not None:
                modules.setdefault(cap, []).append(n.code_refs[0].path)
    eps: Counter = Counter()
    for fact in sg.entry_points:
        h = sg.nodes.get(fact.handler_id or "")
        eps[h.code_refs[0].path if h is not None and h.code_refs else fact.ref.path] += 1
    subs: dict[str, str] = {}
    for cap in sorted(modules):
        files = sorted(set(modules[cap]) | {f for f in eps if caps.of(f) == cap})
        if sum(eps[f] for f in files) <= limit:
            continue
        per_sub: Counter = Counter()
        for f in files:
            rest = f[len(cap) + 1:]
            if "/" in rest and merges_into_other(first := rest.split("/", 1)[0]):
                per_sub[f"{cap}/{first}"] += eps[f]
        ranked = sorted(per_sub, key=lambda d: (-per_sub[d], d))
        keep = [d for d in ranked if per_sub[d] >= SPLIT_MIN_CHILD_ENTRY_POINTS]
        if not keep:
            continue
        if len(keep) < len(ranked) or len(keep) > SPLIT_MAX_CHILDREN:  # an "other" child is needed: it takes a slot
            keep = keep[: SPLIT_MAX_CHILDREN - 1]
            subs[f"{cap}/{OTHER_CHILD}"] = cap
        for d in keep:
            subs[d] = cap
    return subs


def build_structure(sg: SideGraph, language_of: Callable[[str], str], splits: dict[str, str] | None = None,
                    lang: str = "en") -> Structure:
    """`splits`: sub-capability path -> parent path to use instead of planning them (the PR head reuses the map's).
    `lang` only names the merged "other" child."""
    side = sg.side
    caps = group_capabilities(sg.files, language_of)
    caps.subs = dict(plan_splits(sg, caps) if splits is None else splits)
    st = Structure(side, caps)
    members: dict[str, set[str]] = {}  # "other" child -> the sub-directories merged into it
    for sub in sorted(caps.subs):
        prefix = caps.subs[sub] if sub.endswith("/" + OTHER_CHILD) else sub
        mine = [f for f in sorted(sg.files) if f.startswith(prefix + "/") and caps.of(f) == sub]
        if not mine or caps.subs[sub] not in caps.markers:
            continue  # nothing of it on this side (the head may have deleted it)
        caps.markers[sub], caps.kinds[sub] = mine[0], "split"
        if prefix != sub:
            members[sub] = {f[len(prefix) + 1:].split("/", 1)[0] for f in mine}
    caps.subs = {s: p for s, p in caps.subs.items() if s in caps.markers}

    for path in sorted(caps.markers):
        cid = ids.capability_id(path)
        marker = caps.markers[path]
        kind = caps.kinds[path]
        desc = {"django_app": f"Django app {path} (declared by {marker})",
                "ts_feature": f"TS/JS feature directory {path}",
                "package": f"Top-level package/directory {path}",
                "split": (f"Sub-directories {', '.join(sorted(members[path])[:10])}"
                          f"{f' (+{len(members[path]) - 10} more)' if len(members[path]) > 10 else ''} of "
                          f"{caps.subs.get(path)} merged into one child: each has fewer than {SPLIT_MIN_CHILD_ENTRY_POINTS} "
                          f"entry points, or the parent reached {SPLIT_MAX_CHILDREN} children"
                          if path in members else
                          f"Sub-directory {path} of {caps.subs.get(path)}: split because its capability has more than "
                          f"{SPLIT_MAX_ENTRY_POINTS} entry points")}[kind]
        ev = Evidence(f"ev:cap:{side}:{path}", "framework_rule" if kind == "django_app" else "ast", desc,
                      [CodeRef(side, marker, 1)], PRODUCER)
        st.evidence[ev.id] = ev
        parent = ids.capability_id(caps.subs[path]) if path in caps.subs else None
        name = OTHER_LABEL.get(lang, OTHER_LABEL["en"]) if path in members else path.rsplit("/", 1)[-1]
        st.capabilities[cid] = Capability(cid, name, path, [ev.id], component_count=0,
                                          parent_capability_id=parent)

    langs: dict[str, dict[str, int]] = {}
    for n in sg.nodes.values():
        if not n.code_refs:
            continue
        cid = caps.cap_id(n.code_refs[0].path)
        if cid is None or cid not in st.capabilities:
            continue
        st.node_cap[n.id] = cid
        if n.kind not in ("route", "external"):
            st.capabilities[cid].component_count += 1
            if n.level == "module" and n.language:
                langs.setdefault(cid, {}).setdefault(n.language, 0)
                langs[cid][n.language] += 1
    for cid, counts in langs.items():
        st.capabilities[cid].language = max(sorted(counts), key=lambda k: counts[k])
    # External placeholders (unresolved/dynamic targets) belong to the capability of their first caller.
    for e in sorted(sg.edges.values(), key=lambda e: e.id):
        if e.target not in st.node_cap and e.source in st.node_cap and sg.nodes.get(e.target) and sg.nodes[e.target].kind == "external":
            st.node_cap[e.target] = st.node_cap[e.source]

    for n in sorted(sg.nodes.values(), key=lambda n: n.id):
        if n.kind != "model" or n.meta.get("abstract") or n.id not in st.node_cap:
            continue
        eid = ids.entity_id(n.id)
        st.entity_of_node[n.id] = eid
        st.entities[eid] = Entity(eid, n.label, st.node_cap[n.id], [n.code_refs[0]], n.evidence_refs[:2], meta={"node_id": n.id})
        st.capabilities[st.node_cap[n.id]].entity_ids.append(eid)

    adj = _adjacency(sg)
    for fact in sg.entry_points:
        cap = st.node_cap.get(fact.handler_id or "") or caps.cap_id(fact.ref.path)
        if cap is None or cap not in st.capabilities:
            st.skipped_entry_points += 1
            continue
        epid = ids.entry_point_id(fact.id)
        if epid in st.entry_points:
            continue
        handler = fact.handler_id if fact.handler_id in sg.nodes else None
        st.facts[epid] = fact
        st.entry_points[epid] = EntryPoint(epid, fact.label, cap, fact.kind, [fact.ref], [fact.evidence_id],
                                           handler_component_id=handler, meta={"fact_id": fact.id})
        st.capabilities[cap].entry_point_ids.append(epid)
        fid = ids.flow_id(epid)
        reach = reach_from(handler, adj)
        st.reach[fid] = reach
        cap_ids = list(dict.fromkeys([cap] + [st.node_cap[x] for x in reach if x in st.node_cap]))
        ent_ids = list(dict.fromkeys(st.entity_of_node[x] for x in reach if x in st.entity_of_node))
        st.lite_flows[fid] = Flow(fid, fact.label, epid, cap_ids, ent_ids, Timeline("static_flow", [], LITE_REASON),
                                  [fact.evidence_id])

    agg: dict[str, list] = {}  # relation id -> [source, target, kind, weight, confirmed, evidence]
    for e in sorted(sg.edges.values(), key=lambda e: e.id):
        src = st.node_cap.get(e.source)
        if src is None:
            continue
        if e.kind == "uses_model":
            dst, kind = st.entity_of_node.get(e.target), "uses_entity"
        elif e.kind in ("calls", "routes_to", "emits"):
            dst, kind = st.node_cap.get(e.target), "calls"
        elif e.kind in ("imports", "inherits", "renders"):
            dst, kind = st.node_cap.get(e.target), "depends_on"
        else:
            continue
        if dst is None or dst == src:
            continue
        rid = ids.relation_id(kind, src, dst)
        a = agg.setdefault(rid, [src, dst, kind, 0, False, []])
        a[3] += 1
        a[4] = a[4] or e.confidence == "confirmed"
        if len(a[5]) < 5:
            a[5].extend(r for r in e.evidence_refs[:1] if r not in a[5])
    for rid, (src, dst, kind, weight, confirmed, evs) in sorted(agg.items()):
        if evs:
            st.relations[rid] = Relation(rid, src, dst, kind, weight, "confirmed" if confirmed else "unknown", evs)
    categorize(st)
    return st


def categorize(st: Structure) -> None:
    """engineering: an ENGINEERING_DIRS path with no entry points and no entities. shared: few entry points and depended
    on (imports / calls) by most other non-engineering capabilities. Everything else: business."""
    caps = st.capabilities
    for c in caps.values():
        eng = set(c.path.split("/")) & ENGINEERING_DIRS and not c.entry_point_ids and not c.entity_ids
        c.category = "engineering" if eng else "business"
    others = [c for c in caps.values() if c.category != "engineering"]
    dependents: dict[str, set[str]] = {}
    for r in st.relations.values():
        if r.kind != "uses_entity" and r.target in caps and caps[r.source].category != "engineering":
            dependents.setdefault(r.target, set()).add(r.source)
    for c in others:
        n = len(dependents.get(c.id, ()))
        if (len(c.entry_point_ids) <= SHARED_MAX_ENTRY_POINTS and n >= SHARED_MIN_DEPENDENTS
                and n >= SHARED_MIN_DEPENDENT_SHARE * (len(others) - 1)):
            c.category = "shared"


def select_representative(st: Structure, budget: int = 20) -> list[str]:
    """PR-independent pick of flows that get full timelines: the richest flow of each capability first (largest
    capabilities first), then the richest remaining flows. Deterministic."""
    def rank(fid: str) -> tuple:
        f = st.lite_flows[fid]
        return (-len(f.capability_ids), -len(f.entity_ids), -len(st.reach.get(fid, [])), fid)

    by_cap: dict[str, list[str]] = {}
    for fid, f in st.lite_flows.items():
        by_cap.setdefault(st.entry_points[f.entry_point_id].capability_id, []).append(fid)
    chosen: list[str] = []
    for cid in sorted(by_cap, key=lambda c: (-(st.capabilities[c].component_count or 0), c)):
        if len(chosen) >= budget:
            break
        chosen.append(min(by_cap[cid], key=rank))
    for fid in sorted(st.lite_flows, key=rank):
        if len(chosen) >= budget:
            break
        if fid not in chosen:
            chosen.append(fid)
    return chosen
