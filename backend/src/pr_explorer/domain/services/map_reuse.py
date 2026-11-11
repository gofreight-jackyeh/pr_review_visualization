"""Incremental system-map explanation (pure): reuse Claude-written texts from an earlier map of the same repository.

The static map is always rebuilt for the PR's merge base; only the Claude texts are carried over. A capability's
texts (its own, its entities', entry points', flows' and their steps') are reused when its fingerprint is unchanged:
the git blob SHAs of every file grouped into it plus the IDs of its elements and flow steps (so a flow that now runs
through different steps is re-explained). Every reused text is re-validated against the new map — all of its
evidence must still exist — and dropped otherwise (counted like any other dropped claim).

Also carried over (schema 2.1.0): a flow's phases when its step IDs are unchanged, and the Claude domain assignment
when the set of business capabilities is unchanged. Any stored 2.x map donates; a 2.0.0 map simply has no phases or
domains, so only those steps are asked. Engineering capabilities are never sent to Claude (hidden by default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..model.document import Phase, SystemMap, Text
from ..model.ids import short_hash
from .domains import reuse_domains
from .semantic_gate import phase_flows
from .system_structure import group_capabilities

SUMMARY_REUSE_MAX_CHANGED = 0.25  # re-ask the system summary when more than this share of capabilities changed


def _cap_elements(doc: dict) -> dict[str, list[str]]:
    """capability id -> sorted IDs of its entities, entry points, flows (by entry point) and their steps."""
    out: dict[str, list[str]] = {c["id"]: [] for c in doc["capabilities"]}
    ep_cap: dict[str, str] = {}
    for key in ("entities", "entry_points"):
        for el in doc[key]:
            out.setdefault(el["capability_id"], []).append(el["id"])
            if key == "entry_points":
                ep_cap[el["id"]] = el["capability_id"]
    for f in doc["flows"]:
        cap = ep_cap.get(f["entry_point_id"])
        if cap is not None:
            out[cap] += [f["id"]] + [s["id"] for s in f["timeline"]["steps"]]
    return {k: sorted(v) for k, v in out.items()}


def skeleton(smap: SystemMap) -> dict:
    """The JSON shape `capability_fingerprints` reads, from an in-memory map."""
    return {
        "capabilities": [{"id": c.id, "path": c.path, **({"parent_capability_id": c.parent_capability_id}
                                                          if c.parent_capability_id else {})} for c in smap.capabilities],
        "entities": [{"id": e.id, "capability_id": e.capability_id} for e in smap.entities],
        "entry_points": [{"id": e.id, "capability_id": e.capability_id} for e in smap.entry_points],
        "flows": [{"id": f.id, "entry_point_id": f.entry_point_id, "timeline": {"steps": [{"id": s.id} for s in f.timeline.steps]}}
                  for f in smap.flows],
    }


def capability_fingerprints(doc: dict, blobs: dict[str, str], language_of: Callable[[str], str]) -> dict[str, str]:
    """doc: a system map as JSON; blobs: path -> blob SHA at the map's commit (`git ls-tree -r`)."""
    idx = group_capabilities(set(blobs), language_of)
    paths = {c["id"]: c.get("path") for c in doc["capabilities"]}
    idx.subs = {c["path"]: paths.get(c["parent_capability_id"]) or c["parent_capability_id"].removeprefix("cap:")
                for c in doc["capabilities"] if c.get("parent_capability_id") and c.get("path")}
    files: dict[str, list[str]] = {}
    for path in sorted(blobs):
        cid = idx.cap_id(path)
        if cid is not None:
            files.setdefault(cid, []).append(f"{path}@{blobs[path]}")
    return {cid: short_hash(*files.get(cid, []), "|", *ids, length=24) for cid, ids in _cap_elements(doc).items()}


@dataclass
class ReusePlan:
    donor_id: str | None = None
    reused: list[str] = field(default_factory=list)  # capabilities whose texts were copied
    ask: list[str] = field(default_factory=list)  # capabilities Claude must (re)explain
    summary_reused: bool = False
    # the donor's still-grounded summary when it is re-asked (too much changed): kept if no new summary gets written
    summary_fallback: Text | None = None
    domains_reused: bool = False
    phases_reused: list[str] = field(default_factory=list)  # flows whose phases were copied
    phases_ask: list[str] = field(default_factory=list)  # eligible flows Claude still has to cut
    dropped: int = 0
    reasons: list[str] = field(default_factory=list)

    def drop(self, why: str) -> None:
        self.dropped += 1
        if len(self.reasons) < 20:
            self.reasons.append(why[:200])


def _text(t: object) -> Text | None:
    if isinstance(t, dict) and t.get("source") == "semantic" and isinstance(t.get("text"), str) and t.get("evidence_refs"):
        return Text(t["text"], "semantic", [e for e in t["evidence_refs"] if isinstance(e, str)])
    return None


def reuse_texts(smap: SystemMap, donor: dict | None, new_fp: dict[str, str], donor_fp: dict[str, str]) -> ReusePlan:
    """Copy grounded texts from `donor` (JSON) into the static `smap` in place; returns what Claude still has to write."""
    caps = [c.id for c in smap.capabilities if c.category != "engineering"]
    plan = ReusePlan(donor_id=donor.get("system_map_id") if donor else None)
    if not donor:
        plan.ask = caps
        plan.phases_ask = [f.id for f in phase_flows(smap)]
        return plan
    evidence = {e.id for e in smap.evidence}

    def valid(t: Text | None, what: str) -> Text | None:
        if t is None:
            return None
        missing = [e for e in t.evidence_refs if e not in evidence]
        if missing or not t.evidence_refs:
            plan.drop(f"reused {what}: evidence no longer exists ({', '.join(missing[:2])})")
            return None
        return t

    old = {el["id"]: el for k in ("capabilities", "entities", "entry_points", "flows") for el in donor.get(k, [])}
    old_steps = {s["id"]: s for f in donor.get("flows", []) for s in f["timeline"]["steps"]}
    ep_cap = {ep.id: ep.capability_id for ep in smap.entry_points}
    members: dict[str, list] = {c.id: [c] for c in smap.capabilities}
    for el in [*smap.entities, *smap.entry_points]:
        members.setdefault(el.capability_id, []).append(el)
    steps: dict[str, list] = {}
    for f in smap.flows:
        cap = ep_cap.get(f.entry_point_id)
        if cap in members:
            members[cap].append(f)
            steps.setdefault(cap, []).extend(f.timeline.steps)

    for cid in caps:
        prev = old.get(cid)
        if prev is None or not prev.get("business_name") or new_fp.get(cid) is None or new_fp.get(cid) != donor_fp.get(cid):
            plan.ask.append(cid)
            continue
        for el in members[cid]:
            o = old.get(el.id)
            if o is None:
                continue
            el.business_name = valid(_text(o.get("business_name")), f"{el.id}.business_name") or el.business_name
            el.description = valid(_text(o.get("description")), f"{el.id}.description") or el.description
        for s in steps.get(cid, []):
            o = old_steps.get(s.id)
            if o is not None:
                s.business_label = valid(_text(o.get("business_label")), f"{s.id}.business_label") or s.business_label
        cap = members[cid][0]
        if cap.business_name is None:
            plan.ask.append(cid)  # its own name did not survive validation: Claude rewrites the whole capability
        else:
            plan.reused.append(cid)

    old_flows = {f["id"]: f for f in donor.get("flows", []) if isinstance(f, dict)}
    for f in phase_flows(smap):
        o = old_flows.get(f.id) or {}
        sids = [s.id for s in f.timeline.steps]
        if o.get("phases") and [s["id"] for s in o["timeline"]["steps"]] == sids:
            phases = [Phase(p["id"], lab, list(p["step_ids"])) for p in o["phases"]
                      if (lab := valid(_text(p.get("label")), f"{p['id']}.label"))]
            if len(phases) == len(o["phases"]) and [x for p in phases for x in p.step_ids] == sids:
                f.phases = phases
                plan.phases_reused.append(f.id)
                continue
        plan.phases_ask.append(f.id)
    plan.domains_reused, dropped, reasons = reuse_domains(smap, donor)
    plan.dropped += dropped
    plan.reasons += reasons[:5]

    changed = len(plan.ask) / len(caps) if caps else 0.0
    summary = _text((donor.get("system") or {}).get("summary"))
    if summary is not None and changed <= SUMMARY_REUSE_MAX_CHANGED:
        kept = valid(summary, "system_summary")
        if kept is not None:
            smap.system.summary = kept
            plan.summary_reused = True
    elif summary is not None and summary.evidence_refs and all(e in evidence for e in summary.evidence_refs):
        plan.summary_fallback = summary
    return plan


__all__ = ["ReusePlan", "SUMMARY_REUSE_MAX_CHANGED", "capability_fingerprints", "reuse_texts", "skeleton"]
