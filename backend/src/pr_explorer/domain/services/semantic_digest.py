"""What Claude sees (pure). Two digests: one per capability batch of the system map (call a) and one for the PR
(call b). Only IDs, technical names, statuses and short doc/diff excerpts — Claude writes text about them and must
cite these IDs. Everything here ends up inside the prompt's untrusted-data fence."""

from __future__ import annotations

import json
import re
from typing import Any

from ..model.convention import DocExcerpt
from ..model.document import FlowChange, Impact, SystemMap
from ..model.finding import Finding
from ..model.graph import SideGraph

CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
MODULES_PER_CAP = 12
# Elements per capability Claude is asked to write about; the rest keep their technical names (the UI lists them). A
# capability's entry points that start a built flow come first. (A large repo can have a capability with very many entry points.)
ENTRY_POINTS_PER_CAP = 25
ENTITIES_PER_CAP = 30


def detect_language(*texts: str) -> str:
    """zh-Hant when the repository docs / PR text are written in Chinese, else en (spec: business language)."""
    return "zh-Hant" if sum(len(CJK.findall(t or "")) for t in texts) >= 20 else "en"


def capability_batches(smap: SystemMap, size: int = 15, only: list[str] | None = None, sg: SideGraph | None = None,
                       char_budget: int | None = None, partial: dict[str, list[str]] | None = None) -> list[list[str]]:
    """`only`: the capabilities that still need texts (incremental reuse); [[]] = a summary-only call.
    With `sg` + `char_budget`, a batch also closes once its capabilities' own digest parts (capability, entities, entry
    points, flows and steps — what Claude has to write about) reach `char_budget` characters: a batch's reply time grows
    with that, not with the number of capabilities (a single capability with very many entry points can time out alone).
    `partial`: capabilities of which only some elements are re-asked (see `system_map_digest`)."""
    keep = set(only) if only is not None else None
    caps = [c.id for c in smap.capabilities if keep is None or c.id in keep]
    if sg is None or char_budget is None:
        return [caps[i: i + size] for i in range(0, len(caps), size)] or [[]]
    cost = capability_costs(smap, sg, caps, partial)
    out: list[list[str]] = []
    cur: list[str] = []
    used = 0
    for cid in caps:
        if cur and (len(cur) >= size or used + cost[cid] > char_budget):
            out.append(cur)
            cur, used = [], 0
        cur.append(cid)
        used += cost[cid]
    return out + ([cur] if cur else []) or [[]]


def capability_costs(smap: SystemMap, sg: SideGraph, caps: list[str], partial: dict[str, list[str]] | None = None) -> dict[str, int]:
    """Characters of each capability's own part of a system-map digest."""
    out = {}
    for cid in caps:
        d = system_map_digest(smap, sg, [cid], [], first=False, partial=partial)
        out[cid] = sum(len(json.dumps(d[k], ensure_ascii=False, separators=(",", ":"))) for k in ("capabilities", "entities", "entry_points", "flows"))
    return out


def system_map_digest(smap: SystemMap, sg: SideGraph, batch: list[str], docs: list[tuple[str, DocExcerpt]],
                      first: bool, doc_budget: int = 12_000, write_summary: bool | None = None,
                      partial: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """docs: (evidence id, excerpt). The first batch also asks for the system summary (unless it was reused).
    `partial`: capability -> the only elements (entity / entry point / flow ids) of it to show: their reused texts
    cited code that changed, the capability's other texts are kept. A flow brings its entry point along as context."""
    in_batch = set(batch)
    part = {c: set(v) for c, v in (partial or {}).items() if c in in_batch}
    ep_of_flow = {f.id: f.entry_point_id for f in smap.flows}
    part_eps = {c: v | {ep_of_flow[x] for x in v if x in ep_of_flow} for c, v in part.items()}
    modules: dict[str, list[str]] = {}
    for c in smap.components:
        if c.kind == "module" and c.capability_id in in_batch and len(modules.setdefault(c.capability_id, [])) < MODULES_PER_CAP:
            modules[c.capability_id].append(c.technical_name)
    flow_eps = {f.entry_point_id for f in smap.flows}
    eps_of: dict[str, list] = {}
    for ep in smap.entry_points:
        if ep.capability_id in in_batch and (ep.capability_id not in part or ep.id in part_eps[ep.capability_id]):
            eps_of.setdefault(ep.capability_id, []).append(ep)
    shown_eps: set[str] = set()
    for cid, lst in eps_of.items():
        lst.sort(key=lambda ep: (ep.id not in flow_eps, ep.id))
        shown_eps |= {ep.id for ep in lst[:ENTRY_POINTS_PER_CAP]}
    ents_of: dict[str, list] = {}
    for e in smap.entities:
        if e.capability_id in in_batch and (e.capability_id not in part or e.id in part[e.capability_id]):
            ents_of.setdefault(e.capability_id, []).append(e)
    shown_ents = {e.id for lst in ents_of.values() for e in lst[:ENTITIES_PER_CAP]}
    caps = [{
        "id": c.id, "technical_name": c.technical_name, "path": c.path, "language": c.language,
        "modules": modules.get(c.id, []), "entity_ids": [x for x in c.entity_ids if x in shown_ents],
        "entry_point_ids": [x for x in c.entry_point_ids if x in shown_eps],
        **({"entry_points_total": len(c.entry_point_ids)} if len(c.entry_point_ids) > ENTRY_POINTS_PER_CAP else {}),
        **({"entities_total": len(c.entity_ids)} if len(c.entity_ids) > ENTITIES_PER_CAP else {}),
    } for c in smap.capabilities if c.id in in_batch]
    ents = []
    for e in smap.entities:
        if e.id not in shown_ents:
            continue
        node = sg.nodes.get(e.meta.get("node_id", ""))
        ents.append({"id": e.id, "technical_name": e.technical_name, "capability_id": e.capability_id,
                     "fields": sorted((node.meta.get("fields") or {}) if node else {})[:20]})
    eps = [{"id": ep.id, "kind": ep.kind, "technical_name": ep.technical_name, "capability_id": ep.capability_id}
           for ep in smap.entry_points if ep.id in shown_eps]
    ep_ids = {ep["id"] for ep in eps}
    flows = []
    cap_of_ep = {ep["id"]: ep["capability_id"] for ep in eps}
    for f in smap.flows:
        if f.entry_point_id not in ep_ids or (cap_of_ep[f.entry_point_id] in part and f.id not in part[cap_of_ep[f.entry_point_id]]):
            continue
        flows.append({
            "id": f.id, "technical_name": f.technical_name, "entry_point_id": f.entry_point_id,
            "capability_ids": f.capability_ids, "entity_ids": f.entity_ids,
            "steps": [{"id": s.id, "kind": s.kind, "capability_id": s.capability_id, "entity_id": s.entity_id,
                       "technical_label": s.technical_label, "evidence_status": s.evidence_status,
                       **({"condition": s.branch.condition[:160]} if s.branch else {})} for s in f.timeline.steps],
        })
    rels = [{"source": r.source, "target": r.target, "kind": r.kind, "weight": r.weight}
            for r in smap.relations if r.source in in_batch or r.target in in_batch][:80]
    out_docs, left = [], doc_budget if first else doc_budget // 3
    for ev_id, d in docs:
        if left <= 0:
            break
        out_docs.append({"evidence_id": ev_id, "path": d.path, "text": d.text[:left]})
        left -= len(d.text)
    return {
        "repository": smap.repo.name, "write_system_summary": first if write_summary is None else write_summary, "docs": out_docs,
        "capabilities": caps, "entities": ents, "entry_points": eps, "flows": flows, "relations": rels,
        # the summary-only call ([] batch) also sees the names / descriptions already written (e.g. reused texts)
        "other_capabilities": [{"id": c.id, "technical_name": c.technical_name,
                                **({"business_name": c.business_name.text} if c.business_name and not batch else {}),
                                **({"description": c.description.text[:200]} if c.description and not batch else {})}
                               for c in smap.capabilities if c.id not in in_batch and c.category != "engineering"][:80],
    }


def digest_targets(smap: SystemMap, batch: list[str], partial: dict[str, list[str]] | None = None) -> set[str]:
    """IDs a system-map call for `batch` asks Claude to write about: every element of a capability explained as a
    whole (itself, its entities, entry points, flows by entry point and their steps), only the listed elements (a flow
    with its steps) of a `partial` capability."""
    in_batch = set(batch)
    part = {c: set(v) for c, v in (partial or {}).items() if c in in_batch}
    out = {c for c in in_batch if c not in part} | {x for v in part.values() for x in v}
    ep_cap = {}
    for el in [*smap.entities, *smap.entry_points]:
        if el.capability_id in in_batch and el.capability_id not in part:
            out.add(el.id)
    for ep in smap.entry_points:
        ep_cap[ep.id] = ep.capability_id
    for f in smap.flows:
        cap = ep_cap.get(f.entry_point_id)
        if cap in in_batch and (cap not in part or f.id in part[cap]):
            out.add(f.id)
            out.update(s.id for s in f.timeline.steps)
    return out


def restrict_reply(reply: object, allowed: set[str], known: set[str]) -> object:
    """Keep only what a system-map call was asked about: an item naming another known map element (one this call did
    not ask for — its text is kept from an earlier map or written by its own call) is left out. Unknown IDs stay, so
    the gate still counts them as dropped claims. The system summary is untouched."""
    if not isinstance(reply, dict):
        return reply
    out = dict(reply)
    for key in ("capabilities", "entities", "entry_points", "flows", "steps"):
        items = reply.get(key)
        if isinstance(items, list):
            out[key] = [it for it in items if not (isinstance(it, dict) and isinstance(it.get("id"), str)
                                                   and it["id"] in known and it["id"] not in allowed)]
    return out


def phase_batches(smap: SystemMap, flow_ids: list[str], size: int = 10, char_budget: int = 24_000) -> list[list[str]]:
    """Flows per phase call: at most `size`, and at most `char_budget` characters of flow data (one long flow per call
    when it alone exceeds the budget)."""
    out: list[list[str]] = []
    cur: list[str] = []
    used = 0
    for fid in flow_ids:
        cost = len(json.dumps(phase_digest(smap, [fid])["flows"], ensure_ascii=False, separators=(",", ":")))
        if cur and (len(cur) >= size or used + cost > char_budget):
            out.append(cur)
            cur, used = [], 0
        cur.append(fid)
        used += cost
    return out + ([cur] if cur else [])


def phase_digest(smap: SystemMap, flow_ids: list[str]) -> dict[str, Any]:
    """The flows Claude cuts into phases (call kind `phases`): ordered steps with their labels."""
    want = set(flow_ids)
    return {"repository": smap.repo.name, "flows": [{
        "id": f.id, "technical_name": f.technical_name, **({"business_name": f.business_name.text} if f.business_name else {}),
        "steps": [{"id": s.id, "kind": s.kind, "capability_id": s.capability_id, "technical_label": s.technical_label,
                   **({"business_label": s.business_label.text} if s.business_label else {}),
                   "evidence_status": s.evidence_status} for s in f.timeline.steps],
    } for f in smap.flows if f.id in want]}


TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|e2e|spec)/|(^|/)test_[^/]*$|_test\.py$|\.(spec|test)\.[jt]sx?$")


def code_first(diff: str) -> str:
    """The unified diff with test files moved to the end, so the excerpt budget is spent on the behaviour change
    (a real PR whose added lines were two-thirds tests pushed the views out of the deep-tier excerpt)."""
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    head = [x for x in parts if not x.startswith("diff --git ")]
    files = [x for x in parts if x.startswith("diff --git ")]
    is_test = lambda x: bool(TEST_PATH.search(x.split("\n", 1)[0].rsplit(" b/", 1)[-1]))  # noqa: E731
    return "".join(head + [x for x in files if not is_test(x)] + [x for x in files if is_test(x)])


def relevant_rules(rules: list[dict], touched: set[str], checks: list, limit: int = 40) -> list[dict]:
    """Architecture rules worth sending with a PR: those a static check of this PR cites, then those scoped to a
    capability the PR touches. Never the whole list (a large repo can have hundreds of rules)."""
    cited = {c.rule_id for c in checks if getattr(c, "rule_id", None)}
    first = [r for r in rules if r["id"] in cited]
    scoped = [r for r in rules if r["id"] not in cited and set(r.get("scope_capability_ids") or []) & touched]
    return (first + scoped)[:limit]


BODY_BUDGET = {"light": 8_000, "standard": 20_000, "deep": 32_000}
OMITTED = "\n\n[… middle of the description omitted …]\n\n"


def clip_body(body: str, limit: int) -> str:
    """The PR description within `limit` characters. A longer one keeps its start and its end: authors put the
    settled decisions, QA status and open issues of a rework at the bottom (before 2.7.0 only the first 4000 characters
    were sent, so those records never reached the AI)."""
    if len(body) <= limit:
        return body
    head = limit * 2 // 3
    return body[:head] + OMITTED + body[-(limit - head - len(OMITTED)):]


def pr_digest(*, title: str, body: str, pr_evidence: str, system_name: str, system_summary: str | None,
              names: dict[str, str], files: list, impacts: list[Impact], flow_changes: list[FlowChange],
              findings: list[Finding], diff_evidence: dict[str, str], diff_excerpt: str, docs: list[DocExcerpt],
              context_docs: list, tier: str, head_additions: list[dict] | None = None,
              architecture_rules: list[dict] | None = None, architecture_checks: list[dict] | None = None,
              architecture_docs: list[dict] | None = None, interface_changes: list[dict] | None = None) -> dict[str, Any]:
    cap = {"light": 40, "standard": 120, "deep": 400}.get(tier, 40)
    budget = {"light": 6_000, "standard": 20_000, "deep": 60_000}.get(tier, 6_000)
    impact_ids = {i.target_id for i in impacts}
    return {
        "pr": {"title": title, "body": clip_body(body, BODY_BUDGET.get(tier, 8_000)), "evidence_id": pr_evidence},
        "system": {"name": system_name, "summary": system_summary},
        "changed_files": [{"id": f.id, "path": f.path, "status": f.status, "capability_id": f.capability_id,
                           "evidence_id": diff_evidence.get(f.path)} for f in files[:cap]],
        "elements": [{"id": k, "name": v} for k, v in sorted(names.items()) if k in impact_ids][:cap],
        "impacts": [{"target_id": i.target_id, "target_kind": i.target_kind, "change_status": i.change_status,
                     "impact_status": i.impact_status} for i in impacts if i.target_kind != "component"][:cap],
        "flow_changes": [{
            "flow_id": fc.flow_id, "name": names.get(fc.flow_id, fc.flow_id), "change_status": fc.change_status,
            "steps": [{"id": s.id, "kind": s.kind, "capability_id": s.capability_id, "entity_id": s.entity_id,
                       "technical_label": s.technical_label, "step_change_status": s.step_change_status,
                       "evidence_status": s.evidence_status,
                       **({"business_label": s.business_label.text} if s.business_label else {}),
                       **({"condition": s.branch.condition[:160]} if s.branch else {})} for s in fc.steps],
        } for fc in flow_changes[:cap // 4 or 10]],
        # elements that exist only on the PR head: Claude names them like the system map's elements
        "head_additions": (head_additions or [])[:cap],
        "static_findings": [{"id": f.id, "title": f.title, "rule": f.rule, "capability_ids": f.capability_ids,
                             "evidence_refs": f.evidence_refs[:3]} for f in findings[:30]],
        "architecture_rules": (architecture_rules or [])[:40],
        "architecture_checks": (architecture_checks or [])[:30],  # static: already decided, do not repeat
        "architecture_docs": (architecture_docs or [])[:25],
        # static interface changes, outer ring first (spec §8): already decided; Claude says who is affected
        "interface_changes": (interface_changes or [])[:30],
        "diff_excerpt": code_first(diff_excerpt)[:budget],
        "docs": [{"path": d.path, "text": d.text[: budget // 4]} for d in docs[:3]],
        "context_docs": [{"name": d.name, "content": d.content[: budget // 4]} for d in context_docs[:10]],
    }
