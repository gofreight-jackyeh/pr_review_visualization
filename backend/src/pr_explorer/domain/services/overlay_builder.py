"""PR overlay construction (pure): lay one PR's base/head graphs over a (possibly cached) system map.

The overlay may reference anything the system map lists; elements that exist only on head go to head_additions.
Symbols the bounded system map does not list are referenced through their module component."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ..model.changes import ChangedFile
from ..model.document import (
    BehaviorChange, Flow, FlowChange, HeadAdditions, Impact, Interface, Phase, Step, Text, Timeline,
)
from ..model.finding import Finding
from ..model.graph import Graph, SideGraph
from ..model import ids
from .flow_steps import FOCUS_REASON, MAX_DEPTH, build_timeline, diff_steps
from .system_map_builder import component_for
from .system_structure import Structure, reverse_reach

IMPACT_RANK = {"direct": 0, "evidence_supported": 1, "confirmed_dependency": 2, "potential": 3, "unknown": 4}
MAX_RELATED_FLOWS = 60
# Flows a PR touches are found by walking the call graph backwards from the changed code to the entry points, nearest
# first. The walk goes as deep as a flow's steps can show (MAX_DEPTH) and stops at TOUCH_MAX_NODES callers; at most
# TOUCH_MAX_BUILT touched flows are rebuilt on base and head (a change in a hub helper reaches hundreds of them).
TOUCH_MAX_DEPTH = MAX_DEPTH
TOUCH_MAX_NODES = 20_000
TOUCH_MAX_BUILT = 150
MAX_COMPONENT_IMPACTS = 300


@dataclass
class MapIndex:
    """What the system map document contains (IDs and display names); built from its JSON."""

    capabilities: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)
    entry_points: set[str] = field(default_factory=set)
    components: set[str] = field(default_factory=set)
    flows: set[str] = field(default_factory=set)
    relations: set[str] = field(default_factory=set)
    interfaces: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    names: dict[str, str] = field(default_factory=dict)  # element id -> business name (or technical name)
    step_labels: dict[str, dict] = field(default_factory=dict)  # step id -> business_label JSON
    phases: dict[str, list[Phase]] = field(default_factory=dict)  # flow id -> its phases in the map
    cap_domains: dict[str, tuple[str, str]] = field(default_factory=dict)  # business capability id -> (path, domain id)
    summary: dict | None = None
    system_name: str = ""

    @classmethod
    def from_doc(cls, doc: dict) -> "MapIndex":
        mi = cls(system_name=doc["system"]["technical_name"], summary=doc["system"].get("summary"))
        for key, attr in (("capabilities", "capabilities"), ("entities", "entities"), ("entry_points", "entry_points"),
                          ("components", "components"), ("flows", "flows")):
            for el in doc[key]:
                getattr(mi, attr).add(el["id"])
                bn = el.get("business_name")
                mi.names[el["id"]] = bn["text"] if bn else el["technical_name"]
        mi.cap_domains = {c["id"]: (c["path"], c["domain_id"]) for c in doc["capabilities"] if c.get("domain_id")}
        mi.relations = {r["id"] for r in doc["relations"]}
        mi.interfaces = {i["id"] for i in doc.get("interfaces", [])}
        mi.evidence = {e["id"] for e in doc["evidence"]}
        for f in doc["flows"]:
            for s in f["timeline"]["steps"]:
                if s.get("business_label"):
                    mi.step_labels[s["id"]] = s["business_label"]
            if f.get("phases"):
                mi.phases[f["id"]] = [Phase(p["id"], Text(p["label"]["text"], p["label"]["source"], list(p["label"]["evidence_refs"])),
                                            list(p["step_ids"])) for p in f["phases"]]
        return mi


class OverlayBuilder:
    def __init__(self, graph: Graph, base_st: Structure, head_st: Structure, mi: MapIndex):
        self.g = graph
        self.b = base_st
        self.h = head_st
        self.mi = mi
        self.ha = HeadAdditions()
        self._ha_ids: set[str] = set()
        self.changed = {n.id for n in graph.nodes.values() if n.change_status != "unchanged" and n.kind != "external"}

    # ---------- element references ----------
    def _add_ha(self, coll: list, el) -> str:
        if el.id not in self._ha_ids:
            self._ha_ids.add(el.id)
            coll.append(el)
        return el.id

    def cap_ref(self, cid: str | None) -> str | None:
        if cid is None:
            return None
        if cid in self.mi.capabilities:
            return cid
        if cid in self.h.capabilities:
            c = self.h.capabilities[cid]
            if c.category == "business" and c.domain_id is None:
                c.domain_id = self.domain_for(c.path, c.parent_capability_id)
            return self._add_ha(self.ha.capabilities, c)
        return None

    def iface_ref(self, i: Interface) -> str | None:
        """An interface the overlay may cite: listed in the map, else added to head_additions (head-only)."""
        if i.id in self.mi.interfaces or i.id in self._ha_ids:
            return i.id
        cap = self.cap_ref(i.capability_id)
        if cap is None:
            return None
        x = copy.copy(i)
        x.capability_id = cap
        if x.entry_point_id not in self.mi.entry_points and x.entry_point_id not in self._ha_ids:
            x.entry_point_id = None
        return self._add_ha(self.ha.interfaces, x)

    def domain_for(self, path: str, parent: str | None) -> str | None:
        """A capability new on the PR head joins its parent's domain, else the domain of the map capability sharing the
        longest directory prefix (the UI files a business capability without a domain under 'unassigned')."""
        if parent in self.mi.cap_domains:
            return self.mi.cap_domains[parent][1]
        parts = path.split("/")

        def shared(p: str) -> int:
            n = 0
            for a, b in zip(parts, p.split("/")):
                if a != b:
                    break
                n += 1
            return n

        best = sorted(((-shared(p), cid, d) for cid, (p, d) in self.mi.cap_domains.items()))
        return best[0][2] if best else None

    def entity_ref(self, eid: str | None) -> str | None:
        if eid is None:
            return None
        if eid in self.mi.entities:
            return eid
        if eid in self.h.entities and self.cap_ref(self.h.entities[eid].capability_id):
            return self._add_ha(self.ha.entities, self.h.entities[eid])
        return None

    def comp_ref(self, nid: str | None) -> str | None:
        if not nid:
            return None
        if nid in self.mi.components or nid in self._ha_ids:
            return nid
        head, base = self.g.head.nodes.get(nid), self.g.base.nodes.get(nid)
        if head is not None and base is None:  # exists only on the PR head
            cap = self.cap_ref(self.h.node_cap.get(nid))
            c = component_for(head, cap, "head") if cap else None
            if c is not None:
                return self._add_ha(self.ha.components, c)
        n = head or base
        while n is not None and n.level != "module" and n.parent_id:  # bounded map: fall back to the module
            n = (self.g.head.nodes.get(n.parent_id) or self.g.base.nodes.get(n.parent_id))
        if n is not None and n.id != nid and n.level == "module":
            return self.comp_ref(n.id)
        return None

    def node_cap(self, nid: str) -> str | None:
        return self.cap_ref(self.h.node_cap.get(nid) or self.b.node_cap.get(nid))

    def name(self, el_id: str) -> str:
        if el_id in self.mi.names:
            return self.mi.names[el_id]
        for coll in (self.h.capabilities, self.h.entities, self.h.entry_points, self.h.lite_flows):
            if el_id in coll:
                return coll[el_id].technical_name
        return el_id

    # ---------- changed files ----------
    def assign_capabilities(self, files: list[ChangedFile]) -> None:
        for f in files:
            st = self.b if f.status == "removed" else self.h
            path = (f.old_path or f.path) if f.status == "removed" else f.path
            f.capability_id = self.cap_ref(st.caps.cap_id(path))

    # ---------- flows ----------
    def _remap(self, steps: list[Step], raw: set[str]) -> None:
        for s in steps:
            raw.update(s.component_ids)
            s.capability_id = self.cap_ref(s.capability_id) or s.capability_id
            s.entity_id = self.entity_ref(s.entity_id)
            s.component_ids = list(dict.fromkeys(c for c in (self.comp_ref(x) for x in s.component_ids) if c))

    def touched_flows(self, base_sg: SideGraph, head_sg: SideGraph) -> tuple[dict[str, int], set[str], list[str]]:
        """Flow id -> distance (call edges) from its entry point to the nearest changed code, on either side, plus every
        node that reaches changed code and notes on the bounds that were hit. Added / removed flows count as distance 0;
        the forward reach of the map's structure (bounded per entry point) is kept as a fallback."""
        dist: dict[str, int] = {}
        inside: set[str] = set()
        notes: list[str] = []
        for sg, st in ((base_sg, self.b), (head_sg, self.h)):
            depth, capped = reverse_reach(sg, self.changed & set(sg.nodes), TOUCH_MAX_DEPTH, TOUCH_MAX_NODES)
            inside |= set(depth)
            if capped and not notes:
                notes.append(f"backward walk from the changed code stopped at {TOUCH_MAX_DEPTH} calls / {TOUCH_MAX_NODES} "
                             "callers: entry points further away are not compared")
            for epid, fact in st.facts.items():
                d = min((x for x in (depth.get(fact.id), depth.get(fact.handler_id or "")) if x is not None), default=None)
                fid = ids.flow_id(epid)
                if d is None and any(x in self.changed for x in st.reach.get(fid, [])):
                    d = TOUCH_MAX_DEPTH + 1
                if d is not None:
                    dist[fid] = min(d, dist.get(fid, d))
        for fid in set(self.b.lite_flows) ^ set(self.h.lite_flows):
            dist[fid] = 0
        return dist, inside, notes

    def flow_changes(self, base_sg: SideGraph, head_sg: SideGraph) -> tuple[list[FlowChange], dict[str, set[str]], list[str]]:
        """Flows the PR touches, rebuilt on base and head (built on demand: most are outside the map's flow budget).
        The third value holds notes on the bounds that were hit (empty: every touched flow was compared)."""
        out: list[FlowChange] = []
        flow_nodes: dict[str, set[str]] = {}
        dist, inside, notes = self.touched_flows(base_sg, head_sg)
        order = sorted(dist, key=lambda f: (dist[f], f))
        built = unchanged = 0
        for n, fid in enumerate(order):
            in_b, in_h = fid in self.b.lite_flows, fid in self.h.lite_flows
            lite = self.h.lite_flows[fid] if in_h else self.b.lite_flows[fid]
            epid = lite.entry_point_id
            if in_b and fid not in self.mi.flows:
                continue  # map built differently (should not happen for the same analyzer version)
            if len(out) >= MAX_RELATED_FLOWS or built >= TOUCH_MAX_BUILT:
                notes.append(f"{len(order)} entry points reach the changed code; the {n} nearest were compared "
                             f"({len(out)} flow change(s) shown, at most {MAX_RELATED_FLOWS})")
                break
            built += 1
            b_tl = build_timeline(base_sg, self.b, epid, fid, self.changed) if in_b else None
            h_tl = build_timeline(head_sg, self.h, epid, fid, self.changed) if in_h else None
            steps = diff_steps(b_tl.steps if b_tl else [], h_tl.steps if h_tl else [], self.changed, inside)
            status = "added" if not in_b else ("removed" if not in_h else "modified")
            focused = False
            if status == "modified" and all(s.step_change_status == "unchanged" for s in steps):
                # the change lies beyond the flow's step limits: walk only the calls that lead to it
                b_tl = build_timeline(base_sg, self.b, epid, fid, self.changed, inside)
                h_tl = build_timeline(head_sg, self.h, epid, fid, self.changed, inside)
                steps = diff_steps(b_tl.steps, h_tl.steps, self.changed, inside)
                focused = True
            if status == "modified" and all(s.step_change_status == "unchanged" for s in steps):
                unchanged += 1
                continue
            raw: set[str] = set()
            self._remap(steps, raw)
            flow_nodes[fid] = raw
            for s in steps:
                if s.step_change_status == "unchanged" and s.id in self.mi.step_labels:
                    lab = self.mi.step_labels[s.id]
                    s.business_label = Text(lab["text"], lab["source"], list(lab["evidence_refs"]))
            tl = h_tl or b_tl
            assert tl is not None
            static = [t for t in (b_tl, h_tl) if t is not None and t.mode == "static_flow"]
            mode, reason = ("static_flow", static[0].mode_reason) if static else ("animation", tl.mode_reason)
            if focused:
                reason = FOCUS_REASON + (f"; {reason}" if reason else "")
            ev = list(dict.fromkeys(e for s in steps if s.step_change_status != "unchanged" for e in s.evidence_refs[:1]))[:10]
            if status == "added":
                ep = self.h.entry_points[epid]
                if self.cap_ref(ep.capability_id) is None:
                    continue
                ep.handler_component_id = self.comp_ref(ep.handler_component_id)
                self._add_ha(self.ha.entry_points, ep)
                head_steps = [Step(s.id, i, s.kind, s.capability_id, s.component_ids, s.technical_label, s.evidence_status,
                                   s.evidence_refs, s.entity_id, s.business_label, s.branch)
                              for i, s in enumerate(x for x in steps if x.step_change_status != "removed")]
                caps = [c for c in dict.fromkeys(s.capability_id for s in head_steps) if c] or [ep.capability_id]
                ents = [e for e in dict.fromkeys(s.entity_id for s in head_steps) if e]
                self._add_ha(self.ha.flows, Flow(fid, lite.technical_name, epid, caps, ents,
                                                 Timeline(mode, head_steps, reason), lite.evidence_refs))
            out.append(FlowChange(fid, status, mode, steps, ev or list(lite.evidence_refs), reason))
        if unchanged:
            notes.append(f"{unchanged} touched flow(s) show no step difference (the change is beyond their step limits "
                         "or does not alter a business step)")
        return out, flow_nodes, notes

    # ---------- impacts ----------
    def impacts(self, flow_changes: list[FlowChange], diff_ev: dict[str, str]) -> list[Impact]:
        best: dict[str, Impact] = {}

        def put(tid: str | None, kind: str, change: str, status: str, ev: list[str], depth: int | None = None) -> None:
            ev = [e for e in dict.fromkeys(ev) if e]
            if not tid or not ev:
                return
            cur = best.get(tid)
            if cur is None:
                best[tid] = Impact(tid, kind, change, status, ev[:4], depth)
                return
            if IMPACT_RANK[status] < IMPACT_RANK[cur.impact_status]:
                cur.impact_status, cur.impact_depth = status, depth
            if cur.change_status == "unchanged" and change != "unchanged":
                cur.change_status = change
            cur.evidence_refs = list(dict.fromkeys(cur.evidence_refs + ev))[:4]

        def cap_change(cid: str) -> str:
            if cid not in self.b.capabilities:
                return "added"
            if cid not in self.h.capabilities:
                return "removed"
            return "modified"

        for n in sorted(self.g.nodes.values(), key=lambda n: n.id):
            if n.kind in ("external", "route") or (n.change_status == "unchanged" and n.impact_status == "none"):
                continue
            direct = n.change_status != "unchanged"
            status = "direct" if direct else n.impact_status
            path = n.code_refs[0].path if n.code_refs else ""
            ev = n.evidence_refs[:1] + ([diff_ev[path]] if direct and path in diff_ev else [])
            cid = self.comp_ref(n.id)
            if cid:
                change = n.change_status if cid == n.id else ("modified" if direct else "unchanged")
                put(cid, "component", change, status, ev, None if direct else n.impact_depth)
            cap = self.node_cap(n.id)
            if cap:
                put(cap, "capability", cap_change(cap) if direct else "unchanged", status, ev, None if direct else n.impact_depth)
            ent = self.h.entity_of_node.get(n.id) or self.b.entity_of_node.get(n.id)
            if ent and self.entity_ref(ent):
                put(ent, "entity", n.change_status, status, ev, None if direct else n.impact_depth)

        comps = sorted((i for i in best.values() if i.target_kind == "component"),
                       key=lambda i: (IMPACT_RANK[i.impact_status], i.target_id))
        for drop in comps[MAX_COMPONENT_IMPACTS:]:
            del best[drop.target_id]

        for fc in flow_changes:
            epid = (self.h.lite_flows.get(fc.flow_id) or self.b.lite_flows[fc.flow_id]).entry_point_id
            moved = [s for s in fc.steps if s.step_change_status != "unchanged"]
            enter_changed = any(s.kind == "enter" for s in moved) or fc.change_status != "modified"
            if enter_changed:
                status = "direct"
            elif any(s.evidence_status == "confirmed" for s in moved):
                status = "evidence_supported"
            elif any(s.evidence_status == "possible" for s in moved):
                status = "potential"
            else:
                status = "unknown"
            put(fc.flow_id, "flow", fc.change_status, status, fc.evidence_refs)
            ep_change = fc.change_status if fc.change_status != "modified" else ("modified" if enter_changed else "unchanged")
            put(epid, "entry_point", ep_change, status, fc.evidence_refs)
        kinds = {"capability": 0, "flow": 1, "entry_point": 2, "entity": 3, "component": 4}
        return sorted(best.values(), key=lambda i: (kinds[i.target_kind], IMPACT_RANK[i.impact_status], i.target_id))

    # ---------- findings ----------
    def map_findings(self, findings: list[Finding], flow_nodes: dict[str, set[str]], lang: str = "en") -> None:
        zh = lang == "zh-Hant"
        sep = "、" if zh else ", "
        for f in findings:
            f.capability_ids = list(dict.fromkeys(c for c in (self.node_cap(n) for n in f.node_ids) if c))
            f.flow_ids = [fid for fid, raw in sorted(flow_nodes.items()) if raw & set(f.node_ids)][:10]
            if f.capability_ids or f.flow_ids:
                caps = sep.join(self.name(c) for c in f.capability_ids) or ("無" if zh else "no capability")
                flows = sep.join(self.name(x) for x in f.flow_ids[:3])
                more = (f" 等 {len(f.flow_ids)} 條" if zh else f" (+{len(f.flow_ids) - 3} more)") if len(f.flow_ids) > 3 else ""
                if zh:
                    f.business_impact = f"影響的業務能力：{caps}" + (f"；相關流程：{flows}{more}" if flows else "")
                else:
                    f.business_impact = f"Affects {caps}" + (f"; flows: {flows}{more}" if flows else "")


def static_behavior_changes(flow_changes: list[FlowChange], names, lang: str = "en") -> list[BehaviorChange]:
    """Without Claude: one technical before/after per changed flow, derived from its step changes only."""
    out: list[BehaviorChange] = []
    for fc in flow_changes[:20]:
        moved = [s for s in fc.steps if s.step_change_status != "unchanged"]
        if not moved:
            continue
        before = [s for s in moved if s.step_change_status in ("removed", "changed")]
        after = [s for s in moved if s.step_change_status in ("added", "changed")]
        ev = list(dict.fromkeys(e for s in moved for e in s.evidence_refs[:1]))[:6]
        caps = list(dict.fromkeys(s.capability_id for s in moved))

        def summarize(steps: list[Step]) -> str:
            return "; ".join(f"[{s.step_change_status}] {s.technical_label}" for s in steps[:6]) + (
                f"; +{len(steps) - 6} more" if len(steps) > 6 else "")

        flow_name = names(fc.flow_id)
        zh = lang == "zh-Hant"
        w_before, w_after = ("修改前：", "修改後：") if zh else ("before: ", "after: ")
        gone = "這個流程已不存在" if zh else "this flow no longer exists"
        b = Text(f"{flow_name} — {w_before}" + summarize(before), "static", ev) if before and fc.change_status != "added" else None
        a_text = (f"{flow_name} — {w_after}" + summarize(after)) if after else f"{flow_name} — {gone}"
        out.append(BehaviorChange(f"bc:static:{fc.flow_id}", Text(a_text, "static", ev), [fc.flow_id], caps, b))
    return out


__all__ = ["MapIndex", "OverlayBuilder", "static_behavior_changes"]
