"""Evidence-gated, bounded impact propagation (spec §5 Layer 5).

Direction: an edge `source -> target` means *source depends on target*, so impact flows
from a changed target back to its sources.

  direct               the node itself changed
  evidence_supported   1 hop, confirmed edge that *uses the changed unit itself*
                       (calls/routes_to/renders/uses_model/inherits a changed leaf symbol)
  confirmed_dependency 1 hop over a confirmed edge, but no evidence the changed part is used
                       (e.g. imports a changed module, or uses a class whose internals changed)
  potential            reachable over confirmed edges, 2..max_depth hops
  unknown              reachable only through confidence=unknown edges
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..model.graph import Edge, Node

USE_KINDS = {"calls", "routes_to", "renders", "uses_model", "inherits"}


@dataclass
class ImpactResult:
    assigned: int
    truncated: bool
    max_depth: int
    max_nodes: int


def _bfs(seeds: list[str], reverse: dict[str, list[Edge]], max_depth: int, allow_unknown: bool) -> dict[str, int]:
    depth = {s: 0 for s in seeds}
    queue = deque(seeds)
    while queue:
        cur = queue.popleft()
        if depth[cur] >= max_depth:
            continue
        for e in reverse.get(cur, []):
            if not allow_unknown and e.confidence != "confirmed":
                continue
            if e.source not in depth:
                depth[e.source] = depth[cur] + 1
                queue.append(e.source)
    return depth


def propagate_impact(
    nodes: dict[str, Node], edges: dict[str, Edge], max_depth: int = 3, max_nodes: int = 200
) -> ImpactResult:
    # External placeholders (unresolved/dynamic targets) have no content of their own: never a change seed.
    changed = sorted(n.id for n in nodes.values() if n.change_status != "unchanged" and n.kind != "external")
    for nid in changed:
        nodes[nid].impact_status = "direct"
        nodes[nid].impact_depth = 0

    live = [e for e in sorted(edges.values(), key=lambda e: e.id) if e.change_status != "removed"]
    reverse: dict[str, list[Edge]] = {}
    forward: dict[str, list[Edge]] = {}
    for e in live:
        reverse.setdefault(e.target, []).append(e)
        forward.setdefault(e.source, []).append(e)
    has_children = {n.parent_id for n in nodes.values() if n.parent_id}
    changed_set = set(changed)

    confirmed = _bfs(changed, reverse, max_depth, allow_unknown=False)
    anything = _bfs(changed, reverse, max_depth, allow_unknown=True)

    order = sorted(
        (nid for nid in anything if nid not in changed_set and nid in nodes),
        key=lambda nid: (confirmed.get(nid, max_depth + 1), anything[nid], nid),
    )
    assigned = 0
    truncated = False
    for nid in order:
        if assigned >= max_nodes:
            truncated = True
            break
        node = nodes[nid]
        if nid in confirmed:
            d = confirmed[nid]
            if d == 1:
                uses_changed_unit = any(
                    e.confidence == "confirmed"
                    and e.kind in USE_KINDS
                    and e.target in changed_set
                    and e.target not in has_children
                    for e in forward.get(nid, [])
                )
                node.impact_status = "evidence_supported" if uses_changed_unit else "confirmed_dependency"
            else:
                node.impact_status = "potential"
            node.impact_depth = d
        else:
            node.impact_status = "unknown"
            node.impact_depth = anything[nid]
        assigned += 1
    return ImpactResult(assigned=assigned, truncated=truncated, max_depth=max_depth, max_nodes=max_nodes)
