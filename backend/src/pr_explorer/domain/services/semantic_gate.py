"""Evidence gate for Claude output (spec v2 §5 Layer 7, §8). Pure.

Claude only writes text. Every statement must cite IDs that exist — element IDs (capability, entity, entry point,
flow, step, changed file...) or evidence IDs; a statement citing an unknown ID, or citing nothing, is dropped and
counted in `semantic.dropped_claims`. Cited element IDs are resolved to that element's evidence, so every stored
Text carries evidence_refs. Anything that is not text (structure, grouping, new IDs) is ignored by construction.

PR statements may also cite `file:<changed path>:<start>[-<end>]`: accepted only inside one of that file's changed
hunks (± CITE_MARGIN lines), capped at MAX_CITE_LINES; such a cite counts as citing the file. Findings and architecture
hypotheses with accepted precise cites show those locations instead of the whole cited elements' evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model.changes import ChangedFile
from ..model.convention import instruction_scope
from ..model.document import AUTHOR_NOTE_KINDS, ArchCheck, AuthorNote, BehaviorChange, InterfaceChange, Phase, SystemMap, Text
from ..model.finding import Finding
from ..model.graph import CodeRef, Evidence
from ..model.ids import parse_file_cite, short_hash

SEVERITIES = {"info", "attention", "warning"}
LIMITS = {"name": 80, "label": 240, "description": 600, "summary": 1500, "body": 1500, "title": 200}
MAX_REFS = 6
WALK_KEYS = ("system", "capabilities", "flows", "behavior", "impact", "questions")
PHASE_MIN, PHASE_MAX = 3, 7  # spec v2 §4 "Flow 呈現（主軸）"
PHASE_MIN_STEPS = 4  # shorter flows are not cut (the UI segments them by capability)
HYPOTHESIS_CHECKS = {"interface_change", "dependency_direction", "interface", "placement", "shared_mechanism", "cycle",
                     "documented_rule"}
CITE_MARGIN = 3  # lines around a hunk a precise cite may still point at (context of the change)
MAX_CITE_LINES = 30  # longer cited ranges are cut to their first MAX_CITE_LINES lines
MAX_PRECISE = 3  # own locations kept per finding / hypothesis
# what a finding is about (prompts/pr.md "focus"); stored as Finding.rule "semantic_review:<focus>"
FOCUS = ("human_decision", "release_dependency", "public_contract", "responsibility_boundary", "convention",
         "claim_needs_proof")
SEVERITY_UP = {"info": "attention", "attention": "warning", "warning": "warning"}
HYPOTHESIS_RING = {"dependency_direction": "middle", "interface": "middle", "cycle": "middle", "placement": "inner",
                   "shared_mechanism": "inner", "documented_rule": "inner"}


def _str(v: object, limit: int) -> str:
    if not isinstance(v, str):
        return ""
    v = " ".join(v.split()) if limit <= LIMITS["label"] else v.strip()
    return v[:limit]


def _ids(v: object) -> list[str]:
    return [x for x in v if isinstance(x, str) and x] if isinstance(v, list) else []


def _list(v: object) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def hunk_ranges(f: ChangedFile) -> list[tuple[int, int]]:
    """Changed line ranges of a file on the side it is read from (head; base for a removed file)."""
    removed = f.status == "removed"
    out = []
    for h in f.hunks:
        start, n = (h.old_start, h.old_lines) if removed else (h.new_start, h.new_lines)
        out.append((max(start, 1), max(start, 1) + max(n, 1) - 1))
    return out


class Resolver:
    """Turns cited IDs into evidence IDs. `elements` maps element id -> its evidence_refs. With `files` (PR statements),
    `file:<path>:<start>[-<end>]` cites inside a changed hunk are accepted as precise locations (see `split`)."""

    def __init__(self, elements: dict[str, list[str]], evidence: set[str], files: list[ChangedFile] | None = None):
        self.elements = elements
        self.evidence = evidence
        self.files = {f.id: f for f in files or []}
        self.dropped = 0
        self.reasons: list[str] = []

    def split(self, cites: list[str], what: str) -> tuple[list[str], list[CodeRef]]:
        """Precise file-line cites → (cites with each accepted one replaced by its file id, the accepted locations).
        A precise cite outside the file's changed hunks, or of an unknown file, is dropped (the statement keeps its
        other cites)."""
        if not self.files:
            return cites, []
        out: list[str] = []
        refs: list[CodeRef] = []
        for c in cites:
            got = parse_file_cite(c) if c not in self.elements else None
            if got is None:
                out.append(c)
                continue
            fid, start, end = got
            f = self.files.get(fid)
            end = min(end or start, start + MAX_CITE_LINES - 1)
            if f is None or not any(lo - CITE_MARGIN <= start and end <= hi + CITE_MARGIN for lo, hi in hunk_ranges(f)):
                self.drop(f"{what}: location {c[:120]} is not inside a changed hunk")
                continue
            side, path = f.side_path
            ref = CodeRef(side, path, start, end if end > start else None)
            if ref not in refs:
                refs.append(ref)
            if fid not in out:
                out.append(fid)
        return out, refs

    def drop(self, why: str) -> None:
        self.dropped += 1
        if len(self.reasons) < 20:
            self.reasons.append(why[:200])

    def resolve(self, cites: list[str], what: str) -> list[str] | None:
        """Evidence refs for the cited IDs, or None (and the claim is counted as dropped)."""
        cites, _ = self.split(cites, what)
        unknown = [c for c in cites if c not in self.elements and c not in self.evidence]
        if unknown:
            self.drop(f"{what}: cites unknown id(s) {', '.join(unknown[:3])}")
            return None
        out: list[str] = []
        for c in cites:
            for e in ([c] if c in self.evidence else self.elements[c]):
                if e in self.evidence and e not in out:
                    out.append(e)
        if not out:
            self.drop(f"{what}: cites nothing that exists")
            return None
        return out[:MAX_REFS]

    def text(self, value: object, cites: list[str], what: str, limit: int) -> Text | None:
        s = _str(value, limit)
        if not s:
            return None
        refs = self.resolve(cites, what)
        return Text(s, "semantic", refs) if refs else None


# ---------------------------------------------------------------- (a) system map explanation

def system_map_elements(smap: SystemMap) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for coll in (smap.capabilities, smap.entities, smap.entry_points, smap.components, smap.flows):
        for el in coll:
            out[el.id] = list(el.evidence_refs)
    for f in smap.flows:
        for s in f.timeline.steps:
            out[s.id] = list(s.evidence_refs)
    return out


def apply_system_semantics(smap: SystemMap, replies: list[dict]) -> Resolver:
    """Write grounded business names/descriptions/labels into the map in place. Returns the resolver (dropped count)."""
    r = Resolver(system_map_elements(smap), {e.id for e in smap.evidence})
    named = {el.id: el for coll in (smap.capabilities, smap.entities, smap.entry_points, smap.flows) for el in coll}
    steps = {s.id: s for f in smap.flows for s in f.timeline.steps}
    for reply in replies:
        if not isinstance(reply, dict):
            r.drop("reply is not an object")
            continue
        summ = reply.get("system_summary")
        if isinstance(summ, dict) and smap.system.summary is None:
            t = r.text(summ.get("text"), _ids(summ.get("cites")), "system_summary", LIMITS["summary"])
            if t:
                smap.system.summary = t
        for key in ("capabilities", "entities", "entry_points", "flows"):
            for item in _list(reply.get(key)):
                eid = item.get("id")
                el = named.get(eid) if isinstance(eid, str) else None
                if el is None:
                    r.drop(f"{key}: unknown element {str(eid)[:80]}")
                    continue
                cites = [eid] + _ids(item.get("cites"))
                name = r.text(item.get("business_name"), cites, f"{eid}.business_name", LIMITS["name"])
                desc = r.text(item.get("description"), cites, f"{eid}.description", LIMITS["description"])
                if name:
                    el.business_name = name
                if desc:
                    el.description = desc
        for item in _list(reply.get("steps")):
            sid = item.get("id")
            st = steps.get(sid) if isinstance(sid, str) else None
            if st is None:
                r.drop(f"steps: unknown step {str(sid)[:80]}")
                continue
            lab = r.text(item.get("business_label"), [sid] + _ids(item.get("cites")), f"{sid}.business_label", LIMITS["label"])
            if lab:
                st.business_label = lab
    return r


# ---------------------------------------------------------------- flow phases (map flows and PR flow changes)

def gate_phases(flow_id: str, step_ids: list[str], items: object, r: Resolver) -> list[Phase] | None:
    """Claude may only cut the existing step order: PHASE_MIN–PHASE_MAX phases whose step lists, concatenated, are
    exactly the flow's steps in order (contiguous, full coverage, each step once, no unknown step). Anything else drops
    every phase of this flow (one dropped claim)."""
    phases = _list(items)
    got = [sid for p in phases for sid in _ids(p.get("step_ids"))]
    labels = [_str(p.get("label"), LIMITS["label"]) for p in phases]
    if not PHASE_MIN <= len(phases) <= PHASE_MAX:
        r.drop(f"{flow_id}.phases: {len(phases)} phases, expected {PHASE_MIN}–{PHASE_MAX}")
        return None
    if got != step_ids or any(not _ids(p.get("step_ids")) for p in phases):
        unknown = [x for x in got if x not in step_ids]
        why = (f"unknown step(s) {', '.join(unknown[:2])}" if unknown else
               "steps not contiguous / not covering every step exactly once")
        r.drop(f"{flow_id}.phases: {why}")
        return None
    if not all(labels):
        r.drop(f"{flow_id}.phases: a phase has no label")
        return None
    out: list[Phase] = []
    for p, label in zip(phases, labels):
        sids = _ids(p.get("step_ids"))
        known = [c for c in _ids(p.get("cites")) if c in r.elements or c in r.evidence]
        text = r.text(label, sids + known, f"{flow_id}.phase", LIMITS["label"])
        if text is None:
            return None
        out.append(Phase(f"phase:{short_hash(flow_id, sids[0], sids[-1], length=12)}", text, sids))
    return out


def phase_flows(smap: SystemMap) -> list:
    """Built flows long enough to be cut into phases."""
    return [f for f in smap.flows if len(f.timeline.steps) >= PHASE_MIN_STEPS]


def apply_phases(smap: SystemMap, replies: list[dict], only: set[str] | None = None) -> Resolver:
    """Write validated phases into the map's flows in place (flows in `only`, default all eligible)."""
    r = Resolver(system_map_elements(smap), {e.id for e in smap.evidence})
    flows = {f.id: f for f in phase_flows(smap) if only is None or f.id in only}
    for reply in replies:
        for item in _list(reply.get("flows") if isinstance(reply, dict) else None):
            fid = item.get("flow_id")
            f = flows.get(fid) if isinstance(fid, str) else None
            if f is None:
                r.drop(f"phases: unknown or ineligible flow {str(fid)[:80]}")
                continue
            got = gate_phases(f.id, [s.id for s in f.timeline.steps], item.get("phases"), r)
            if got is not None:
                f.phases = got
    return r


# ---------------------------------------------------------------- (b) PR explanation

@dataclass
class PrKnown:
    """What a PR statement may cite: every element of the map + overlay, evidence of both documents."""

    elements: dict[str, list[str]]  # element id -> evidence refs
    evidence: dict[str, Evidence]  # id -> evidence (refs used to anchor semantic findings)
    capabilities: set[str]
    flows: set[str]
    steps: set[str]  # overlay step ids Claude may label
    files: list[ChangedFile]
    additions: set[str] = field(default_factory=set)  # head_additions element ids Claude may name
    flow_steps: dict[str, list[str]] = field(default_factory=dict)  # flow change id -> its merged step ids (phases)
    interfaces: dict[str, str] = field(default_factory=dict)  # interface id (map + head_additions) -> ring
    interface_checks: set[str] = field(default_factory=set)  # static interface_change check ids Claude may annotate
    pr_body: str = ""  # the PR description: author_record quotes must occur in it
    pr_evidence: str = ""  # its evidence id


@dataclass
class PrSemantic:
    purpose: Text | None = None
    design_rationale: Text | None = None
    behavior_changes: list[BehaviorChange] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    walkthrough: dict[str, Text] = field(default_factory=dict)
    questions: list[dict[str, Any]] = field(default_factory=list)  # {question, answer: Text, capability_ids, flow_id}
    step_labels: dict[str, Text] = field(default_factory=dict)  # overlay step id -> business label
    names: dict[str, dict[str, Text]] = field(default_factory=dict)  # head_additions id -> {business_name, description}
    phases: dict[str, list[Phase]] = field(default_factory=dict)  # flow change id -> phases over its merged steps
    hypotheses: list[ArchCheck] = field(default_factory=list)  # architecture doubts: always needs_confirmation
    interface_notes: dict[str, Text] = field(default_factory=dict)  # interface check id -> who is affected (grounded)
    author_record: list[AuthorNote] = field(default_factory=list)
    dropped: int = 0
    reasons: list[str] = field(default_factory=list)


def _changed_files_for(refs: list[CodeRef], files: list[ChangedFile]) -> list[str]:
    out: list[str] = []
    for ref in refs:
        for f in files:
            side, path = ("base", f.old_path or f.path) if ref.side == "base" else ("head", f.path)
            if ref.path == path and f.id not in out:
                out.append(f.id)
    return out


MAX_AUTHOR_NOTES = 15
QUOTE_MIN, QUOTE_MAX = 8, 400


def _plain(s: str) -> str:
    """Markdown emphasis / code marks dropped, whitespace collapsed, lower case: a quote may lose formatting."""
    return " ".join(s.translate(str.maketrans("", "", "*`_")).split()).lower()


def gate_pr(payload: dict, known: PrKnown) -> PrSemantic:
    if not isinstance(payload, dict):
        raise ValueError("reply is not a JSON object")
    r = Resolver(known.elements, set(known.evidence), known.files)
    out = PrSemantic()

    def rule_backed(refs_ev: list[str]) -> bool:
        """Cites a rule written in an instruction file (CLAUDE.md, AGENTS.md, .claude/rules)."""
        return any(known.evidence[e].kind == "doc" and any(instruction_scope(x.path) is not None for x in known.evidence[e].refs)
                   for e in refs_ev)

    def text_of(obj: object, what: str, limit: int, extra: list[str] = ()) -> Text | None:
        if not isinstance(obj, dict):
            return None
        return r.text(obj.get("text"), _ids(obj.get("cites")) + list(extra), what, limit)

    out.purpose = text_of(payload.get("purpose"), "purpose", LIMITS["body"])
    out.design_rationale = text_of(payload.get("design_rationale"), "design_rationale", LIMITS["body"])

    for i, bc in enumerate(_list(payload.get("behavior_changes"))):
        flows, caps = _ids(bc.get("flow_ids")), _ids(bc.get("capability_ids"))
        bad = [x for x in flows if x not in known.flows] + [x for x in caps if x not in known.capabilities]
        if bad:
            r.drop(f"behavior_change {i}: unknown flow/capability {', '.join(bad[:3])}")
            continue
        cites = flows + caps + _ids(bc.get("cites"))
        after = r.text(bc.get("after"), cites, f"behavior_change {i}.after", LIMITS["body"])
        if after is None:
            continue
        before = r.text(bc.get("before"), cites, f"behavior_change {i}.before", LIMITS["body"]) if bc.get("before") else None
        out.behavior_changes.append(BehaviorChange(f"bc:semantic:{short_hash(after.text, *flows, *caps)}", after, flows, caps,
                                                   before))

    for i, fd in enumerate(_list(payload.get("findings"))):
        title = _str(fd.get("title"), LIMITS["title"])
        if not title:
            continue
        caps, flows = _ids(fd.get("capability_ids")), _ids(fd.get("flow_ids"))
        bad = [x for x in caps if x not in known.capabilities] + [x for x in flows if x not in known.flows]
        if bad:
            r.drop(f"finding '{title[:60]}': unknown capability/flow {', '.join(bad[:3])}")
            continue
        cites, precise = r.split(_ids(fd.get("cites")), f"finding '{title[:60]}'")
        refs_ev = r.resolve(cites, f"finding '{title[:60]}'")
        if refs_ev is None:
            continue
        refs = precise[:MAX_PRECISE] or [ref for e in refs_ev for ref in known.evidence[e].refs][:5]
        files = _changed_files_for(refs, known.files)  # only evidence located in a changed file links the finding
        if not refs or not files:
            r.drop(f"finding '{title[:60]}': not linked to a changed file with path:line evidence")
            continue
        sev = fd.get("severity") if fd.get("severity") in SEVERITIES else "attention"
        if rule_backed(refs_ev):
            sev = SEVERITY_UP[sev]  # a written rule of this repository backs the question
        focus = fd.get("focus") if fd.get("focus") in FOCUS else None
        fid = f"finding:semantic:{short_hash(title, *refs_ev)}"
        if any(f.id == fid for f in out.findings):
            continue  # Claude repeated itself
        out.findings.append(Finding(
            id=fid, source="semantic", title=title, description=_str(fd.get("description"), LIMITS["body"]),
            severity=sev, status="needs_confirmation", changed_file_ids=files, node_ids=[], refs=refs,
            evidence_refs=refs_ev, rule=f"semantic_review:{focus}" if focus else "semantic_review", capability_ids=caps,
            flow_ids=flows,
            business_impact=_str(fd.get("business_impact"), LIMITS["description"]) or None,
        ))

    for item in _list(payload.get("step_labels")):
        sid = item.get("step_id")
        if not isinstance(sid, str) or sid not in known.steps:
            r.drop(f"step_labels: unknown step {str(sid)[:80]}")
            continue
        lab = r.text(item.get("business_label"), [sid] + _ids(item.get("cites")), f"{sid}.business_label", LIMITS["label"])
        if lab:
            out.step_labels[sid] = lab

    for item in _list(payload.get("head_additions")):
        eid = item.get("id")
        if not isinstance(eid, str) or eid not in known.additions:
            r.drop(f"head_additions: unknown element {str(eid)[:80]}")
            continue
        cites = [eid] + _ids(item.get("cites"))
        got = {k: t for k, limit in (("business_name", LIMITS["name"]), ("description", LIMITS["description"]))
               if (t := r.text(item.get(k), cites, f"{eid}.{k}", limit))}
        if got:
            out.names[eid] = got

    for item in _list(payload.get("flow_phases")):
        fid = item.get("flow_id")
        if not isinstance(fid, str) or fid not in known.flow_steps:
            r.drop(f"flow_phases: unknown flow change {str(fid)[:80]}")
            continue
        if len(known.flow_steps[fid]) < PHASE_MIN_STEPS:
            continue
        got = gate_phases(fid, known.flow_steps[fid], item.get("phases"), r)
        if got is not None:
            out.phases[fid] = got

    for i, h in enumerate(_list(payload.get("architecture_hypotheses"))[:10]):
        title = _str(h.get("title"), LIMITS["title"])
        check = h.get("check") if h.get("check") in HYPOTHESIS_CHECKS else None
        caps = _ids(h.get("capability_ids"))
        iface = h.get("interface_id") if check == "interface_change" else None
        if not title or check is None or any(c not in known.capabilities for c in caps) or (
                check == "interface_change" and iface not in known.interfaces):
            r.drop(f"architecture_hypothesis {i}: missing title, unknown check, capability or interface")
            continue
        cites, precise = r.split(_ids(h.get("cites")), f"architecture_hypothesis '{title[:60]}'")
        refs_ev = r.resolve(cites, f"architecture_hypothesis '{title[:60]}'")
        if refs_ev is None:
            continue
        refs = [ref for e in refs_ev for ref in known.evidence[e].refs]
        pr_refs = precise[:MAX_PRECISE] or [x for x in refs if _changed_files_for([x], known.files)][:5]
        if not pr_refs:
            r.drop(f"architecture_hypothesis '{title[:60]}': not linked to a changed file with path:line evidence")
            continue
        existing = [x for x in refs if x not in pr_refs and not _changed_files_for([x], known.files)][:5]
        verdict = h.get("verdict") if h.get("verdict") in ("violates", "unclear") else "unclear"
        detail = _str(h.get("description"), LIMITS["body"]) or title
        out.hypotheses.append(ArchCheck(
            id=f"arch:hypothesis:{short_hash(title, *refs_ev)}", origin="hypothesis", source="semantic", check=check,
            ring=known.interfaces[iface] if iface else HYPOTHESIS_RING[check],
            verdict=verdict, status="needs_confirmation", title=title,
            interface_change=InterfaceChange(iface, "modified", "unknown", detail) if iface else None,
            description=_str(h.get("description"), LIMITS["body"]), capability_ids=caps, pr_refs=pr_refs,
            existing_refs=existing, evidence_refs=refs_ev,
            business_impact=_str(h.get("business_impact"), LIMITS["description"]) or None))

    for item in _list(payload.get("interface_notes"))[:30]:
        cid = item.get("check_id")
        if not isinstance(cid, str) or cid not in known.interface_checks:
            r.drop(f"interface_notes: unknown interface check {str(cid)[:80]}")
            continue
        note = r.text(item.get("who_is_affected"), [cid] + _ids(item.get("cites")), f"{cid}.who_is_affected", LIMITS["description"])
        if note:
            out.interface_notes[cid] = note

    body = _plain(known.pr_body)
    for i, item in enumerate(_list(payload.get("author_record"))[:MAX_AUTHOR_NOTES]):
        kind, quote = item.get("kind"), _str(item.get("quote"), QUOTE_MAX)
        text = _str(item.get("text"), LIMITS["description"])
        if kind not in AUTHOR_NOTE_KINDS or not text or not known.pr_evidence:
            r.drop(f"author_record {i}: kind {str(kind)[:30]!r} or text missing")
            continue
        if len(_plain(quote)) < QUOTE_MIN or _plain(quote) not in body:  # the author's words, not the AI's
            r.drop(f"author_record {i}: quote not found in the PR description")
            continue
        out.author_record.append(AuthorNote(f"author:{short_hash(kind, quote)}", kind,
                                            Text(text, "semantic", [known.pr_evidence]), quote))

    walk = payload.get("walkthrough")
    if isinstance(walk, dict):
        for key in WALK_KEYS:
            t = text_of(walk.get(key), f"walkthrough.{key}", LIMITS["body"])
            if t:
                out.walkthrough[key] = t

    for i, q in enumerate(_list(payload.get("questions"))[:12]):
        question = _str(q.get("question"), LIMITS["title"])
        caps, flow = _ids(q.get("capability_ids")), q.get("flow_id") if isinstance(q.get("flow_id"), str) else None
        bad = [c for c in caps if c not in known.capabilities] + ([flow] if flow and flow not in known.flows else [])
        if bad:
            r.drop(f"question {i}: unknown capability/flow {', '.join(bad[:3])}")
            continue
        ans = r.text(q.get("answer"), _ids(q.get("cites")) + caps + ([flow] if flow else []), f"question {i}", LIMITS["body"])
        if question and ans:
            out.questions.append({"question": question, "answer": ans, "capability_ids": caps, "flow_id": flow})

    out.dropped, out.reasons = r.dropped, r.reasons
    return out
