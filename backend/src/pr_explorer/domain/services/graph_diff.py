"""Merge base and head side graphs into one graph with change_status (spec §4 Before/After)."""

from __future__ import annotations

import copy

from ..model.graph import Edge, Graph, Node, SideGraph

CONTAINER_KINDS = {"system", "package", "service"}


def _merge_refs(a: list, b: list) -> list:
    out = list(a)
    for r in b:
        if r not in out:
            out.append(r)
    return out


def diff_graphs(base: SideGraph, head: SideGraph) -> Graph:
    nodes: dict[str, Node] = {}
    for nid in sorted(set(base.nodes) | set(head.nodes)):
        b, h = base.nodes.get(nid), head.nodes.get(nid)
        if b and h:
            n = copy.copy(h)
            n.code_refs = _merge_refs(h.code_refs, b.code_refs)
            n.evidence_refs = _merge_refs(h.evidence_refs, b.evidence_refs)
            n.tags = sorted(set(h.tags) | set(b.tags))
            n.meta = dict(h.meta)
            n.meta["base_meta"] = b.meta
            hb, hh = b.meta.get("hash"), h.meta.get("hash")
            n.change_status = "modified" if (hb is not None and hh is not None and hb != hh) else "unchanged"
        elif h:
            n = copy.copy(h)
            n.code_refs = list(h.code_refs)
            n.evidence_refs = list(h.evidence_refs)
            n.meta = dict(h.meta)
            n.change_status = "added"
        else:
            assert b is not None
            n = copy.copy(b)
            n.code_refs = list(b.code_refs)
            n.evidence_refs = list(b.evidence_refs)
            n.meta = dict(b.meta)
            n.change_status = "removed"
        nodes[nid] = n

    # Containers carry no content of their own: they are modified iff something inside changed.
    children: dict[str, list[str]] = {}
    for n in nodes.values():
        if n.parent_id:
            children.setdefault(n.parent_id, []).append(n.id)

    def rollup(nid: str) -> bool:
        n = nodes[nid]
        changed_child = any([rollup(c) for c in children.get(nid, [])])
        if n.kind in CONTAINER_KINDS and n.change_status == "unchanged" and changed_child:
            n.change_status = "modified"
        return n.change_status != "unchanged"

    for n in list(nodes.values()):
        if n.parent_id is None:
            rollup(n.id)

    edges: dict[str, Edge] = {}
    for eid in sorted(set(base.edges) | set(head.edges)):
        b, h = base.edges.get(eid), head.edges.get(eid)
        src = h or b
        assert src is not None
        e = copy.copy(src)
        e.evidence_refs = _merge_refs(h.evidence_refs if h else [], b.evidence_refs if b else [])
        if b and h:
            e.change_status = "modified" if b.confidence != h.confidence else "unchanged"
        else:
            e.change_status = "added" if h else "removed"
        if e.source in nodes and e.target in nodes:
            edges[eid] = e

    evidence = dict(base.evidence)
    evidence.update(head.evidence)
    return Graph(nodes=nodes, edges=edges, evidence=evidence, base=base, head=head)
