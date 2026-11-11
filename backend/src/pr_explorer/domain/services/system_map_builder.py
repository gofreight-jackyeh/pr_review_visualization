"""Assemble the system_map document from one side's structure (pure).

Components are bounded so a very large (10k+ file) repository stays a few MB: every code module, plus only the symbols that
appear in built flow steps, entry-point handlers and entity classes."""

from __future__ import annotations

from ..model.changes import Coverage
from ..model.document import (
    ArchRule, Component, Flow, FlowBudget, Interface, Repo, SemanticStatus, SystemInfo, SystemMap,
)
from .domains import static_domains
from ..model.graph import Evidence, Node, SideGraph
from .flow_steps import build_timeline
from .system_structure import Structure, select_representative

COMPONENT_KIND = {
    "module": "module", "class": "class", "function": "function", "method": "method", "view": "view",
    "serializer": "serializer", "form": "form", "model": "model", "task": "task", "command": "command",
    "component": "ui_component", "external": "external",
}
DEEP_MODULE_PREFIXES = ("node:py:", "node:ts:")


def component_for(n: Node, cap: str, side: str) -> Component | None:
    kind = COMPONENT_KIND.get(n.kind)
    if kind is None or not n.evidence_refs:
        return None
    refs = [r for r in n.code_refs if r.side == side][:1]
    if kind != "external" and not refs:
        return None
    name = n.meta.get("dotted") or n.label
    return Component(n.id, name, cap, kind, refs, n.evidence_refs[:2])


def build_flows(sg: SideGraph, st: Structure, budget: int) -> tuple[list[Flow], list[str]]:
    """All entry points get a flow; `budget` representative flows get full business steps. Returns (flows, built ids)."""
    built = select_representative(st, budget)
    flows: list[Flow] = []
    for fid in sorted(st.lite_flows):
        lite = st.lite_flows[fid]
        if fid not in built:
            flows.append(lite)
            continue
        tl = build_timeline(sg, st, lite.entry_point_id, fid)
        caps = list(dict.fromkeys([lite.capability_ids[0]] + [s.capability_id for s in tl.steps]))
        ents = list(dict.fromkeys(s.entity_id for s in tl.steps if s.entity_id))
        flows.append(Flow(fid, lite.technical_name, lite.entry_point_id, caps, ents, tl, lite.evidence_refs))
    return flows, built


def collect_components(sg: SideGraph, st: Structure, flows: list[Flow]) -> list[Component]:
    wanted: set[str] = {n.id for n in sg.nodes.values() if n.level == "module" and n.id.startswith(DEEP_MODULE_PREFIXES)}
    for f in flows:
        for s in f.timeline.steps:
            wanted.update(s.component_ids)
    wanted.update(ep.handler_component_id for ep in st.entry_points.values() if ep.handler_component_id)
    wanted.update(e.meta["node_id"] for e in st.entities.values())
    out: list[Component] = []
    for nid in sorted(wanted):
        n = sg.nodes.get(nid)
        cap = st.node_cap.get(nid)
        c = component_for(n, cap, sg.side) if n is not None and cap else None
        if c is not None:
            out.append(c)
    return out


def referenced_evidence(doc_parts: list, pool: dict[str, Evidence]) -> list[Evidence]:
    """Only evidence some element cites is written (keeps the document bounded)."""
    seen: set[str] = set()

    def walk(v) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "evidence_refs":
                    seen.update(x)
                else:
                    walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif hasattr(v, "__dataclass_fields__"):
            walk({k: getattr(v, k) for k in v.__dataclass_fields__ if k != "meta"})

    walk(doc_parts)
    return [pool[e] for e in sorted(seen) if e in pool]


def assemble_system_map(
    sg: SideGraph, st: Structure, flows: list[Flow], built: list[str], *, repo: Repo, commit_sha: str,
    analyzer_version: str, system_map_id: str, generated_at: str, coverage: list[Coverage], semantic: SemanticStatus,
    extra_evidence: dict[str, Evidence] | None = None, rules: list[ArchRule] | None = None,
    interfaces: list[Interface] | None = None,
) -> SystemMap:
    """Domains start static (one per top-level directory); Claude's assignment replaces them later."""
    components = collect_components(sg, st, flows)
    known = {c.id for c in components}
    eps = []
    for ep in st.entry_points.values():
        if ep.handler_component_id and ep.handler_component_id not in known:
            ep.handler_component_id = None
        eps.append(ep)
    for f in flows:  # a step may only cite components the map lists
        for s in f.timeline.steps:
            s.component_ids = [c for c in s.component_ids if c in known]
    caps = sorted(st.capabilities.values(), key=lambda c: c.id)
    domains = static_domains(caps)
    rules = rules or []
    known_eps = {ep.id for ep in eps}
    ifaces = sorted((i for i in interfaces or [] if i.capability_id in st.capabilities),
                    key=lambda i: ({"outer": 0, "middle": 1}[i.ring], i.kind, i.id))
    for i in ifaces:
        if i.entry_point_id not in known_eps:
            i.entry_point_id = None
    parts = [caps, list(st.entities.values()), eps, components, list(st.relations.values()), flows, domains, rules, ifaces]
    pool = {**sg.evidence, **st.evidence, **(extra_evidence or {})}
    return SystemMap(
        analyzer_version=analyzer_version, system_map_id=system_map_id, generated_at=generated_at, repo=repo,
        commit_sha=commit_sha, system=SystemInfo(repo.name), coverage=coverage, capabilities=caps,
        entities=sorted(st.entities.values(), key=lambda e: e.id), entry_points=sorted(eps, key=lambda e: e.id),
        components=components, relations=sorted(st.relations.values(), key=lambda r: r.id),
        flows=flows, flow_budget=FlowBudget(len(st.entry_points), sum(1 for f in flows if f.id in built)),
        evidence=referenced_evidence(parts, pool), semantic=semantic, domains=domains, architecture_rules=rules,
        interfaces=ifaces,
    )


__all__ = ["assemble_system_map", "build_flows", "collect_components", "component_for", "referenced_evidence"]
