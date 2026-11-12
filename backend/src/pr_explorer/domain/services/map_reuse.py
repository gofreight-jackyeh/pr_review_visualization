"""Incremental system-map explanation (pure): reuse Claude-written texts from an earlier map of the same repository.

The static map is always rebuilt for the PR's merge base; only the Claude texts are carried over. A capability's texts
(its own, its entities', entry points', flows' and their steps') are kept unless one of these holds, in which case
Claude re-explains the capability as a whole (`ReusePlan.ask`):

- its business-facing *surface* changed: name / path / category, entry points, entities and their fields, interfaces,
  built flows and their step structure, relations (`capability_surfaces`; stored per map in `text_basis.surface`).
  A change to implementation only keeps the texts. A donor stored before schema 2.5.0 has no surface: there the
  earlier rule applies (the git blob SHAs of the capability's files plus its element IDs, `capability_fingerprints`);
- its texts are stale (`ReuseLimits`): too many of its code lines changed since Claude wrote them, or they were carried
  across too many base commits, or they are too old;
- its own business name does not survive re-validation (below), or it is new.

Every reused text is re-validated against the new map: all of its evidence must exist. Evidence IDs carry line
numbers, so a cited ID that no longer exists is remapped across the base change with the diff hunks between the
donor's commit and the new base (`BaseChange`: same file, shifted lines, the cited range itself untouched). A text
whose cited range was modified, or whose file moved or was deleted, is dropped and only its element (an entity, an
entry point, or a flow with its steps) is re-asked (`ReusePlan.partial`).

Also carried over: a flow's phases when its step IDs are unchanged, the Claude domain assignment when the set of
business capabilities is unchanged, and the system summary while at most a quarter of the capabilities is re-explained
as a whole. Engineering capabilities are never sent to Claude (hidden by default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..model.changes import ChangedFile
from ..model.document import Phase, SystemMap, Text, TextBasis
from ..model.graph import SideGraph
from ..model.ids import short_hash
from .semantic_digest import detect_language
from .domains import reuse_domains
from .semantic_gate import phase_flows
from .system_structure import CapabilityIndex, group_capabilities

SUMMARY_REUSE_MAX_CHANGED = 0.25  # re-ask the system summary when more than this share of capabilities is re-explained
SURFACE_VERSION = "surface-1"  # part of every surface fingerprint: changing what it covers re-asks once, never silently
CODE_LANGS = frozenset({"python", "typescript", "javascript"})  # files whose lines count towards the staleness share


@dataclass(frozen=True)
class ReuseLimits:
    """Staleness guard (PipelineConfig.reuse_*): a capability's texts are re-explained as a whole, whatever its surface,
    once more than `max_changed_share` of its code lines changed since Claude wrote them (summed over every base change
    they were carried across), once they were carried across more than `max_base_changes` base commits, or once they
    are older than `max_age_days`."""

    max_changed_share: float = 0.4
    max_base_changes: int = 40
    max_age_days: float = 14.0


# ---------------------------------------------------------------- what changed between the donor and the new base

@dataclass
class BaseChange:
    """The diff between the donor map's commit and the new base (`git diff -M -U0 donor base`)."""

    same_commit: bool = False
    hunks: dict[str, list[tuple[int, int, int, int]]] = field(default_factory=dict)  # modified path -> sorted hunks
    gone: set[str] = field(default_factory=set)  # paths deleted or renamed away (their evidence cannot move)
    changed: dict[str, int] = field(default_factory=dict)  # path -> lines added + removed
    deleted: set[str] = field(default_factory=set)

    @classmethod
    def from_files(cls, files: list[ChangedFile], same_commit: bool = False) -> "BaseChange":
        b = cls(same_commit)
        for f in files:
            b.changed[f.path] = f.additions + f.deletions
            if f.status == "added":
                continue
            if f.status == "removed":
                b.gone.add(f.path)
                b.deleted.add(f.path)
                continue
            if f.old_path and f.old_path != f.path:
                b.gone.add(f.old_path)
                continue
            b.hunks[f.path] = sorted((h.old_start, h.old_lines, h.new_start, h.new_lines) for h in f.hunks)
        return b


def shift_range(hunks: list[tuple[int, int, int, int]], start: int, end: int) -> int | None:
    """New first line of the old line range [start, end] of a file, or None when a hunk changed a line inside it
    (or inserted lines into it). `hunks` must be zero-context (-U0) and sorted."""
    delta = 0
    for old_start, old_lines, _new_start, new_lines in hunks:
        if old_lines == 0:  # pure insertion after old line `old_start`
            if old_start < start:
                delta += new_lines
                continue
            if old_start >= end:
                break
            return None
        if old_start + old_lines - 1 < start:
            delta += new_lines - old_lines
            continue
        if old_start > end:
            break
        return None
    return start + delta


def _parse_evidence_id(eid: str) -> tuple[str, str, int, str] | None:
    """`ev:<kind>:<side>:<path>:<line>:<hash>` → ('ev:<kind>:<side>:', path, line, hash); None for any other ID."""
    if not eid.startswith("ev:") or eid.count(":") < 5:
        return None
    head, line, h = eid.rsplit(":", 2)
    parts = head.split(":", 3)  # ev, kind, side, path
    if not line.isdigit() or not h or not parts[3]:
        return None
    return f"ev:{parts[1]}:{parts[2]}:", parts[3], int(line), h


class EvidenceRemap:
    """Re-validates reused texts: keeps cited evidence that still exists, moves line-based evidence IDs whose cited
    range was only shifted by the base change, and rejects the rest. Counts what it did (`kept`, `moved`, `failed`)."""

    def __init__(self, new_evidence: set[str], donor_evidence: dict[str, tuple[str, int, int]] | None = None,
                 change: BaseChange | None = None):
        self.new = new_evidence
        self.donor = donor_evidence or {}
        self.change = change
        self.kept = self.moved = self.failed = 0

    def ref(self, eid: str) -> str | None:
        if eid in self.new:
            return eid
        if self.change is None:
            return None
        p = _parse_evidence_id(eid)
        if p is None:
            return None
        prefix, path, line, h = p
        if path in self.change.gone:
            return None
        _p, start, end = self.donor.get(eid, (path, line, line))
        new_start = shift_range(self.change.hunks.get(path, []), start, max(end, start))
        if new_start is None:
            return None
        cand = f"{prefix}{path}:{line + new_start - start}:{h}"
        return cand if cand in self.new else None

    def text(self, t: Text | None) -> Text | None:
        """The text with every cited evidence ID valid in the new map, or None (then it is not reused)."""
        if t is None:
            return None
        out = [self.ref(e) for e in t.evidence_refs]
        if not t.evidence_refs or any(x is None for x in out):
            self.failed += 1
            return None
        if out == t.evidence_refs:
            self.kept += 1
        else:
            self.moved += 1
        return Text(t.text, t.source, list(dict.fromkeys(x for x in out if x)))


def donor_evidence(donor: dict) -> dict[str, tuple[str, int, int]]:
    """evidence id -> (path, start, end) of its first code reference in a stored map."""
    out = {}
    for e in donor.get("evidence", []):
        refs = (e.get("refs") or []) if isinstance(e, dict) else []
        if refs and isinstance(refs[0], dict) and isinstance(refs[0].get("start_line"), int):
            r = refs[0]
            out[e["id"]] = (r.get("path", ""), r["start_line"], r.get("end_line") or r["start_line"])
    return out


# ---------------------------------------------------------------- per-capability fingerprints

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
    """The JSON shape `capability_fingerprints` / `capability_lines` read, from an in-memory map."""
    return {
        "capabilities": [{"id": c.id, "path": c.path, **({"parent_capability_id": c.parent_capability_id}
                                                          if c.parent_capability_id else {})} for c in smap.capabilities],
        "entities": [{"id": e.id, "capability_id": e.capability_id} for e in smap.entities],
        "entry_points": [{"id": e.id, "capability_id": e.capability_id} for e in smap.entry_points],
        "flows": [{"id": f.id, "entry_point_id": f.entry_point_id, "timeline": {"steps": [{"id": s.id} for s in f.timeline.steps]}}
                  for f in smap.flows],
    }


def _index(doc: dict, files: set[str], language_of: Callable[[str], str]) -> CapabilityIndex:
    idx = group_capabilities(files, language_of)
    paths = {c["id"]: c.get("path") for c in doc["capabilities"]}
    idx.subs = {c["path"]: paths.get(c["parent_capability_id"]) or c["parent_capability_id"].removeprefix("cap:")
                for c in doc["capabilities"] if c.get("parent_capability_id") and c.get("path")}
    return idx


def capability_fingerprints(doc: dict, blobs: dict[str, str], language_of: Callable[[str], str]) -> dict[str, str]:
    """Pre-2.5.0 donors only. doc: a system map as JSON; blobs: path -> blob SHA at the map's commit (`git ls-tree -r`)."""
    idx = _index(doc, set(blobs), language_of)
    files: dict[str, list[str]] = {}
    for path in sorted(blobs):
        cid = idx.cap_id(path)
        if cid is not None:
            files.setdefault(cid, []).append(f"{path}@{blobs[path]}")
    return {cid: short_hash(*files.get(cid, []), "|", *ids, length=24) for cid, ids in _cap_elements(doc).items()}


def capability_surfaces(smap: SystemMap, sg: SideGraph | None) -> dict[str, str]:
    """capability id -> fingerprint of what is business-facing about it (what the system-map digest shows Claude about
    the capability, minus implementation detail such as module lists and relation weights): its id, path, technical
    name, category and split parent; its entities with their field names; its entry points; its interfaces; its built
    flows with each step's identity, kind, capability, entity, technical label, evidence status and branch condition;
    the kinds of its relations to other capabilities."""
    head: dict[str, list[str]] = {c.id: [SURFACE_VERSION, c.id, c.path, c.technical_name, c.category,
                                         c.parent_capability_id or ""] for c in smap.capabilities}
    items: dict[str, list[str]] = {c.id: [] for c in smap.capabilities}

    def add(cid: str | None, *part: object) -> None:
        if cid in items:
            items[cid].append(repr(part))

    nodes = sg.nodes if sg is not None else {}
    for e in smap.entities:
        node = nodes.get(e.meta.get("node_id", ""))
        add(e.capability_id, "entity", e.id, e.technical_name, tuple(sorted((node.meta.get("fields") or {}) if node else {})))
    ep_cap = {}
    for ep in smap.entry_points:
        ep_cap[ep.id] = ep.capability_id
        add(ep.capability_id, "entry_point", ep.id, ep.kind, ep.technical_name)
    for i in smap.interfaces:
        add(i.capability_id, "interface", i.id, i.ring, i.kind, i.technical_name)
    for f in smap.flows:
        add(ep_cap.get(f.entry_point_id), "flow", f.id, f.technical_name, f.entry_point_id, tuple(
            (s.id, s.kind, s.capability_id, s.entity_id or "", s.technical_label, s.evidence_status,
             s.branch.condition[:160] if s.branch else "") for s in f.timeline.steps))
    for r in smap.relations:
        for cid in {r.source, r.target}:
            add(cid, "relation", r.source, r.target, r.kind)
    return {cid: short_hash(*head[cid], *sorted(items[cid]), length=24) for cid in head}


def capability_lines(doc: dict, files: set[str], base_lines: dict[str, int], change: BaseChange | None,
                     language_of: Callable[[str], str]) -> tuple[dict[str, int], dict[str, int]]:
    """Per capability: lines of its code files at the new base (`base_lines`: path -> line count of every file the
    analyzers read), and lines of them added / removed since the donor (deleted code files count on both sides)."""
    lines: dict[str, int] = {}
    changed: dict[str, int] = {}
    deleted = change.deleted if change else set()
    idx = _index(doc, set(files) | deleted, language_of)
    for p, n in base_lines.items():
        cid = idx.cap_id(p)
        if cid is not None:
            lines[cid] = lines.get(cid, 0) + n
    for p, n in (change.changed.items() if change else ()):
        gone = p in deleted and language_of(p) in CODE_LANGS
        if p not in base_lines and not gone:
            continue
        cid = idx.cap_id(p)
        if cid is not None:
            changed[cid] = changed.get(cid, 0) + n
            if gone:
                lines[cid] = lines.get(cid, 0) + n
    return lines, changed


# ---------------------------------------------------------------- the plan

@dataclass
class ReusePlan:
    donor_id: str | None = None
    reused: list[str] = field(default_factory=list)  # capabilities whose texts were copied (some elements may be re-asked)
    ask: list[str] = field(default_factory=list)  # capabilities Claude must (re)explain as a whole
    # capability -> its elements (entity / entry point / flow ids; a flow stands for its steps too) Claude re-explains
    # because their reused texts cited code that changed; the capability's other texts are kept
    partial: dict[str, list[str]] = field(default_factory=dict)
    why: dict[str, str] = field(default_factory=dict)  # asked capability -> new | surface | files | stale: … | evidence
    summary_reused: bool = False
    # the donor's still-grounded summary when it is re-asked (too much changed): kept if no new summary gets written
    summary_fallback: Text | None = None
    domains_reused: bool = False
    phases_reused: list[str] = field(default_factory=list)  # flows whose phases were copied
    phases_ask: list[str] = field(default_factory=list)  # eligible flows Claude still has to cut
    dropped: int = 0
    reasons: list[str] = field(default_factory=list)
    evidence_kept: int = 0  # reused texts whose evidence IDs all still exist
    evidence_moved: int = 0  # ... whose line-based evidence IDs were remapped across the base change
    evidence_failed: int = 0  # ... that could not be remapped (their element is re-asked)

    def drop(self, why: str) -> None:
        self.dropped += 1
        if len(self.reasons) < 20:
            self.reasons.append(why[:200])


def _text(t: object) -> Text | None:
    if isinstance(t, dict) and t.get("source") == "semantic" and isinstance(t.get("text"), str) and t.get("evidence_refs"):
        return Text(t["text"], "semantic", [e for e in t["evidence_refs"] if isinstance(e, str)])
    return None


def _days(earlier: str, now: str) -> float:
    try:
        a = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
        b = datetime.fromisoformat(now.replace("Z", "+00:00"))
        return (b - a).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.0


def reuse_texts(smap: SystemMap, donor: dict | None, new_fp: dict[str, str] | None = None,
                donor_fp: dict[str, str] | None = None, *, surfaces: dict[str, str] | None = None,
                change: BaseChange | None = None, cap_lines: dict[str, int] | None = None,
                cap_changed: dict[str, int] | None = None, now: str | None = None,
                limits: ReuseLimits = ReuseLimits()) -> ReusePlan:
    """Copy grounded texts from `donor` (JSON) into the static `smap` in place; returns what Claude still has to write.

    `surfaces`: this map's `capability_surfaces` (compared with each donor capability's `text_basis.surface`; donors
    without one fall back to `new_fp` / `donor_fp`, the blob fingerprints). `change`: the diff donor → new base, used
    to remap evidence (None: evidence must exist unchanged). `cap_lines` / `cap_changed` (`capability_lines`) and `now`
    feed the staleness guard. Reused capabilities get their `text_basis` carried forward."""
    caps = [c.id for c in smap.capabilities if c.category != "engineering"]
    plan = ReusePlan(donor_id=donor.get("system_map_id") if donor else None)
    if not donor:
        plan.ask = caps
        plan.why = {c: "new" for c in caps}
        plan.phases_ask = [f.id for f in phase_flows(smap)]
        return plan
    new_fp, donor_fp = new_fp or {}, donor_fp or {}
    rm = EvidenceRemap({e.id for e in smap.evidence}, donor_evidence(donor) if change is not None else None, change)

    def valid(t: Text | None, what: str) -> Text | None:
        if t is None:
            return None
        kept = rm.text(t)
        if kept is None:
            plan.drop(f"reused {what}: evidence no longer exists or its code changed ({', '.join(t.evidence_refs[:2])})")
        return kept

    old = {el["id"]: el for k in ("capabilities", "entities", "entry_points", "flows") for el in donor.get(k, [])}
    old_steps = {s["id"]: s for f in donor.get("flows", []) for s in f["timeline"]["steps"]}
    ep_cap = {ep.id: ep.capability_id for ep in smap.entry_points}
    members: dict[str, list] = {c.id: [c] for c in smap.capabilities}
    for el in [*smap.entities, *smap.entry_points]:
        members.setdefault(el.capability_id, []).append(el)
    flows_of: dict[str, list] = {}
    for f in smap.flows:
        cap = ep_cap.get(f.entry_point_id)
        if cap in members:
            members[cap].append(f)
            flows_of.setdefault(cap, []).append(f)
    moved_base = change is not None and not change.same_commit

    for cid in caps:
        prev = old.get(cid)
        basis = prev.get("text_basis") if isinstance(prev, dict) and isinstance(prev.get("text_basis"), dict) else {}
        written_at = basis.get("written_at") or donor.get("generated_at")
        written_commit = basis.get("written_commit") or donor.get("commit_sha")
        base_changes = int(basis.get("base_changes") or 0) + (1 if moved_base else 0)
        changed = int(basis.get("changed_lines") or 0) + (cap_changed or {}).get(cid, 0)
        lines = (cap_lines or {}).get(cid, 0)
        why = None
        if prev is None or not prev.get("business_name"):
            why = "new"
        elif surfaces is not None and basis.get("surface"):
            why = "surface" if surfaces.get(cid) != basis["surface"] else None
        elif new_fp.get(cid) is None or new_fp.get(cid) != donor_fp.get(cid):
            why = "files"
        if why is None and lines and changed / lines > limits.max_changed_share:
            why = f"stale: {changed}/{lines} lines changed"
        elif why is None and base_changes > limits.max_base_changes:
            why = f"stale: {base_changes} base changes"
        elif why is None and now and written_at and _days(written_at, now) > limits.max_age_days:
            why = f"stale: written {written_at[:10]}"
        if why is not None:
            plan.ask.append(cid)
            plan.why[cid] = why
            continue
        redo: list[str] = []
        for el in members[cid]:
            o = old.get(el.id)
            if o is None:
                continue
            ok = True
            for attr in ("business_name", "description"):
                t = _text(o.get(attr))
                if t is None:
                    continue
                kept = valid(t, f"{el.id}.{attr}")
                if kept is None:
                    ok = False
                else:
                    setattr(el, attr, kept)
            if not ok:
                redo.append(el.id)
        for f in flows_of.get(cid, []):
            for s in f.timeline.steps:
                o = old_steps.get(s.id)
                t = _text(o.get("business_label")) if o is not None else None
                if t is None:
                    continue
                kept = valid(t, f"{s.id}.business_label")
                if kept is not None:
                    s.business_label = kept
                elif f.id not in redo:
                    redo.append(f.id)
        cap = members[cid][0]
        if cap.business_name is None:
            plan.ask.append(cid)  # its own name did not survive validation: Claude rewrites the whole capability
            plan.why[cid] = "evidence"
            continue
        plan.reused.append(cid)
        if redo:
            plan.partial[cid] = redo
        if surfaces is not None:
            cap.text_basis = TextBasis(surfaces[cid], written_at, written_commit, base_changes, changed)

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
    domain_donor = _remapped_domains(donor, rm) if change is not None else donor
    plan.domains_reused, dropped, reasons = reuse_domains(smap, domain_donor)
    plan.dropped += dropped
    plan.reasons += reasons[:5]

    share = len(plan.ask) / len(caps) if caps else 0.0  # re-explained as a whole: surface / new / stale, not blob changes
    summary = _text((donor.get("system") or {}).get("summary"))
    if summary is not None and share <= SUMMARY_REUSE_MAX_CHANGED:
        kept = valid(summary, "system_summary")
        if kept is not None:
            smap.system.summary = kept
            plan.summary_reused = True
    elif summary is not None:
        plan.summary_fallback = EvidenceRemap(rm.new, rm.donor, rm.change).text(summary)
    plan.evidence_kept, plan.evidence_moved, plan.evidence_failed = rm.kept, rm.moved, rm.failed
    return plan


def _remapped_domains(donor: dict, rm: EvidenceRemap) -> dict:
    """The donor with its domain texts' evidence remapped where the cited code only shifted (domains are re-validated
    by `reuse_domains`, which drops a text whose evidence is missing)."""
    out = []
    for d in donor.get("domains", []):
        if not isinstance(d, dict):
            out.append(d)
            continue
        d = dict(d)
        for k in ("business_name", "description"):
            t = d.get(k)
            if isinstance(t, dict) and isinstance(t.get("evidence_refs"), list):
                refs = [rm.ref(e) if isinstance(e, str) else None for e in t["evidence_refs"]]
                if refs and all(refs):
                    d[k] = {**t, "evidence_refs": list(dict.fromkeys(refs))}
        out.append(d)
    return {**donor, "domains": out}


def donor_language(doc: dict) -> str:
    """Language an earlier map's Claude texts are written in, detected like repository text (zh-Hant / en)."""
    texts: list[str] = []

    def walk(v: object) -> None:
        if isinstance(v, dict):
            if v.get("source") == "semantic" and isinstance(v.get("text"), str):
                texts.append(v["text"])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(doc)
    return detect_language(*texts[:200])


def has_surfaces(donor: dict) -> bool:
    """True for a donor stored with surface fingerprints (schema 2.5.0+)."""
    return any(isinstance(c.get("text_basis"), dict) and c["text_basis"].get("surface") for c in donor.get("capabilities", []))


__all__ = ["BaseChange", "EvidenceRemap", "ReuseLimits", "ReusePlan", "SUMMARY_REUSE_MAX_CHANGED", "capability_fingerprints",
           "capability_lines", "capability_surfaces", "donor_language", "has_surfaces", "reuse_texts", "shift_range", "skeleton"]
