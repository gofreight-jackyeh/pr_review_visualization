"""Spec v2 Layer 5 — Business flow discovery: entry point → ordered business steps (pure).

A step is created when the walk
  * enters a different capability (kind `call`; the first step is `enter`),
  * acts on an entity through the ORM (`create` / `read` / `write` / `delete`),
  * emits (signal, background task) or calls outside the repository (`emit` / `external`),
  * raises, or dispatches dynamically (`call` with evidence_status unknown).
Calls that stay inside one capability are folded into that capability's current step, and consecutive identical
steps collapse into one. Steps under an if/loop/except are `possible` (with the condition); steps reached only via
dynamic (unknown) edges are `unknown`. Without statement order the flow is a `static_flow`, never an animation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import ids
from ..model.document import Branch, Step, Timeline
from ..model.graph import EntryFact, FlowStep, SideGraph
from .system_structure import Structure

MAX_STEPS = 60
MAX_DEPTH = 14
MAX_EXPANDED = 300
MAX_COMPONENTS = 12
ACTIONS = {"create", "read", "write", "delete"}
STATUS_RANK = {"confirmed": 0, "possible": 1, "unknown": 2}
FOCUS_REASON = "focused on the path to the changed code: calls that do not lead to it are left out"


@dataclass
class _Raw:
    kind: str
    cap: str
    comps: list[str]
    label: str
    status: str  # confirmed | unknown (possible is derived from conds)
    conds: tuple[str, ...]
    evidence: list[str]
    entity: str | None = None
    anchor: str = ""
    merged: int = 1

    @property
    def evidence_status(self) -> str:
        return "unknown" if self.status == "unknown" else ("possible" if self.conds else "confirmed")

    @property
    def collapse_key(self) -> tuple:
        return (self.kind, self.cap, self.entity, self.evidence_status, self.conds, self.anchor if self.kind == "call" else "")


@dataclass
class _Walk:
    sg: SideGraph
    st: Structure
    steps: list[_Raw] = field(default_factory=list)
    expanded: set[str] = field(default_factory=set)
    unordered: bool = False
    truncated: bool = False
    out_edges: dict | None = None
    highlight: set[str] | frozenset = frozenset()  # functions that get a step of their own even inside one capability
    focus: set[str] | None = None  # when set: outside highlighted code, only calls into these nodes are followed

    def add(self, r: _Raw) -> None:
        if not r.evidence:
            return  # a step without evidence cannot be shown (schema: evidence_refs minItems 1)
        if len(self.steps) >= MAX_STEPS:
            self.truncated = True
            return
        self.steps.append(r)

    def attach(self, cap: str, comp: str) -> None:
        for s in reversed(self.steps):
            if s.cap == cap:
                if comp not in s.comps and len(s.comps) < MAX_COMPONENTS:
                    s.comps.append(comp)
                return

    def label_of(self, nid: str) -> str:
        n = self.sg.nodes.get(nid)
        return (n.meta.get("dotted") or n.label) if n else nid

    def facts(self, fn: str) -> list[FlowStep]:
        facts = self.sg.flows.get(fn)
        if facts is not None:
            return facts
        node = self.sg.nodes.get(fn)
        if node is None or node.language in self.sg.ordered_languages:
            return []
        # No statement facts (e.g. TypeScript): fall back to the call graph, in a deterministic but NOT execution order.
        self.unordered = True
        out = []
        for e in self._out().get(fn, []):
            out.append(FlowStep("", 0, 0, "call", f"{self.label_of(e.target)}()", [e.target], [e.id], e.confidence,
                                evidence_refs=e.evidence_refs[:1]))
        return out

    def _out(self) -> dict:
        if self.out_edges is None:
            self.out_edges = {}
            for e in sorted(self.sg.edges.values(), key=lambda e: e.id):
                if e.kind in ("calls", "renders"):
                    self.out_edges.setdefault(e.source, []).append(e)
        return self.out_edges

    def visit(self, fn: str, conds: tuple[str, ...], status: str, cap: str, depth: int, free: bool = False) -> None:
        if fn in self.expanded or depth > MAX_DEPTH or len(self.expanded) >= MAX_EXPANDED:
            return
        self.expanded.add(fn)
        free = free or self.focus is None or fn in self.highlight  # inside changed code everything is followed
        for f in self.facts(fn):
            c = conds + f.conditions
            s = "unknown" if status == "unknown" or f.confidence == "unknown" else "confirmed"
            if f.kind == "call":
                if not free and not any(t in self.focus for t in f.target_ids):  # type: ignore[operator]
                    continue
                if f.confidence == "unknown":
                    self.add(_Raw("call", cap, [fn] + f.target_ids[:MAX_COMPONENTS - 1], f.label, "unknown", c, f.evidence_refs,
                                  anchor=f"dyn:{f.label}"))
                    for t in f.target_ids:
                        if t in self.sg.nodes and self.sg.nodes[t].kind != "external":
                            self.descend(t, c, "unknown", cap, depth, f, free)
                elif f.target_ids:
                    self.descend(f.target_ids[0], c, s, cap, depth, f, free)
            elif f.kind == "query" and f.target_ids:
                ent = self.st.entity_of_node.get(f.target_ids[0])
                action = f.orm_action if f.orm_action in ACTIONS else "read"
                model = self.sg.nodes[f.target_ids[0]].label if f.target_ids[0] in self.sg.nodes else "?"
                self.add(_Raw(action, cap, [fn], f"{action} {model} ({f.label})", s, c, f.evidence_refs, entity=ent, anchor=action))
            elif f.kind == "emit":
                target = f.target_ids[0] if f.target_ids else None
                tcap = self.st.node_cap.get(target, cap) if target else cap
                what = f"enqueue {self.label_of(target)}" if target else f"emit {f.label}"
                self.add(_Raw("emit", tcap, [target] if target else [fn], what, s, c, f.evidence_refs, anchor=target or f.label))
            elif f.kind == "external":
                self.add(_Raw("external", cap, [fn], f"external call {f.label}", s, c, f.evidence_refs, anchor=f.label))
            elif f.kind == "raise":
                self.add(_Raw("raise", cap, [fn], f.label, s, c, f.evidence_refs, anchor=f.label))

    def descend(self, t: str, conds: tuple[str, ...], status: str, cap: str, depth: int, f: FlowStep,
                free: bool = True) -> None:
        node = self.sg.nodes.get(t)
        if node is None or node.kind in ("model", "external") or not (free or t in self.focus):  # type: ignore[operator]
            return
        tcap = self.st.node_cap.get(t, cap)
        if tcap != cap:
            self.add(_Raw("call", tcap, [t], f"{self.st.cap_name(cap)} → {self.st.cap_name(tcap)}: {self.label_of(t)}()", status,
                          conds, f.evidence_refs, anchor=t))
        elif t in self.highlight:  # code a PR changed: its own step, so the flow shows where the change sits
            self.add(_Raw("call", tcap, [t], f"{self.st.cap_name(cap)}: {self.label_of(t)}()", status, conds, f.evidence_refs,
                          anchor=t))
        else:
            self.attach(cap, t)
        self.visit(t, conds, status, tcap, depth + 1, free)


def _collapse(raw: list[_Raw]) -> list[_Raw]:
    out: list[_Raw] = []
    for r in raw:
        prev = out[-1] if out else None
        if prev is not None and r.kind != "enter" and prev.collapse_key == r.collapse_key:
            prev.merged += 1
            prev.comps.extend(c for c in r.comps if c not in prev.comps and len(prev.comps) < MAX_COMPONENTS)
            prev.evidence.extend(e for e in r.evidence if e not in prev.evidence and len(prev.evidence) < 5)
            continue
        out.append(_Raw(r.kind, r.cap, list(r.comps), r.label, r.status, r.conds, list(r.evidence), r.entity, r.anchor))
    for r in out:
        if r.merged > 1:
            r.label = f"{r.label} (×{r.merged})"
    return out


def build_timeline(sg: SideGraph, st: Structure, epid: str, flow: str, highlight: set[str] | frozenset = frozenset(),
                   focus: set[str] | None = None) -> Timeline:
    """`highlight`: functions (a PR's changed code) shown as their own step instead of folded into their caller's.
    `focus`: nodes that reach the changed code; when given, calls outside it are skipped until the walk enters
    highlighted code, so a long flow still reaches a change that lies beyond its step limits."""
    fact: EntryFact = st.facts[epid]
    ep = st.entry_points[epid]
    w = _Walk(sg, st, highlight=highlight, focus=focus)
    handler = ep.handler_component_id
    enter_label = f"{fact.label} → {w.label_of(handler)}" if handler and handler != fact.id else fact.label
    w.add(_Raw("enter", ep.capability_id, [handler] if handler else [], enter_label, "confirmed", (), [fact.evidence_id],
               anchor="enter"))
    if handler:
        w.visit(handler, (), "confirmed", st.node_cap.get(handler, ep.capability_id), 0)
    raw = _collapse(w.steps)
    steps: list[Step] = []
    occ: dict[tuple, int] = {}
    for i, r in enumerate(raw):
        key = (r.kind, r.cap, r.entity, r.anchor)
        occ[key] = occ.get(key, 0) + 1
        cond = " and ".join(r.conds)
        steps.append(Step(
            id=ids.step_id(flow, key, occ[key]), order=i, kind=r.kind, capability_id=r.cap, component_ids=r.comps,
            technical_label=r.label[:300], evidence_status=r.evidence_status, evidence_refs=r.evidence[:5],
            entity_id=r.entity, branch=Branch(cond[:300]) if cond else None,
            meta={"key": key, "loose": (r.kind, r.cap, r.entity)},
        ))
    if w.unordered:
        return Timeline("static_flow", steps, "statement order is unknown for part of this flow (call graph without "
                                              "statement order, e.g. TypeScript): shown as a static flow, not an animation")
    if handler is None:
        return Timeline("static_flow", steps, "the entry point's handler could not be resolved statically")
    if len(steps) < 2:
        return Timeline("static_flow", steps, "only one business step: nothing to animate")
    reason = f"truncated at {MAX_STEPS} steps" if w.truncated else None
    return Timeline("animation", steps, reason)


# ---------------- base/head comparison ----------------

def _lcs(a: list, b: list) -> list[tuple[int, int]]:
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _differs(b: Step, h: Step, changed: set[str]) -> bool:
    """Only the step's primary component counts (the function that performs the action, or the one entered):
    other components are intra-capability calls folded into the step and would flag unrelated steps."""
    return (b.technical_label != h.technical_label or b.evidence_status != h.evidence_status
            or (b.branch.condition if b.branch else None) != (h.branch.condition if h.branch else None)
            or any(c in changed for c in h.component_ids[:1] + b.component_ids[:1]))


def diff_steps(base: list[Step], head: list[Step], changed: set[str], inside: set[str] | frozenset = frozenset()) -> list[Step]:
    """Merged step sequence with step_change_status. Steps are matched on (kind, capability, entity, anchor) by LCS;
    within each unmatched gap, base/head steps with the same (kind, capability, entity) pair up as `changed`.
    A matched step is `changed` when its label/status/branch differs or its primary component changed in the PR.
    Only when that finds nothing, a step that enqueues a task reaching changed code is `changed` (`inside`: nodes that
    transitively reach the change). Before view = every step except `added`; after view = every step except `removed`."""
    pairs = _lcs([s.meta["key"] for s in base], [s.meta["key"] for s in head])
    out: list[Step] = []

    def emit(step: Step, status: str) -> None:
        out.append(Step(step.id, 0, step.kind, step.capability_id, list(step.component_ids), step.technical_label,
                        step.evidence_status, list(step.evidence_refs), step.entity_id, step.business_label, step.branch,
                        status, dict(step.meta)))

    def gap(bs: list[Step], hs: list[Step]) -> None:
        used: set[int] = set()
        partner: dict[int, Step] = {}
        for j, h in enumerate(hs):
            for i, b in enumerate(bs):
                if i not in used and b.meta["loose"] == h.meta["loose"]:
                    used.add(i)
                    partner[j] = b
                    break
        for i, b in enumerate(bs):
            if i not in used:
                emit(b, "removed")
        for j, h in enumerate(hs):
            emit(h, "changed" if j in partner else "added")

    bi = hi = 0
    for i, j in pairs + [(len(base), len(head))]:
        gap(base[bi:i], head[hi:j])
        if i < len(base) and j < len(head):
            emit(head[j], "changed" if _differs(base[i], head[j], changed) else "unchanged")
        bi, hi = i + 1, j + 1
    if out and all(s.step_change_status == "unchanged" for s in out):
        for s in out:
            if s.kind == "emit" and any(c in inside for c in s.component_ids[:1]):
                s.step_change_status = "changed"
    seen: dict[str, int] = {}
    for k, s in enumerate(out):
        s.order = k
        if s.id in seen:  # a removed and an added step can share a content id; keep ids unique
            seen[s.id] += 1
            s.id = f"{s.id}-{seen[s.id]}"
        else:
            seen[s.id] = 1
    return out
