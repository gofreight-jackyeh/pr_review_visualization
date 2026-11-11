"""Business domains above capabilities (spec v2 §4 System Hierarchy, Layer 7). Pure.

Static: one domain per top-level directory of the business capabilities (after their common path prefix); a directory
holding more than half of a large system is split into its sub-directories, and at most 10 static domains are kept.
Semantic: Claude assigns every existing business capability to exactly one domain and names it; the backend only
accepts an assignment that covers each business capability once, cites nothing unknown and has 2–10 domains.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model.document import Capability, Domain, SystemMap, Text
from ..model.ids import short_hash
from .semantic_gate import LIMITS, Resolver, _ids, _list, _str, system_map_elements

MIN_DOMAINS, MAX_DOMAINS = 2, 10  # accepted; the prompt asks for 6–8 (fewer for small systems)
MIN_BUSINESS_FOR_CLAUDE = 3  # fewer business capabilities: static domains, no Claude call
MAX_STATIC_DOMAINS = 10
SPLIT_DOMAIN_MIN_BUSINESS = 12  # smaller systems keep one domain per top-level directory
MERGED_KEY = "*"  # prefix of the static domains that each hold a run of single-capability directories


def business(caps: list[Capability]) -> list[Capability]:
    return [c for c in caps if c.category == "business"]


def _evidence(members: list[Capability]) -> list[str]:
    return list(dict.fromkeys(c.evidence_refs[0] for c in members if c.evidence_refs))[:6]


def static_domains(caps: list[Capability]) -> list[Domain]:
    """One domain per first directory below the business capabilities' common prefix; assigns domain_id in place.

    Large systems (≥ SPLIT_DOMAIN_MIN_BUSINESS business capabilities) whose one directory holds more than half of them
    (e.g. a monorepo that keeps all its apps under one directory, putting most capabilities in a single domain) use that directory's sub-directories instead
    (one level; its own files and its `<dir>/*` merge stay as `<dir>`). Then single-capability directories are merged
    into balanced "a, b, c +N" domains so there are at most MAX_STATIC_DOMAINS."""
    members = business(caps)
    for c in caps:
        c.domain_id = None
    if not members:
        return []
    split = [c.path.split("/") for c in members]
    common = 0
    while all(len(p) > common + 1 for p in split) and len({p[common] for p in split}) == 1:
        common += 1
    keys = ["/".join(parts[: common + 1]) for parts in split]
    sizes: dict[str, int] = {}
    for k in keys:
        sizes[k] = sizes.get(k, 0) + 1
    big = max(sorted(sizes), key=lambda k: sizes[k])
    if len(members) >= SPLIT_DOMAIN_MIN_BUSINESS and sizes[big] * 2 > len(members):
        keys = ["/".join(parts[: common + 2]) if k == big and len(parts) > common + 1 and parts[common + 1] != "*" else k
                for k, parts in zip(keys, split)]
    groups: dict[str, list[Capability]] = {}
    for c, k in zip(members, keys):
        groups.setdefault(k, []).append(c)
    merged_names: dict[str, str] = {}
    if len(groups) > MAX_STATIC_DOMAINS:
        # directories holding several capabilities stay domains (biggest first); the single-capability ones fill the
        # remaining slots as balanced alphabetical runs ("billing, catalog, inventory +4"), so no catch-all domain
        # ends up holding most of the system
        rank = sorted(groups, key=lambda k: (-len(groups[k]), -sum(len(c.entry_point_ids) for c in groups[k]), k))
        keep = [k for k in rank if len(groups[k]) > 1][: MAX_STATIC_DOMAINS - 1]
        small = sorted(k for k in groups if k not in keep)
        slots = MAX_STATIC_DOMAINS - len(keep)
        sizes = [len(small) // slots + (1 if i < len(small) % slots else 0) for i in range(slots)]
        start = 0
        for size in (x for x in sizes if x):
            run, start = small[start: start + size], start + size
            key = f"{MERGED_KEY}:{run[0]}"
            groups[key] = [c for k in run for c in groups.pop(k)]
            names = [k.rsplit("/", 1)[-1] for k in run]
            merged_names[key] = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    out: list[Domain] = []
    for order, key in enumerate(sorted(groups, key=lambda k: (k in merged_names, k))):
        did = f"domain:{key}"
        for c in groups[key]:
            c.domain_id = did
        out.append(Domain(did, merged_names.get(key) or key.rsplit("/", 1)[-1], "static", order, _evidence(groups[key])))
    return out


@dataclass
class DomainGate:
    domains: list[Domain] = field(default_factory=list)
    assignment: dict[str, str] = field(default_factory=dict)  # capability id -> domain id
    problems: list[str] = field(default_factory=list)  # structural violations: the whole reply is rejected
    dropped: int = 0
    reasons: list[str] = field(default_factory=list)


def gate_domains(smap: SystemMap, payload: object) -> DomainGate:
    """Validate Claude's domain assignment. Any structural problem rejects the whole reply (caller retries once, then
    falls back to static domains); texts are evidence-gated like every other Claude statement."""
    out = DomainGate()
    biz = {c.id: c for c in business(smap.capabilities)}
    every = {c.id for c in smap.capabilities}
    items = _list(payload.get("domains")) if isinstance(payload, dict) else []
    if not MIN_DOMAINS <= len(items) <= MAX_DOMAINS:
        out.problems.append(f"expected {MIN_DOMAINS}–{MAX_DOMAINS} domains, got {len(items)}")
    seen: dict[str, int] = {}
    for i, d in enumerate(items):
        caps = _ids(d.get("capability_ids"))
        if not caps:
            out.problems.append(f"domain {i} has no capabilities")
        if not _str(d.get("name"), LIMITS["name"]):
            out.problems.append(f"domain {i} has no name")
        for c in caps:
            if c not in every:
                out.problems.append(f"domain {i}: unknown capability {c[:80]}")
            elif c not in biz:
                out.problems.append(f"domain {i}: {c[:80]} is not a business capability")
            elif c in seen:
                out.problems.append(f"{c[:80]} is assigned to domains {seen[c]} and {i}")
            else:
                seen[c] = i
    missing = sorted(set(biz) - set(seen))
    if missing:
        out.problems.append(f"{len(missing)} business capabilit{'y' if len(missing) == 1 else 'ies'} not assigned: "
                            + ", ".join(missing[:5]))
    if out.problems:
        out.problems = out.problems[:12]
        return out
    r = Resolver(system_map_elements(smap), {e.id for e in smap.evidence})
    for order, d in enumerate(items):
        caps = _ids(d.get("capability_ids"))
        known = [c for c in _ids(d.get("cites")) if c in r.elements or c in r.evidence]
        if len(known) < len(_ids(d.get("cites"))):
            r.drop(f"domain {order}: ignored unknown cite(s)")
        cites = caps + known
        did = f"domain:semantic:{short_hash(*sorted(caps), length=12)}"
        members = [biz[c] for c in caps]
        tech = ", ".join(c.technical_name for c in members[:3]) + (f" +{len(members) - 3}" if len(members) > 3 else "")
        dom = Domain(did, tech, "semantic", order, _evidence(members),
                     business_name=r.text(d.get("name"), cites, f"{did}.name", LIMITS["name"]),
                     description=r.text(d.get("description"), cites, f"{did}.description", LIMITS["description"]))
        out.domains.append(dom)
        out.assignment.update({c: did for c in caps})
    out.dropped, out.reasons = r.dropped, r.reasons
    return out


def apply_domains(smap: SystemMap, gate: DomainGate) -> None:
    smap.domains = gate.domains
    for c in smap.capabilities:
        c.domain_id = gate.assignment.get(c.id)


def domain_digest(smap: SystemMap, docs: list[tuple[str, object]], doc_budget: int = 6_000) -> dict:
    """What Claude sees for the domain step: business capabilities (names, paths, sizes) and their relations."""
    biz = business(smap.capabilities)
    ids = {c.id for c in biz}
    caps = [{"id": c.id, "technical_name": c.technical_name, "path": c.path,
             **({"business_name": c.business_name.text} if c.business_name else {}),
             **({"description": c.description.text} if c.description else {}),
             **({"parent_capability_id": c.parent_capability_id} if c.parent_capability_id else {}),
             "entry_points": len(c.entry_point_ids), "entities": len(c.entity_ids)} for c in biz]
    rels = sorted((r for r in smap.relations if r.source in ids and r.target in ids), key=lambda r: -r.weight)[:150]
    out_docs, left = [], doc_budget
    for ev_id, d in docs:
        if left <= 0:
            break
        out_docs.append({"evidence_id": ev_id, "path": d.path, "text": d.text[:left]})
        left -= len(d.text)
    return {"repository": smap.repo.name, "business_capabilities": caps,
            "relations": [{"source": r.source, "target": r.target, "weight": r.weight} for r in rels], "docs": out_docs,
            "domain_count": "6-8" if len(biz) >= 12 else f"2-{max(2, min(6, len(biz) // 2))}"}


def reuse_domains(smap: SystemMap, donor: dict | None) -> tuple[bool, int, list[str]]:
    """Copy the donor's Claude domains when its set of business capabilities equals this map's (re-validating every
    text's evidence). Returns (reused, dropped texts, reasons)."""
    if not donor:
        return False, 0, []
    doms = [d for d in donor.get("domains", []) if isinstance(d, dict) and d.get("source") == "semantic"]
    assign = {c["id"]: c.get("domain_id") for c in donor.get("capabilities", []) if c.get("category") == "business"}
    biz = {c.id for c in business(smap.capabilities)}
    if not doms or set(assign) != biz or any(v is None for v in assign.values()):
        return False, 0, []
    known = {d.get("id") for d in doms}
    if any(v not in known for v in assign.values()):
        return False, 0, []
    evidence = {e.id for e in smap.evidence}
    dropped, reasons = 0, []

    def text(t: object, what: str) -> Text | None:
        nonlocal dropped
        if not (isinstance(t, dict) and t.get("source") == "semantic" and isinstance(t.get("text"), str)):
            return None
        refs = [e for e in t.get("evidence_refs", []) if isinstance(e, str)]
        if not refs or any(e not in evidence for e in refs):
            dropped += 1
            reasons.append(f"reused {what}: evidence no longer exists")
            return None
        return Text(t["text"], "semantic", refs)

    by_id = {c.id: c for c in smap.capabilities}
    out = []
    for order, d in enumerate(sorted(doms, key=lambda d: d.get("order", 0))):
        members = [by_id[c] for c, v in sorted(assign.items()) if v == d["id"]]
        out.append(Domain(d["id"], str(d.get("technical_name") or d["id"]), "semantic", order, _evidence(members),
                          business_name=text(d.get("business_name"), f"{d['id']}.business_name"),
                          description=text(d.get("description"), f"{d['id']}.description")))
    smap.domains = out
    for c in smap.capabilities:
        c.domain_id = assign.get(c.id)
    return True, dropped, reasons


__all__ = ["DomainGate", "apply_domains", "business", "domain_digest", "gate_domains", "reuse_domains", "static_domains"]
